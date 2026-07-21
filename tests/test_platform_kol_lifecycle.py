from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from kol_search.discovery.promotion import (
    BalancedPromotionPolicy,
    KolStatus,
    PromotionInput,
)
from kol_search.platform_modules.x_adapter import XAutomationAdapter
from kol_search.platform_modules.x_native import XRepository
from kol_search.platform_modules.xiaohongshu_adapter import (
    XiaohongshuAutomationAdapter,
)
from kol_search.platform_modules.xiaohongshu_native import XiaohongshuRepository
from kol_search.platforms.x import XAccount, XTweet
from kol_search.platforms.xiaohongshu import (
    XiaohongshuComment,
    XiaohongshuNote,
    XiaohongshuUser,
)


def _settings(*, inactive_pause_days: int = 90) -> SimpleNamespace:
    return SimpleNamespace(
        inactive_pause_days=inactive_pause_days,
        signal_opportunity_ttl_hours=24,
        auto_publish_score=80.0,
    )


def _timestamp_days_ago(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def test_active_promotion_has_an_inactivity_grace_period() -> None:
    policy = BalancedPromotionPolicy(pause_after_days=90)

    retained = policy.decide(
        PromotionInput(
            score=0.40,
            relevant_content_count_30d=0,
            relationship_evidence_count=0,
            current_status=KolStatus.ACTIVE,
            inactive_days=30,
        )
    )
    paused = policy.decide(
        PromotionInput(
            score=0.40,
            relevant_content_count_30d=0,
            relationship_evidence_count=0,
            current_status=KolStatus.ACTIVE,
            inactive_days=91,
        )
    )

    assert retained.status == KolStatus.ACTIVE
    assert retained.qualified is False
    assert paused.status == KolStatus.PAUSED


def test_x_active_scan_does_not_reset_inactivity_clock(tmp_path: Path) -> None:
    repository = XRepository(tmp_path / "x-lifecycle.db")
    adapter = XAutomationAdapter(_settings(), repository=repository)
    account = XAccount(
        external_id="creator",
        username="creator",
        display_name="Creator",
        followers_count=10_000,
        source_provider="test",
    )
    repository.upsert_account(account)
    repository.set_kol_status("creator", KolStatus.ACTIVE, score=0.8)
    recent_qualification = _timestamp_days_ago(30)
    with repository.database.transaction() as connection:
        connection.execute(
            "UPDATE x_kols SET last_qualified_at=? WHERE account_id=?",
            (recent_qualification, "creator"),
        )

    adapter.project_discovery((account,), payload={})

    retained = repository.get_kol("creator")
    assert retained is not None
    assert retained["status"] == KolStatus.ACTIVE.value
    assert retained["last_qualified_at"] == recent_qualification

    stale_qualification = _timestamp_days_ago(91)
    with repository.database.transaction() as connection:
        connection.execute(
            "UPDATE x_kols SET last_qualified_at=? WHERE account_id=?",
            (stale_qualification, "creator"),
        )

    adapter.project_discovery((account,), payload={})

    paused = repository.get_kol("creator")
    assert paused is not None
    assert paused["status"] == KolStatus.PAUSED.value
    assert paused["last_qualified_at"] == stale_qualification


def test_x_automatic_scan_reclassifies_stored_review_without_new_content(
    tmp_path: Path,
) -> None:
    repository = XRepository(tmp_path / "x-auto-review.db")
    adapter = XAutomationAdapter(_settings(), repository=repository)
    account = XAccount(
        external_id="observed",
        username="observed",
        followers_count=10_000,
        source_provider="test",
    )
    repository.upsert_account(account)
    repository.upsert_tweets(
        [
            XTweet(
                external_id="stored-post",
                author_external_id="observed",
                author_username="observed",
                text="RWA research",
                created_at=datetime.now(timezone.utc).isoformat(),
                relevance_score=1.0,
                source_provider="test",
            )
        ]
    )
    repository.set_kol_status("observed", KolStatus.REVIEW, score=0.60)

    adapter.project_signals((), payload={})

    observed = repository.get_kol("observed")
    assert observed is not None
    assert observed["status"] == KolStatus.CANDIDATE.value
    assert "automatic scoring will continue" in observed["reasons"][0]


def test_xiaohongshu_active_scan_does_not_reset_inactivity_clock(
    tmp_path: Path,
) -> None:
    repository = XiaohongshuRepository(tmp_path / "xhs-lifecycle.db")
    adapter = XiaohongshuAutomationAdapter(_settings(), repository=repository)
    user = XiaohongshuUser(
        external_id="creator",
        nickname="创作者",
        followers_count=10_000,
        source_provider="test",
    )
    repository.upsert_user(user)
    repository.set_kol_status("creator", KolStatus.ACTIVE, score=0.8)
    recent_qualification = _timestamp_days_ago(30)
    with repository.database.transaction() as connection:
        connection.execute(
            "UPDATE xhs_kols SET last_qualified_at=? WHERE user_id=?",
            (recent_qualification, "creator"),
        )

    adapter.project_discovery((user,), payload={})

    retained = repository.get_kol("creator")
    assert retained is not None
    assert retained["status"] == KolStatus.ACTIVE.value
    assert retained["last_qualified_at"] == recent_qualification

    stale_qualification = _timestamp_days_ago(91)
    with repository.database.transaction() as connection:
        connection.execute(
            "UPDATE xhs_kols SET last_qualified_at=? WHERE user_id=?",
            (stale_qualification, "creator"),
        )

    adapter.project_discovery((user,), payload={})

    paused = repository.get_kol("creator")
    assert paused is not None
    assert paused["status"] == KolStatus.PAUSED.value
    assert paused["last_qualified_at"] == stale_qualification


def test_xiaohongshu_seed_note_comment_discovers_candidate_then_promotes(
    tmp_path: Path,
) -> None:
    repository = XiaohongshuRepository(tmp_path / "xhs-comment-discovery.db")
    adapter = XiaohongshuAutomationAdapter(_settings(), repository=repository)
    repository.upsert_user(
        XiaohongshuUser(external_id="seed", nickname="种子账号")
    )
    repository.set_kol_status("seed", KolStatus.SEED, score=1.0)
    now = datetime.now(timezone.utc).isoformat()
    seed_note = XiaohongshuNote(
        external_id="seed-note",
        author_external_id="seed",
        title="RWA 研究",
        published_at=now,
    )
    comment = XiaohongshuComment(
        external_id="comment-1",
        note_external_id="seed-note",
        author_external_id="candidate",
        author_nickname="候选人",
        body="值得继续研究",
        created_at=now,
    )

    adapter.project_signals((seed_note, comment), payload={})

    discovered = repository.get_kol("candidate")
    assert discovered is not None
    assert discovered["status"] == KolStatus.CANDIDATE.value
    assert repository.relationship_evidence_count("candidate") == 1

    adapter.project_discovery(
        tuple(
            XiaohongshuNote(
                external_id=f"candidate-note-{index}",
                author_external_id="candidate",
                title=f"Crypto RWA research {index}",
                published_at=now,
            )
            for index in range(3)
        ),
        payload={},
    )

    promoted = repository.get_kol("candidate")
    assert promoted is not None
    assert promoted["status"] == KolStatus.ACTIVE.value


def test_x_projection_rolls_back_native_writes_when_promotion_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = XRepository(tmp_path / "x-rollback.db")
    adapter = XAutomationAdapter(_settings(), repository=repository)
    account = XAccount(external_id="creator", username="creator", source_provider="test")
    tweet = XTweet(
        external_id="tweet-1",
        author_external_id="creator",
        author_username="creator",
        text="crypto research",
        created_at=datetime.now(timezone.utc).isoformat(),
        source_provider="test",
    )

    def fail_promotion(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("promotion failed")

    monkeypatch.setattr(adapter, "_promote", fail_promotion)
    with pytest.raises(RuntimeError, match="promotion failed"):
        adapter.project_discovery((account, tweet), payload={})

    assert repository.get_kol("creator") is None
    with repository.database.transaction() as connection:
        assert connection.execute("SELECT COUNT(*) FROM x_tweets").fetchone()[0] == 0


def test_xiaohongshu_projection_rolls_back_native_writes_when_promotion_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = XiaohongshuRepository(tmp_path / "xhs-rollback.db")
    adapter = XiaohongshuAutomationAdapter(_settings(), repository=repository)
    user = XiaohongshuUser(external_id="creator", nickname="创作者", source_provider="test")
    note = XiaohongshuNote(
        external_id="note-1",
        author_external_id="creator",
        author_nickname="创作者",
        title="crypto research",
        published_at=datetime.now(timezone.utc).isoformat(),
        source_provider="test",
    )

    def fail_promotion(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("promotion failed")

    monkeypatch.setattr(adapter, "_promote", fail_promotion)
    with pytest.raises(RuntimeError, match="promotion failed"):
        adapter.project_discovery((user, note), payload={})

    assert repository.get_kol("creator") is None
    with repository.database.transaction() as connection:
        assert connection.execute("SELECT COUNT(*) FROM xhs_notes").fetchone()[0] == 0


def test_x_content_update_beyond_first_500_kols_preserves_rich_profile(
    tmp_path: Path,
) -> None:
    repository = XRepository(tmp_path / "x-large-library.db")
    adapter = XAutomationAdapter(_settings(), repository=repository)
    target = XAccount(
        external_id="target",
        username="rich-handle",
        display_name="Rich Profile",
        description="Complete biography",
        followers_count=42,
        source_provider="profile_api",
    )
    with repository.unit_of_work():
        repository.upsert_account(target)
        for index in range(500):
            repository.upsert_account(
                XAccount(
                    external_id=f"filler-{index:03d}",
                    username=f"filler{index:03d}",
                    followers_count=10_000 + index,
                    source_provider="test",
                )
            )
    assert all(row["id"] != "target" for row in repository.list_kols(limit=500))

    adapter.project_discovery(
        (
            XTweet(
                external_id="target-tweet",
                author_external_id="target",
                author_username="rich-handle",
                text="crypto research",
                created_at=datetime.now(timezone.utc).isoformat(),
                source_provider="timeline",
            ),
        ),
        payload={},
    )

    stored = repository.get_kol("target")
    assert stored is not None
    assert stored["display_name"] == "Rich Profile"
    assert stored["bio"] == "Complete biography"
    assert stored["followers_count"] == 42
    assert stored["source_provider"] == "profile_api"


def test_xiaohongshu_content_update_beyond_first_500_kols_preserves_rich_profile(
    tmp_path: Path,
) -> None:
    repository = XiaohongshuRepository(tmp_path / "xhs-large-library.db")
    adapter = XiaohongshuAutomationAdapter(_settings(), repository=repository)
    target = XiaohongshuUser(
        external_id="target",
        nickname="完整昵称",
        bio="完整简介",
        followers_count=42,
        source_provider="profile_api",
    )
    with repository.unit_of_work():
        repository.upsert_user(target)
        for index in range(500):
            repository.upsert_user(
                XiaohongshuUser(
                    external_id=f"filler-{index:03d}",
                    nickname=f"填充账号 {index:03d}",
                    followers_count=10_000 + index,
                    source_provider="test",
                )
            )
    assert all(row["id"] != "target" for row in repository.list_kols(limit=500))

    adapter.project_discovery(
        (
            XiaohongshuNote(
                external_id="target-note",
                author_external_id="target",
                author_nickname="完整昵称",
                title="crypto research",
                published_at=datetime.now(timezone.utc).isoformat(),
                source_provider="timeline",
            ),
        ),
        payload={},
    )

    stored = repository.get_kol("target")
    assert stored is not None
    assert stored["nickname"] == "完整昵称"
    assert stored["bio"] == "完整简介"
    assert stored["followers_count"] == 42
    assert stored["source_provider"] == "profile_api"
