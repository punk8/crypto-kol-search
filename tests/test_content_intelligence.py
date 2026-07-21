from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from kol_search.platform_modules.content_intelligence import (
    ContentInsight,
    NativeContentSample,
)
from kol_search.platform_modules.x_adapter import XAutomationAdapter
from kol_search.platform_modules.xiaohongshu_adapter import XiaohongshuAutomationAdapter
from kol_search.platforms.x import XAccount, XTrend, XTweet, XTweetMetrics
from kol_search.platforms.xiaohongshu import (
    XiaohongshuNote,
    XiaohongshuNoteMetrics,
    XiaohongshuTrend,
    XiaohongshuUser,
)
from kol_search.settings import Settings


class FakeContentIntelligence:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def analyze(
        self,
        *,
        platform_id: str,
        contents: Sequence[NativeContentSample],
        brand: Mapping[str, Any],
    ) -> Mapping[str, ContentInsight]:
        self.calls.append(
            {
                "platform_id": platform_id,
                "contents": tuple(contents),
                "brand": dict(brand),
            }
        )
        return {
            content.object_id: ContentInsight(
                object_id=content.object_id,
                relevance_score=0.91,
                cluster_key="stablecoin_regulation",
                cluster_title="Stablecoin regulation",
                reply_draft=f"AI draft for {platform_id}",
                rationale="same concrete policy narrative",
            )
            for content in contents
        }


class FailingContentIntelligence:
    def analyze(self, **_kwargs):  # noqa: ANN003, ANN202
        raise RuntimeError("model unavailable")


def _settings(path: Path) -> Settings:
    return Settings(_env_file=None, KOL_DB_PATH=str(path), OPENAI_API_KEY="")


def test_x_optional_intelligence_classifies_clusters_and_drafts(tmp_path: Path) -> None:
    intelligence = FakeContentIntelligence()
    adapter = XAutomationAdapter(
        _settings(tmp_path / "x-ai.db"), content_intelligence=intelligence
    )
    now = datetime.now(timezone.utc).isoformat()

    projection = adapter.project_signals(
        (
            XAccount(external_id="creator", username="creator"),
            XTweet(
                external_id="tweet-1",
                author_external_id="creator",
                text="A policy update without deterministic keyword matches",
                created_at=now,
            ),
        ),
        payload={"brand": {"brand_name": "Acme", "tone": "plain"}},
    )

    opportunity = projection.opportunities[0]
    assert adapter.repository.recent_relevant_content_count("creator") == 1
    assert opportunity.payload["cluster_key"] == "stablecoin_regulation"
    assert opportunity.suggested_actions[0].draft == "AI draft for x"
    assert projection.metadata["content_intelligence_count"] == 1
    assert intelligence.calls[0]["brand"]["brand_name"] == "Acme"


def test_xiaohongshu_optional_intelligence_keeps_native_note_model(
    tmp_path: Path,
) -> None:
    intelligence = FakeContentIntelligence()
    adapter = XiaohongshuAutomationAdapter(
        _settings(tmp_path / "xhs-ai.db"), content_intelligence=intelligence
    )
    now = datetime.now(timezone.utc).isoformat()

    projection = adapter.project_signals(
        (
            XiaohongshuUser(external_id="creator", nickname="Creator"),
            XiaohongshuNote(
                external_id="note-1",
                author_external_id="creator",
                title="政策观察",
                body="没有硬编码关键词的内容",
                published_at=now,
                note_type="video",
            ),
        ),
        payload={"brand": {"brand_name": "Acme"}},
    )

    opportunity = projection.opportunities[0]
    assert adapter.repository.recent_relevant_content_count("creator") == 1
    assert opportunity.payload["note_type"] == "video"
    assert opportunity.payload["cluster_title"] == "Stablecoin regulation"
    assert opportunity.suggested_actions[0].draft == "AI draft for xiaohongshu"
    assert intelligence.calls[0]["platform_id"] == "xiaohongshu"


def test_optional_intelligence_failure_falls_back_without_failing_projection(
    tmp_path: Path,
) -> None:
    adapter = XAutomationAdapter(
        _settings(tmp_path / "fallback.db"),
        content_intelligence=FailingContentIntelligence(),
    )
    now = datetime.now(timezone.utc).isoformat()

    projection = adapter.project_signals(
        (
            XAccount(external_id="creator", username="creator"),
            XTweet(
                external_id="tweet-1",
                author_external_id="creator",
                text="Ethereum RWA update",
                created_at=now,
            ),
        ),
        payload={},
    )

    assert projection.opportunities
    assert "model unavailable" in projection.warnings[0]
    assert adapter.repository.recent_relevant_content_count("creator") == 1


def test_irrelevant_high_engagement_content_and_trends_do_not_create_actions(
    tmp_path: Path,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    x_adapter = XAutomationAdapter(_settings(tmp_path / "x-relevance.db"))
    x_projection = x_adapter.project_signals(
        (
            XAccount(external_id="sports", username="sports"),
            XTweet(
                external_id="viral-sports",
                author_external_id="sports",
                text="Championship final and transfer news",
                created_at=now,
                metrics=XTweetMetrics(
                    likes=10_000_000, reposts=1_000_000, replies=100_000
                ),
            ),
            XTrend(name="World Cup final", rank=1, post_count=10_000_000),
        ),
        payload={},
    )
    assert x_projection.opportunities == ()

    xhs_adapter = XiaohongshuAutomationAdapter(
        _settings(tmp_path / "xhs-relevance.db")
    )
    xhs_projection = xhs_adapter.project_signals(
        (
            XiaohongshuUser(external_id="travel", nickname="旅行博主"),
            XiaohongshuNote(
                external_id="viral-travel",
                author_external_id="travel",
                title="周末旅行攻略",
                body="酒店与路线分享",
                published_at=now,
                metrics=XiaohongshuNoteMetrics(
                    likes=10_000_000, comments=1_000_000, collects=1_000_000
                ),
            ),
            XiaohongshuTrend(name="暑期旅行", rank=1, note_count=10_000_000),
        ),
        payload={},
    )
    assert xhs_projection.opportunities == ()


def test_negated_crypto_mentions_are_not_treated_as_relevant_content(
    tmp_path: Path,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    x_adapter = XAutomationAdapter(_settings(tmp_path / "x-negated-relevance.db"))
    x_projection = x_adapter.project_signals(
        (
            XAccount(external_id="food", username="food"),
            XTweet(
                external_id="food-post",
                author_external_id="food",
                text="Best dumplings recipe — no crypto here, just food.",
                created_at=now,
            ),
        ),
        payload={},
    )
    assert x_projection.opportunities == ()
    assert x_adapter.repository.recent_relevant_content_count("food") == 0

    xhs_adapter = XiaohongshuAutomationAdapter(
        _settings(tmp_path / "xhs-negated-relevance.db")
    )
    xhs_projection = xhs_adapter.project_signals(
        (
            XiaohongshuUser(external_id="food", nickname="美食博主"),
            XiaohongshuNote(
                external_id="food-note",
                author_external_id="food",
                title="饺子做法",
                body="这是一篇与加密无关的美食笔记",
                published_at=now,
            ),
        ),
        payload={},
    )
    assert xhs_projection.opportunities == ()
    assert xhs_adapter.repository.recent_relevant_content_count("food") == 0
