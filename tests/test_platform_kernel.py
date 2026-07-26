from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

import kol_search.platforms as platforms

from kol_search.platforms import (
    ActionExecutionResult,
    PlatformCapability,
    PlatformHealthResult,
    PlatformManifest,
    PlatformPlugin,
    PlatformRegistry,
    PlatformTaskContext,
    PlatformTaskName,
    PlatformTaskResult,
    build_default_registry,
)
from kol_search.platforms.x import XTrendTweet, create_x_plugin
from kol_search.platforms.xiaohongshu import create_xiaohongshu_plugin


@dataclass(frozen=True)
class Channel:
    channel_id: str
    title: str
    subscriber_count: int


@dataclass(frozen=True)
class Video:
    video_id: str
    channel_id: str
    title: str
    duration_seconds: int
    average_view_percentage: float


def _video_plugin(calls: list[str] | None = None) -> PlatformPlugin:
    def discover(context: PlatformTaskContext) -> PlatformTaskResult:
        assert context.payload["query"] == "crypto research"
        return PlatformTaskResult.completed(
            (
                Channel("channel-1", "Deep Research", 42_000),
                Video("video-1", "channel-1", "RWA", 913, 0.67),
            ),
            cursor="next-page",
        )

    def execute(context: PlatformTaskContext) -> ActionExecutionResult:
        if calls is not None:
            calls.append(str(context.payload["action_type"]))
        return ActionExecutionResult(
            success=True,
            external_id="comment-1",
            raw_receipt={"accepted": True},
            confirmed=False,
        )

    def install_schema(connection: object) -> None:
        assert isinstance(connection, sqlite3.Connection)
        connection.execute(
            "CREATE TABLE video_channels (channel_id TEXT PRIMARY KEY, title TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE video_videos (video_id TEXT PRIMARY KEY, duration_seconds INTEGER)"
        )

    return PlatformPlugin(
        manifest=PlatformManifest(
            platform_id="video_test",
            name="Video Test",
            version="1.0",
            capabilities=frozenset(
                {
                    PlatformCapability.ACCOUNT_SEARCH,
                    PlatformCapability.CONTENT_SEARCH,
                    PlatformCapability.FEED,
                    PlatformCapability.COMMENT,
                    PlatformCapability.ANALYTICS,
                }
            ),
            safety_limits={"comment_daily": 5},
            workbench_path="/platforms/video-test",
        ),
        handlers={
            PlatformTaskName.DISCOVER: discover,
            PlatformTaskName.EXECUTE_ACTION: execute,
        },
        native_models={"channel": Channel, "video": Video},
        schema_installer=install_schema,
        router_factory=lambda: object(),
        health_check=lambda: PlatformHealthResult("video_test", ready=True),
    )


def test_fake_video_platform_keeps_channel_and_video_models_out_of_core():
    registry = PlatformRegistry()
    plugin = registry.register(_video_plugin())

    result = registry.dispatch(
        "video_test",
        "discover",
        payload={"query": "crypto research"},
    )

    assert isinstance(result, PlatformTaskResult)
    assert isinstance(result.items[0], Channel)
    assert isinstance(result.items[1], Video)
    assert result.items[1].duration_seconds == 913
    assert plugin.native_models == {"channel": Channel, "video": Video}
    assert registry.supports("video_test", "feed")
    assert not hasattr(result.items[0], "username")
    assert not hasattr(result.items[1], "text")

    connection = sqlite3.connect(":memory:")
    assert registry.install_schemas(connection) == ("video_test",)
    names = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    assert names == {"video_channels", "video_videos"}


def test_action_dispatch_is_exactly_once_and_preserves_uncertain_receipt():
    calls: list[str] = []
    registry = PlatformRegistry([_video_plugin(calls)])

    result = registry.dispatch(
        "video_test",
        PlatformTaskName.EXECUTE_ACTION,
        context=PlatformTaskContext(
            platform_id="video_test",
            payload={"action_type": "comment"},
        ),
    )

    assert isinstance(result, ActionExecutionResult)
    assert result.success is True
    assert result.confirmation_required is True
    assert calls == ["comment"]


