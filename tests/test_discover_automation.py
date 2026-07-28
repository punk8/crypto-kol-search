from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx

from kol_search.backend.connectors.base import ConnectorResult
from kol_search.backend.discover_automation import DiscoverAutomationService
from kol_search.backend.models import (
    AccountEvidence,
    AccountItem,
    AccountScoreComponent,
    AccountSummary,
    ContentItem,
    ContentMetrics,
    ReplyDraftRequest,
)
from kol_search.backend.reply_service import DeepSeekReplyService
from kol_search.platforms.x import XAccount
from kol_search.settings import Settings


def _settings(tmp_path, **values) -> Settings:  # noqa: ANN001
    return Settings(
        _env_file=None,
        KOL_DB_PATH=str(tmp_path / "discover.db"),
        KOL_PROVIDER_STATE_DB_PATH=str(tmp_path / "provider-state.db"),
        KOL_DISCOVERY_DOMAINS_JSON=(
            '[{"key":"ai","name":"AI","query":"AI agents OR LLM"}]'
        ),
        **values,
    )


def _content(
    *,
    post_id: str = "42",
    observed_at: datetime,
    published_at: datetime,
    likes: int,
) -> ContentItem:
    return ContentItem(
        platform="x",
        native_id=post_id,
        provider="fxembed",
        author=AccountSummary(native_id="7", username="alice"),
        text="A practical AI agents evaluation framework",
        canonical_url=f"https://x.com/alice/status/{post_id}",
        published_at=published_at,
        observed_at=observed_at,
        language="en",
        metrics=ContentMetrics(likes=likes, reposts=10, replies=5, quotes=2, views=5000),
    )


def test_multi_domain_candidates_require_two_qualified_runs_before_auto_enrollment(
    tmp_path,
) -> None:
    now = datetime.now(timezone.utc)
    recent = [
        _content(
            post_id=str(42 + index),
            observed_at=now,
            published_at=now - timedelta(days=index + 1),
            likes=200,
        )
        for index in range(2)
    ]
    item = AccountItem(
        platform="x",
        native_id="7",
        provider="getxapi",
        username="alice",
        display_name="Alice",
        followers_count=100_000,
        score=82,
        confidence="High",
        score_version="test",
        score_components=[
            AccountScoreComponent(
                key="relevance",
                label="Relevance",
                score=90,
                weight=1,
                reason="Strong match",
            )
        ],
        evidence=[
            AccountEvidence(kind="profile", summary="AI profile", observed_at=now),
            AccountEvidence(
                kind="domain_content",
                summary="Relevant post",
                native_content_id="42",
                observed_at=now,
            ),
            AccountEvidence(kind="recent_activity", summary="Recent", observed_at=now),
        ],
        recent_content=recent,
        observed_at=now,
    )

    queries = []

    class Connector:
        def search_accounts(self, query):  # noqa: ANN001, ANN201
            queries.append(query)
            return ConnectorResult(
                items=(item,), provider="getxapi+fxembed", attempted_providers=("fxembed", "getxapi")
            )

    service = DiscoverAutomationService(
        _settings(tmp_path, KOL_X_KOL_RECENT_POSTS_PER_ACCOUNT=2),
        Connector(),  # type: ignore[arg-type]
    )
    service.repository.upsert_account(
        XAccount(external_id="7", username="alice", source_provider="getxapi")
    )
    try:
        service.refresh_domains()
        first = service.repository.list_domain_memberships(domain_key="ai")
        service.refresh_domains()
        second = service.repository.list_domain_memberships(domain_key="ai")
        account = service.repository.get_kol("7")
    finally:
        service.close()

    assert first[0]["status"] == "candidate"
    assert first[0]["consecutive_qualified"] == 1
    assert second[0]["status"] == "active"
    assert second[0]["consecutive_qualified"] == 2
    assert account is not None and account["status"] == "active"
    assert queries
    assert all(query.recent_posts_per_account >= 3 for query in queries)


def test_hot_content_uses_repeat_metric_snapshots_for_velocity(tmp_path) -> None:
    now = datetime.now(timezone.utc)

    class Connector:
        pass

    service = DiscoverAutomationService(_settings(tmp_path), Connector())  # type: ignore[arg-type]
    try:
        service._persist_content(  # noqa: SLF001
            [
                _content(
                    observed_at=now - timedelta(minutes=30),
                    published_at=now - timedelta(hours=2),
                    likes=100,
                )
            ]
        )
        service._persist_content(  # noqa: SLF001
            [
                _content(
                    observed_at=now,
                    published_at=now - timedelta(hours=2),
                    likes=300,
                )
            ]
        )
        result = service.hot_content(source="discover", domain_key="ai", limit=10)
    finally:
        service.close()

    assert len(result.items) == 1
    assert result.items[0].velocity_per_hour > 0
    assert result.items[0].hot_score > 0
    assert result.items[0].matched_domains == ("ai",)


def test_deepseek_reply_draft_is_cached_and_returns_manual_reply_link(tmp_path) -> None:
    calls = 0
    now = datetime.now(timezone.utc)

    class Connector:
        def get_content(self, query):  # noqa: ANN001, ANN201
            return ConnectorResult(
                items=(
                    _content(
                        post_id=query.native_id,
                        observed_at=now,
                        published_at=now - timedelta(minutes=10),
                        likes=200,
                    ),
                ),
                provider="fxembed",
                attempted_providers=("fxembed",),
            )

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.headers["authorization"] == "Bearer secret"
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": "A useful framework—what benchmark changed your view most?"}}
                ]
            },
        )

    settings = _settings(
        tmp_path,
        DEEP_SEEK_API_KEY="secret",
        DEEP_SEEK_BASE_URL="https://deepseek.example/v1",
        DEEP_SEEK_MODEL="deepseek-chat",
    )
    client = httpx.Client(transport=httpx.MockTransport(handler))
    service = DeepSeekReplyService(settings, Connector(), client=client)  # type: ignore[arg-type]
    try:
        first = service.generate("42", ReplyDraftRequest())
        second = service.generate("42", ReplyDraftRequest())
    finally:
        service.close()
        client.close()

    assert calls == 1
    assert first.cached is False
    assert second.cached is True
    assert "in_reply_to=42" in first.reply_url
    assert first.draft == second.draft
