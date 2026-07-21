from pathlib import Path

from kol_search.discovery.promotion import KolStatus
from kol_search.platform_modules import (
    XAccount,
    XRepository,
    XTweet,
    XiaohongshuNote,
    XiaohongshuRepository,
    XiaohongshuUser,
)
from kol_search.platforms.x import XTrend, XTrendTweet


def test_x_and_xiaohongshu_keep_independent_native_models(tmp_path: Path) -> None:
    path = tmp_path / "native.db"
    x = XRepository(path)
    xhs = XiaohongshuRepository(path)

    x.upsert_account(XAccount(external_id="same", username="alice", followers_count=1_000))
    x.upsert_tweets([XTweet(external_id="tweet-1", author_external_id="same", text="RWA")])
    x.set_kol_status("same", KolStatus.ACTIVE, score=0.82, reasons=("seed relation",))

    xhs.upsert_user(
        XiaohongshuUser(external_id="same", nickname="alice", followers_count=200)
    )
    xhs.upsert_notes(
        [XiaohongshuNote(external_id="note-1", author_external_id="same", title="RWA")]
    )
    xhs.set_kol_status("same", KolStatus.REVIEW, score=0.61)

    assert x.list_kols()[0]["status"] == "active"
    assert xhs.list_kols()[0]["status"] == "review"
    assert x.summary()["content"] == 1
    assert xhs.summary()["content"] == 1


def test_xiaohongshu_migration_marks_legacy_local_author_hash_unresolved(
    tmp_path: Path,
) -> None:
    repository = XiaohongshuRepository(tmp_path / "legacy-xhs-author.db")
    legacy_hash = "abcdef0123456789abcd"
    repository.upsert_user(
        XiaohongshuUser(
            external_id=legacy_hash,
            nickname="Legacy author",
            source_provider="opencli",
        )
    )

    repository.migrate()

    saved = repository.get_kol(legacy_hash)
    assert saved is not None
    assert saved["native_id_resolved"] == 0
    targets, cursor = repository.list_scan_targets(
        statuses=("candidate",), after=None, limit=20
    )
    assert targets == []
    assert cursor is None


def test_x_workspace_feeds_are_time_ordered_and_exclude_paused_kols(tmp_path: Path) -> None:
    repository = XRepository(tmp_path / "x-workspace-feeds.db")
    repository.upsert_account(XAccount(external_id="active", username="active_kol"))
    repository.upsert_account(XAccount(external_id="paused", username="paused_kol"))
    repository.upsert_account(
        XAccount(external_id="trend-author", username="trend_author"),
        track_as_kol=False,
    )
    repository.set_kol_status("active", KolStatus.ACTIVE, score=0.9)
    repository.set_kol_status("paused", KolStatus.PAUSED, score=0.5)
    repository.upsert_tweets(
        [
            XTweet(
                external_id="older",
                author_external_id="active",
                author_username="active_kol",
                text="Older managed tweet",
                created_at="2026-07-21T08:00:00+00:00",
            ),
            XTweet(
                external_id="newer",
                author_external_id="active",
                author_username="active_kol",
                text="Newer managed tweet",
                created_at="2026-07-21T09:00:00+00:00",
            ),
            XTweet(
                external_id="paused-tweet",
                author_external_id="paused",
                author_username="paused_kol",
                text="Do not show",
                created_at="2026-07-21T10:00:00+00:00",
            ),
            XTweet(
                external_id="trend-tweet",
                author_external_id="trend-author",
                author_username="trend_author",
                text="A concrete tweet under the trend",
                created_at="2026-07-21T11:00:00+00:00",
            ),
        ]
    )
    repository.upsert_trends(
        [
            XTrend(name="Second trend", rank=2, post_count=50),
            XTrend(name="First trend", rank=1, post_count=100),
        ]
    )
    repository.upsert_trend_tweets(
        [
            XTrendTweet(
                trend_name="First trend",
                trend_rank=1,
                tweet=XTweet(
                    external_id="trend-tweet",
                    author_external_id="trend-author",
                    author_username="trend_author",
                ),
            )
        ]
    )

    assert [row["id"] for row in repository.list_managed_kol_tweets()] == [
        "newer",
        "older",
    ]
    assert [row["name"] for row in repository.list_recent_trends()] == [
        "First trend",
        "Second trend",
    ]
    assert repository.list_recent_trend_tweets(limit=1)[0]["id"] == "trend-tweet"
    assert repository.list_recent_trend_tweets(limit=1)[0]["trend_name"] == "First trend"
    assert repository.list_recent_trend_tweets(limit=1, offset=1) == []