def test_registry_health_isolates_platform_failures():
    broken = PlatformPlugin(
        manifest=PlatformManifest("broken", "Broken", "1"),
        health_check=lambda: (_ for _ in ()).throw(RuntimeError("provider down")),
    )
    registry = PlatformRegistry([broken, _video_plugin()])

    results = {item.platform_id: item for item in registry.health()}

    assert results["broken"].ready is False
    assert results["broken"].detail == "provider down"
    assert results["video_test"].ready is True
    with pytest.raises(ValueError, match="already registered"):
        registry.register(_video_plugin())


def test_default_registry_enables_x_and_allows_explicit_xiaohongshu_registration():
    settings = SimpleNamespace(
        signal_interval_minutes=15,
        comment_hourly_limit=3,
        comment_daily_limit=10,
        dm_hourly_limit=2,
        dm_daily_limit=5,
        author_cooldown_days=7,
    )

    registry = build_default_registry(settings)

    assert registry.platform_ids == ("x",)
    assert registry.require("x").manifest.default_scan_interval_seconds == 15 * 60
    assert registry.supports("x", PlatformCapability.OWNED_PUBLISH)
    assert registry.require("x").native_models["tweet"].__name__ == "XTweet"

    settings.x_signal_interval_minutes = 5
    settings.enabled_platforms = "x,xiaohongshu"
    overridden_registry = build_default_registry(settings)
    assert overridden_registry.require("x").manifest.default_scan_interval_seconds == 5 * 60
    assert (
        overridden_registry.require("xiaohongshu").manifest.default_scan_interval_seconds
        == 15 * 60
    )
    assert not overridden_registry.supports(
        "xiaohongshu", PlatformCapability.OWNED_PUBLISH
    )
    assert (
        overridden_registry.require("xiaohongshu").native_models["note"].__name__
        == "XiaohongshuNote"
    )

    settings.enabled_platforms = "xiaohongshu"
    selected = build_default_registry(settings)
    assert selected.platform_ids == ("xiaohongshu",)


def test_platform_public_api_does_not_export_legacy_account_post_reader_contract():
    assert "PlatformReader" not in platforms.__all__
    assert "create_platform_readers" not in platforms.__all__
    assert not hasattr(platforms, "PlatformReader")
    assert not hasattr(platforms, "create_platform_readers")


def test_builtin_plugins_construct_their_own_read_providers(monkeypatch):
    x_factory_calls: list[tuple[object, object]] = []
    class XClient:
        name = "factory-test"

        def search_users(self, _query, _limit):  # noqa: ANN001, ANN202
            return [{"id": "x-user", "username": "alice"}]

        def search_tweets(self, _query, _limit):  # noqa: ANN001, ANN202
            return [
                {
                    "id": "tweet-1",
                    "author_id": "x-user",
                    "author_username": "alice",
                    "text": "RWA",
                }
            ]

    def create_x_client(backend, settings):  # noqa: ANN001, ANN202
        x_factory_calls.append((backend, settings))
        return XClient()

    class XiaohongshuProvider:
        provider = "http-test"

        def search_posts(self, _query, _limit):  # noqa: ANN001, ANN202
            return [
                {
                    "id": "xiaohongshu:note-1",
                    "author_id": "xiaohongshu:user-1",
                    "text": "RWA\n研究笔记",
                }
            ]

    monkeypatch.setattr(
        "kol_search.twitter.factory.create_x_read_provider",
        create_x_client,
    )
    settings = SimpleNamespace(twitter_backend="mock")
    x_result = PlatformRegistry([create_x_plugin(settings)]).dispatch(
        "x", "discover", payload={"query": "RWA"}
    )
    xhs_result = PlatformRegistry(
        [create_xiaohongshu_plugin(settings, provider_factory=XiaohongshuProvider)]
    ).dispatch(
        "xiaohongshu", "discover", payload={"query": "RWA"}
    )

    assert x_result.success is True
    assert [type(item).__name__ for item in x_result.items] == ["XAccount", "XTweet"]
    assert x_factory_calls == [("mock", settings)]
    assert xhs_result.success is True
    assert [type(item).__name__ for item in xhs_result.items] == [
        "XiaohongshuNote"
    ]


