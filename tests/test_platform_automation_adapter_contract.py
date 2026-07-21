from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from kol_search.platforms import (
    NativeObjectReference,
    PlatformActionProposal,
    PlatformAutomationAdapter,
    PlatformCapability,
    PlatformManifest,
    PlatformOpportunityProposal,
    PlatformPlugin,
    PlatformProjectionResult,
    PlatformRegistry,
    PlatformSuggestedAction,
)
from kol_search.platform_modules.x_adapter import XAutomationAdapter
from kol_search.platform_modules.x_native import XRepository
from kol_search.platform_modules.xiaohongshu_adapter import XiaohongshuAutomationAdapter
from kol_search.platform_modules.xiaohongshu_native import XiaohongshuRepository
from kol_search.platforms.x import (
    XAccount,
    XAccountRelation,
    XTrend,
    XTrendTweet,
    XTweet,
    XTweetMetrics,
)
from kol_search.platforms.xiaohongshu import (
    XiaohongshuNote,
    XiaohongshuNoteMetrics,
    XiaohongshuTrend,
    XiaohongshuUser,
)


@dataclass(frozen=True)
class VideoChannel:
    channel_id: str
    title: str
    subscribers: int


@dataclass(frozen=True)
class VideoAsset:
    video_id: str
    channel_id: str
    title: str
    duration_seconds: int
    average_view_percentage: float


