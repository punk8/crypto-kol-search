from __future__ import annotations

import json
from types import SimpleNamespace

from kol_search.platforms import PlatformRegistry
from kol_search.platforms.x import XTrendTweet, XTweet, create_x_plugin
from kol_search.platforms.xiaohongshu import (
    XiaohongshuComment,
    XiaohongshuNote,
    create_xiaohongshu_plugin,
)


def _cursor(**accounts: str) -> str:
    return json.dumps({"version": 1, "accounts": accounts})


def test_x_signal_cursor_filters_old_tweets_and_is_isolated_per_account():
    class Reader:
        name = "cursor-test"

        def get_user_tweets(  # noqa: ANN202
            self,
            external_id,  # noqa: ANN001
            _limit,  # noqa: ANN001
            *,
            username,  # noqa: ANN001
        ):
            assert username == external_id
            if external_id == "account-a":
                return [
                    {
                        "id": "a-old",
                        "author_id": external_id,
                        "created_at": "2026-07-20T09:00:00Z",
                    },
                    {
                        "id": "a-new",
                        "author_id": external_id,
                        "created_at": "2026-07-20T11:00:00Z",
                    },
                ]
            return [
                {
                    "id": "b-between",
                    "author_id": external_id,
                    "created_at": "2026-07-20T11:00:00Z",
                }
            ]

    registry = PlatformRegistry(
        [create_x_plugin(SimpleNamespace(), client_factory=Reader)]
    )
    result = registry.dispatch(
        "x",
        "scan_signals",
        payload={
            "account_external_ids": ["account-a", "account-b"],
            "include_trends": False,
            "cursor": _cursor(
                **{
                    "account-a": "2026-07-20T10:00:00Z",
                    "account-b": "2026-07-20T12:00:00Z",
                }
            ),
        },
    )

    tweets = [item for item in result.items if isinstance(item, XTweet)]
    assert result.success is True
    assert [tweet.external_id for tweet in tweets] == ["a-new"]
    assert json.loads(result.cursor or "")["accounts"] == {
        "account-a": "2026-07-20T11:00:00Z",
        "account-b": "2026-07-20T12:00:00Z",
    }


def test_x_signal_scan_uses_native_handle_without_replacing_stable_cursor_id():
    observed: list[tuple[str, str]] = []

    class Reader:
        name = "native-handle-test"

        def get_user_tweets(  # noqa: ANN202
            self,
            external_id,  # noqa: ANN001
            _limit,  # noqa: ANN001
            *,
            username,  # noqa: ANN001
        ):
            observed.append((external_id, username))
            return []

    registry = PlatformRegistry(
        [create_x_plugin(SimpleNamespace(), client_factory=Reader)]
    )
    result = registry.dispatch(
        "x",
        "scan_signals",
        payload={
            "account_external_ids": ["123456"],
            "account_targets": [{"id": "123456", "handle": "alice"}],
            "include_trends": False,
        },
    )

    assert result.success is True
    assert observed == [("123456", "alice")]


def test_x_signal_scan_passes_incremental_cursors_to_timeline_and_trend_search():
    observed: dict[str, str | None] = {}

    class Reader:
        name = "incremental-test"

        def get_user_tweets(  # noqa: ANN202
            self,
            external_id,  # noqa: ANN001
            _limit,  # noqa: ANN001
            *,
            username,  # noqa: ANN001
            start_time=None,  # noqa: ANN001
        ):
            observed["timeline"] = start_time
            return []

        def get_trends(self, _limit):  # noqa: ANN001, ANN202
            return [SimpleNamespace(name="#AI", rank=1, post_count=100)]

        def search_tweets(  # noqa: ANN202
            self,
            _query,  # noqa: ANN001
            _limit,  # noqa: ANN001
            *,
            start_time=None,  # noqa: ANN001
        ):
            observed["trends"] = start_time
            return [
                SimpleNamespace(
                    external_id="trend-old",
                    author_id="author-1",
                    author_username="alice",
                    text="#AI old",
                    created_at="2026-07-22T10:30:00Z",
                ),
                SimpleNamespace(
                    external_id="trend-new",
                    author_id="author-1",
                    author_username="alice",
                    text="#AI new",
                    created_at="2026-07-22T12:30:00Z",
                ),
            ]

    registry = PlatformRegistry(
        [create_x_plugin(SimpleNamespace(), client_factory=Reader)]
    )
    result = registry.dispatch(
        "x",
        "scan_signals",
        payload={
            "account_external_ids": ["account-a"],
            "account_targets": [{"id": "account-a", "handle": "alice"}],
            "include_trends": True,
            "cursor": _cursor(
                **{
                    "account-a": "2026-07-22T11:00:00Z",
                    "__trend_tweets__": "2026-07-22T12:00:00Z",
                }
            ),
        },
    )

    links = [item for item in result.items if isinstance(item, XTrendTweet)]
    assert observed == {
        "timeline": "2026-07-22T11:00:00Z",
        "trends": "2026-07-22T12:00:00Z",
    }
    assert [link.tweet.external_id for link in links] == ["trend-new"]
    assert json.loads(result.cursor or "")["accounts"]["__trend_tweets__"] == (
        "2026-07-22T12:30:00Z"
    )