def test_xiaohongshu_without_http_provider_reports_unavailable():
    plugin = create_xiaohongshu_plugin(SimpleNamespace())

    health = plugin.health_check()

    assert health.ready is False
    assert health.detail == "HTTP API provider is not configured"


def test_x_discovery_reports_total_provider_failure_but_keeps_partial_success():
    settings = SimpleNamespace()

    class BrokenClient:
        name = "test"

        def search_users(self, _query, _limit):  # noqa: ANN001, ANN202
            raise RuntimeError("account endpoint down")

        def search_tweets(self, _query, _limit):  # noqa: ANN001, ANN202
            raise RuntimeError("content endpoint down")

    failed_registry = PlatformRegistry(
        [create_x_plugin(settings, client_factory=BrokenClient)]
    )
    failed = failed_registry.dispatch("x", "discover", payload={"query": "RWA"})

    assert failed.success is False
    assert failed.metadata["attempted_calls"] == 2
    assert failed.metadata["succeeded_calls"] == 0
    assert len(failed.warnings) == 2

    class PartialClient(BrokenClient):
        def search_users(self, _query, _limit):  # noqa: ANN001, ANN202
            return []

    partial_registry = PlatformRegistry(
        [create_x_plugin(settings, client_factory=PartialClient)]
    )
    partial = partial_registry.dispatch("x", "discover", payload={"query": "RWA"})

    assert partial.success is True
    assert partial.metadata["attempted_calls"] == 2
    assert partial.metadata["succeeded_calls"] == 1
    assert len(partial.warnings) == 1


def test_x_signal_scan_expands_native_trends_into_concrete_tweets():
    calls: list[tuple[str, int]] = []

    class Client:
        name = "test"

        def get_trends(self, _limit):  # noqa: ANN001, ANN202
            return [
                SimpleNamespace(name="Trend One", rank=1, post_count=100),
                SimpleNamespace(name="Trend Two", rank=2, post_count=50),
            ]

        def search_tweets(self, query, limit):  # noqa: ANN001, ANN202
            calls.append((query, limit))
            return [
                SimpleNamespace(
                    external_id=f"tweet-{query}",
                    author_id="author-1",
                    author_username="alice",
                    text="Tweet about Trend Two",
                )
            ]

    registry = PlatformRegistry(
        [
            create_x_plugin(
                SimpleNamespace(x_trends_per_scan=2, x_tweets_per_trend=7),
                client_factory=Client,
            )
        ]
    )
    result = registry.dispatch(
        "x", "scan_signals", payload={"include_trends": True}
    )

    links = [item for item in result.items if isinstance(item, XTrendTweet)]
    assert result.success is True
    assert calls == [("Trend One OR Trend Two", 14)]
    assert len(links) == 1
    assert links[0].trend_name == "Trend Two"
    assert links[0].tweet.text == "Tweet about Trend Two"


def test_xiaohongshu_signal_scan_reports_total_provider_failure():
    class BrokenReader:
        provider = "test"

        def get_account_posts(self, _external_id, _limit):  # noqa: ANN001, ANN202
            raise RuntimeError("feed down")

        def get_trends(self, _limit):  # noqa: ANN001, ANN202
            raise RuntimeError("trends down")

    registry = PlatformRegistry(
        [create_xiaohongshu_plugin(SimpleNamespace(), provider_factory=BrokenReader)]
    )
    result = registry.dispatch(
        "xiaohongshu",
        "scan_signals",
        payload={"account_external_ids": ["user-1"], "include_trends": True},
    )

    assert result.success is False
    assert result.metadata == {
        "provider": "test",
        "attempted_calls": 2,
        "succeeded_calls": 0,
    }
    assert len(result.warnings) == 2