class VideoAutomationAdapter:
    platform_id = "video_lab"

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def project_discovery(
        self, items, *, payload  # noqa: ANN001
    ) -> PlatformProjectionResult:
        channels = [item for item in items if isinstance(item, VideoChannel)]
        videos = [item for item in items if isinstance(item, VideoAsset)]
        for channel in channels:
            self.connection.execute(
                "INSERT OR REPLACE INTO video_channels VALUES(?, ?, ?, 'candidate')",
                (channel.channel_id, channel.title, channel.subscribers),
            )
        for video in videos:
            self.connection.execute(
                "INSERT OR REPLACE INTO video_assets VALUES(?, ?, ?, ?, ?)",
                (
                    video.video_id,
                    video.channel_id,
                    video.title,
                    video.duration_seconds,
                    video.average_view_percentage,
                ),
            )
        self.connection.commit()
        return PlatformProjectionResult(
            native_counts={"channels": len(channels), "videos": len(videos)},
            metadata={"query": payload.get("query")},
        )

    def project_signals(
        self, items, *, payload  # noqa: ANN001
    ) -> PlatformProjectionResult:
        native = self.project_discovery(items, payload=payload)
        proposals = []
        for video in (item for item in items if isinstance(item, VideoAsset)):
            score = min(100.0, video.average_view_percentage * 100 + 20)
            draft = f"What should viewers test after {video.title}?"
            proposals.append(
                PlatformOpportunityProposal(
                    opportunity_type="video_comment",
                    native_object=NativeObjectReference(
                        self.platform_id, "video", video.video_id
                    ),
                    priority=round(score),
                    score=score,
                    title=video.title,
                    evidence=(
                        {
                            "kind": "retention",
                            "average_view_percentage": video.average_view_percentage,
                            "duration_seconds": video.duration_seconds,
                        },
                    ),
                    score_reasons=("video-local audience retention score",),
                    suggested_actions=(
                        PlatformActionProposal(
                            PlatformSuggestedAction.COMMENT,
                            draft=draft,
                            score=score,
                        ),
                    ),
                )
            )
        return PlatformProjectionResult(
            native_counts=native.native_counts,
            opportunities=tuple(proposals),
        )

    def list_kols(
        self, *, status: str = "all", limit: int = 100
    ) -> list[dict[str, Any]]:
        where = "" if status == "all" else "WHERE status=?"
        params: tuple[object, ...] = () if status == "all" else (status,)
        rows = self.connection.execute(
            f"SELECT * FROM video_channels {where} ORDER BY subscribers DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
        return [dict(zip(("id", "title", "subscribers", "status"), row)) for row in rows]

    def get_kol(self, native_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM video_channels WHERE channel_id=?", (native_id,)
        ).fetchone()
        if row is None:
            return None
        return dict(zip(("id", "title", "subscribers", "status"), row))

    def list_scan_targets(
        self,
        *,
        statuses,  # noqa: ANN001
        after: str | None,
        limit: int,
    ) -> tuple[list[dict[str, Any]], str | None]:
        values = tuple(dict.fromkeys(statuses))
        if not values:
            return [], None
        clauses = [f"status IN ({','.join('?' for _ in values)})"]
        params: list[object] = list(values)
        if after:
            clauses.append("channel_id>?")
            params.append(after)
        batch_size = max(1, limit)
        rows = self.connection.execute(
            f"SELECT * FROM video_channels WHERE {' AND '.join(clauses)} "
            "ORDER BY channel_id ASC LIMIT ?",
            (*params, batch_size + 1),
        ).fetchall()
        targets = [
            dict(zip(("id", "title", "subscribers", "status"), row))
            for row in rows[:batch_size]
        ]
        next_cursor = targets[-1]["id"] if len(rows) > batch_size else None
        return targets, next_cursor

    def summary(self) -> dict[str, int]:
        return {
            "accounts": self.connection.execute(
                "SELECT COUNT(*) FROM video_channels"
            ).fetchone()[0],
            "content": self.connection.execute(
                "SELECT COUNT(*) FROM video_assets"
            ).fetchone()[0],
        }

    def set_kol_status(self, native_id: str, status: str) -> None:
        cursor = self.connection.execute(
            "UPDATE video_channels SET status=? WHERE channel_id=?", (status, native_id)
        )
        self.connection.commit()
        if not cursor.rowcount:
            raise KeyError(native_id)

    def add_seed(self, native_id: str, display_name: str | None = None) -> None:
        self.connection.execute(
            "INSERT OR REPLACE INTO video_channels VALUES(?, ?, 0, 'seed')",
            (native_id, display_name or native_id),
        )
        self.connection.commit()


def _install_video_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE video_channels(
            channel_id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            subscribers INTEGER NOT NULL,
            status TEXT NOT NULL
        );
        CREATE TABLE video_assets(
            video_id TEXT PRIMARY KEY,
            channel_id TEXT NOT NULL,
            title TEXT NOT NULL,
            duration_seconds INTEGER NOT NULL,
            average_view_percentage REAL NOT NULL,
            FOREIGN KEY(channel_id) REFERENCES video_channels(channel_id)
        );
        """
    )


def test_video_adapter_runs_end_to_end_without_shared_account_or_post_model() -> None:
    connection = sqlite3.connect(":memory:")
    adapter = VideoAutomationAdapter(connection)
    plugin = PlatformPlugin(
        manifest=PlatformManifest(
            platform_id="video_lab",
            name="Video Lab",
            version="1",
            capabilities=frozenset(
                {
                    PlatformCapability.ACCOUNT_SEARCH,
                    PlatformCapability.CONTENT_SEARCH,
                    PlatformCapability.ANALYTICS,
                    PlatformCapability.COMMENT,
                }
            ),
        ),
        native_models={"channel": VideoChannel, "video": VideoAsset},
        automation_adapter=adapter,
        schema_installer=_install_video_schema,
    )
    registry = PlatformRegistry([plugin])

    assert isinstance(registry.resolve_automation_adapter("video_lab"), PlatformAutomationAdapter)
    assert registry.install_schemas(connection) == ("video_lab",)

    channel = VideoChannel("channel-1", "Research Lab", 42_000)
    video = VideoAsset("video-1", "channel-1", "RWA deep dive", 913, 0.67)
    discovery = adapter.project_discovery(
        (channel, video), payload={"query": "crypto research"}
    )
    signals = adapter.project_signals((video,), payload={})

    assert discovery.native_counts == {"channels": 1, "videos": 1}
    assert adapter.summary() == {"accounts": 1, "content": 1}
    proposal = signals.opportunities[0]
    assert proposal.native_object == NativeObjectReference(
        "video_lab", "video", "video-1"
    )
    assert proposal.evidence[0]["duration_seconds"] == 913
    assert proposal.suggested_actions[0].action_type == "comment"
    assert not hasattr(video, "text")
    assert not hasattr(channel, "username")

    adapter.set_kol_status("channel-1", "active")
    assert adapter.list_kols(status="active")[0]["id"] == "channel-1"


def test_builtin_adapters_own_promotion_and_platform_local_opportunity_scoring(
    tmp_path: Path,
) -> None:
    settings = SimpleNamespace(
        signal_opportunity_ttl_hours=24,
        auto_publish_score=80.0,
    )
    captured_at = datetime.now(timezone.utc).isoformat()

    x_adapter = XAutomationAdapter(
        settings, repository=XRepository(tmp_path / "x.db")
    )
    x_account = XAccount(
        external_id="x-creator",
        username="alice",
        followers_count=100_000,
        captured_at=captured_at,
    )
    tweets = tuple(
        XTweet(
            external_id=f"tweet-{index}",
            author_external_id=x_account.external_id,
            author_username=x_account.username,
            text="RWA adoption signal",
            created_at=captured_at,
            captured_at=f"{captured_at}-{index}",
            metrics=XTweetMetrics(likes=1_000, reposts=100, replies=20),
        )
        for index in range(3)
    )
    x_adapter.add_seed("seed")
    x_adapter.project_discovery(
        (
            XAccountRelation(
                source_external_id="seed",
                source_status="seed",
                target=x_account,
            ),
            *tweets,
        ),
        payload={},
    )
    x_signals = x_adapter.project_signals(
        (
            tweets[0],
            XTrend(name="RWA", rank=1, post_count=100_000),
        ),
        payload={},
    )

    assert x_adapter.list_kols(status="active")[0]["id"] == "x-creator"
    assert {item.native_object.object_type for item in x_signals.opportunities} == {
        "tweet",
        "trend",
    }
    x_trend = next(
        item for item in x_signals.opportunities if item.native_object.object_type == "trend"
    )
    assert x_trend.suggested_actions[0].action_type == "owned_publish"
    assert x_trend.expires_at is not None

    xhs_adapter = XiaohongshuAutomationAdapter(
        settings, repository=XiaohongshuRepository(tmp_path / "xhs.db")
    )
    xhs_user = XiaohongshuUser(
        external_id="xhs-creator",
        nickname="小爱",
        followers_count=100_000,
        captured_at=captured_at,
    )
    notes = tuple(
        XiaohongshuNote(
            external_id=f"note-{index}",
            author_external_id=xhs_user.external_id,
            author_nickname=xhs_user.nickname,
            title="RWA 观察",
            published_at=captured_at,
            captured_at=f"{captured_at}-{index}",
            metrics=XiaohongshuNoteMetrics(likes=800, comments=60, collects=200),
        )
        for index in range(3)
    )
    xhs_adapter.add_seed("seed-user")
    xhs_adapter.repository.upsert_user(xhs_user)
    xhs_adapter.repository.record_evidence(
        target_user_id=xhs_user.external_id,
        source_user_id="seed-user",
        relation_type="seed_relation",
        evidence="curated seed relationship",
        weight=0.20,
    )
    xhs_adapter.project_discovery(
        (xhs_user, *notes), payload={}
    )
    xhs_signals = xhs_adapter.project_signals(
        (
            notes[0],
            XiaohongshuTrend(name="RWA", rank=1, note_count=100_000),
        ),
        payload={},
    )

    assert xhs_adapter.list_kols(status="active")[0]["id"] == "xhs-creator"
    note_opportunity = next(
        item
        for item in xhs_signals.opportunities
        if item.native_object.object_type == "note"
    )
    assert note_opportunity.payload["note_type"] == "unknown"
    trend_opportunity = next(
        item
        for item in xhs_signals.opportunities
        if item.native_object.object_type == "trend"
    )
    assert trend_opportunity.suggested_actions == ()
    assert trend_opportunity.expires_at is not None


def test_x_trend_tweet_author_is_persisted_without_entering_kol_lifecycle(
    tmp_path: Path,
) -> None:
    repository = XRepository(tmp_path / "x-trend-authors.db")
    adapter = XAutomationAdapter(
        SimpleNamespace(review_queue_enabled=False, inactive_pause_days=90),
        repository=repository,
    )
    adapter.project_discovery(
        (
            XTrend(name="Trend One", rank=1),
            XTrendTweet(
                trend_name="Trend One",
                trend_rank=1,
                tweet=XTweet(
                    external_id="trend-tweet-1",
                    author_external_id="trend-author-1",
                    author_username="trend_author",
                    text="A concrete trend tweet",
                ),
            ),
        ),
        payload={},
    )

    assert repository.list_kols() == []
    assert repository.list_recent_trend_tweets()[0]["id"] == "trend-tweet-1"


def test_unresolved_xiaohongshu_author_is_not_promoted_or_scanned(
    tmp_path: Path,
) -> None:
    adapter = XiaohongshuAutomationAdapter(
        SimpleNamespace(inactive_pause_days=90),
        repository=XiaohongshuRepository(tmp_path / "xhs-unresolved.db"),
    )
    adapter.project_discovery(
        (
            XiaohongshuNote(
                external_id="note-1",
                author_external_id="unresolved:author-hash",
                author_native_id_resolved=False,
                author_nickname="Alice",
                title="RWA research",
                published_at=datetime.now(timezone.utc).isoformat(),
            ),
        ),
        payload={},
    )

    kol = adapter.get_kol("unresolved:author-hash")
    assert kol is not None
    assert kol["status"] == "candidate"
    assert kol["score"] == 0
    assert kol["actionable"] is False
    assert kol["reasons"] == ["native account identifier is unavailable"]
    targets, cursor = adapter.list_scan_targets(
        statuses=("candidate",), after=None, limit=20
    )
    assert targets == []
    assert cursor is None