def test_xiaohongshu_signal_cursor_filters_old_notes_and_is_isolated_per_account():
    class Reader:
        provider = "cursor-test"

        def get_account_posts(self, external_id, _limit):  # noqa: ANN001, ANN202
            if external_id == "account-a":
                return [
                    {
                        "id": "xiaohongshu:a-old",
                        "author_id": "xiaohongshu:account-a",
                        "created_at": "2026-07-20T09:00:00Z",
                    },
                    {
                        "id": "xiaohongshu:a-new",
                        "author_id": "xiaohongshu:account-a",
                        "created_at": "2026-07-20T11:00:00Z",
                    },
                ]
            return [
                {
                    "id": "xiaohongshu:b-between",
                    "author_id": "xiaohongshu:account-b",
                    "created_at": "2026-07-20T11:00:00Z",
                }
            ]

    registry = PlatformRegistry(
        [create_xiaohongshu_plugin(SimpleNamespace(), provider_factory=Reader)]
    )
    result = registry.dispatch(
        "xiaohongshu",
        "scan_signals",
        payload={
            "account_external_ids": ["account-a", "account-b"],
            "include_trends": False,
            "cursor": _cursor(
                **{
                    "account-a": "2026-07-20T10:00:00Z",
                    "account-b": "2026-07-20T12:00:00Z",
                }
            ),
        },
    )

    notes = [item for item in result.items if isinstance(item, XiaohongshuNote)]
    assert result.success is True
    assert [note.external_id for note in notes] == ["a-new"]
    assert json.loads(result.cursor or "")["accounts"] == {
        "account-a": "2026-07-20T11:00:00Z",
        "account-b": "2026-07-20T12:00:00Z",
    }


def test_xiaohongshu_scans_new_comments_on_note_older_than_content_cursor():
    comment_urls: list[str] = []

    class Reader:
        provider = "comment-test"

        def get_account_posts(self, _external_id, _limit):  # noqa: ANN001, ANN202
            return [
                {
                    "id": "xiaohongshu:existing-note",
                    "author_id": "xiaohongshu:account-a",
                    "created_at": "2026-07-20T09:00:00Z",
                    "url": "https://www.xiaohongshu.com/explore/existing-note",
                }
            ]

        def get_comments(self, note_url, _limit):  # noqa: ANN001, ANN202
            comment_urls.append(note_url)
            return [
                {
                    "id": "new-comment",
                    "user_id": "commenter-1",
                    "content": "新增评论",
                    "created_at": "2026-07-20T11:30:00Z",
                }
            ]

    registry = PlatformRegistry(
        [create_xiaohongshu_plugin(SimpleNamespace(), provider_factory=Reader)]
    )
    result = registry.dispatch(
        "xiaohongshu",
        "scan_signals",
        payload={
            "account_external_ids": ["account-a"],
            "include_trends": False,
            "include_comments": True,
            "cursor": _cursor(**{"account-a": "2026-07-20T10:00:00Z"}),
        },
    )

    assert result.success is True
    assert not any(isinstance(item, XiaohongshuNote) for item in result.items)
    comments = [
        item for item in result.items if isinstance(item, XiaohongshuComment)
    ]
    assert [comment.external_id for comment in comments] == ["new-comment"]
    assert [comment.note_external_id for comment in comments] == ["existing-note"]
    assert comment_urls == [
        "https://www.xiaohongshu.com/explore/existing-note"
    ]
    assert json.loads(result.cursor or "")["accounts"] == {
        "account-a": "2026-07-20T10:00:00Z"
    }
