from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import time

from pydantic import BaseModel
import pytest

from kol_search.automation import ActionStatus, AutomationStore, CoreJobType
from kol_search.automation.service import (
    AutomationWorker,
    PlatformAutomationScheduler,
    PlatformAutomationService,
)
from kol_search.discovery.promotion import KolStatus
from kol_search.platform_modules.adapter_utils import build_opportunity_task
from kol_search.platform_modules.x_adapter import XAutomationAdapter
from kol_search.platform_modules.xiaohongshu_adapter import XiaohongshuAutomationAdapter
from kol_search.platforms import (
    ActionExecutionResult,
    PlatformCapability,
    NativeObjectReference,
    PlatformAutomationAdapter,
    PlatformManifest,
    PlatformOpportunityProposal,
    PlatformProjectionResult,
    PlatformPlugin,
    PlatformRegistry,
    PlatformTaskName,
    PlatformTaskResult,
)
from kol_search.platforms.x import XAccount, XAccountRelation, XTweet, XTweetMetrics
from kol_search.platforms.xiaohongshu import XiaohongshuNote
from kol_search.settings import Settings


def _build_x_dm_service(
    tmp_path: Path,
) -> tuple[
    PlatformAutomationService,
    AutomationStore,
    XAutomationAdapter,
]:
    path = tmp_path / "dm-automation.db"
    settings = Settings(
        _env_file=None,
        KOL_DB_PATH=str(path),
        KOL_AUTO_EXECUTION_ENABLED=True,
        KOL_LIVE_WRITE_ENABLED=True,
        KOL_AUTO_DM_FOLLOWUP_SCORE=80,
    )
    adapter = XAutomationAdapter(settings)
    adapter.repository.upsert_account(
        XAccount(
            external_id="creator-1",
            username="alice",
            display_name="Alice",
            followers_count=1_000_000,
            source_provider="test",
        )
    )
    adapter.repository.set_kol_status(
        "creator-1",
        KolStatus.ACTIVE,
        score=0.95,
        reasons=("test active KOL",),
    )
    registry = PlatformRegistry(
        [
            PlatformPlugin(
                manifest=PlatformManifest(
                    platform_id="x",
                    name="X",
                    version="test",
                    capabilities=frozenset({PlatformCapability.DM}),
                ),
                automation_adapter=adapter,
            )
        ]
    )
    automation = AutomationStore(path)
    service = PlatformAutomationService(automation, registry, settings)
    service.initialize_connections()
    service.register_connection(
        "x",
        connection_key="brand-browser",
        display_name="Brand",
        capabilities=[PlatformCapability.DM],
        metadata={
            "kind": "managed_account",
            "external_account_id": "brand-1",
            "username": "brand",
            "browser_profile": "test-profile",
        },
    )
    return service, automation, adapter


def test_multiple_writer_connections_are_balanced_by_recent_usage(tmp_path: Path) -> None:
    path = tmp_path / "multiple-writers.db"
    settings = Settings(_env_file=None, KOL_DB_PATH=str(path))
    registry = PlatformRegistry(
        [
            PlatformPlugin(
                manifest=PlatformManifest(
                    platform_id="x",
                    name="X",
                    version="test",
                    capabilities=frozenset({PlatformCapability.COMMENT}),
                )
            )
        ]
    )
    automation = AutomationStore(path)
    service = PlatformAutomationService(automation, registry, settings)
    service.initialize_connections()
    first_id = service.register_connection(
        "x",
        connection_key="browser-one",
        display_name="Browser One",
        capabilities=[PlatformCapability.COMMENT],
        metadata={"kind": "managed_account", "is_default": True},
    )
    second_id = service.register_connection(
        "x",
        connection_key="browser-two",
        display_name="Browser Two",
        capabilities=[PlatformCapability.COMMENT],
        metadata={"kind": "managed_account"},
    )

    assert service._select_connection("x", "comment")["id"] == first_id
    automation.create_action(
        "x",
        "comment",
        "tweet",
        "tweet-balanced-1",
        connection_id=first_id,
        draft="Useful context",
        initial_status="scheduled",
        idempotency_key="balanced-comment-1",
    )
    assert service._select_connection("x", "comment")["id"] == second_id


def test_registered_platform_discovery_projects_native_data_and_plans_review(
    tmp_path: Path,
) -> None:
    path = tmp_path / "automation.db"
    settings = Settings(
        _env_file=None,
        KOL_DB_PATH=str(path),
        TWITTER_BACKEND="mock",
        KOL_ENABLE_MOCK_BACKEND=True,
        KOL_AUTO_EXECUTION_ENABLED=False,
        KOL_LIVE_WRITE_ENABLED=False,
    )
    now = datetime.now(timezone.utc).isoformat()
    account = XAccount(
        external_id="creator-1",
        username="alice",
        followers_count=100_000,
        source_provider="test",
        captured_at=now,
    )
    tweet = XTweet(
        external_id="tweet-1",
        author_external_id="creator-1",
        author_username="alice",
        text="RWA adoption is accelerating",
        created_at=now,
        captured_at=now,
        source_provider="test",
        url="https://x.com/alice/status/1",
        metrics=XTweetMetrics(likes=1_000, reposts=100, replies=20),
    )

    def discover(_context):  # noqa: ANN001, ANN202
        return PlatformTaskResult.completed((account, tweet))

    adapter = XAutomationAdapter(settings)
    registry = PlatformRegistry(
        [
            PlatformPlugin(
                manifest=PlatformManifest(
                    platform_id="x",
                    name="X",
                    version="test",
                    capabilities=frozenset(
                        {PlatformCapability.CONTENT_SEARCH, PlatformCapability.COMMENT}
                    ),
                ),
                handlers={
                    PlatformTaskName.DISCOVER: discover,
                    PlatformTaskName.SCAN_SIGNALS: discover,
                    PlatformTaskName.BUILD_OPPORTUNITIES: lambda context: (
                        build_opportunity_task(adapter, context)
                    ),
                },
                automation_adapter=adapter,
            )
        ]
    )
    automation = AutomationStore(path)
    service = PlatformAutomationService(automation, registry, settings)
    service.initialize_connections()
    service.register_connection(
        "x",
        connection_key="brand-browser",
        display_name="Brand",
        capabilities=[PlatformCapability.COMMENT],
        metadata={
            "kind": "managed_account",
            "external_account_id": "brand-1",
            "username": "brand",
            "browser_profile": "test-profile",
        },
    )

    job_id = service.enqueue_discovery("x", query="RWA")
    service.run_job(automation.claim_next_job("test-worker"))  # type: ignore[arg-type]
    assert automation.get_job(job_id)["status"] == "succeeded"
    assert service.native_summary("x")["content"] == 1

    signal_id = service.enqueue_signal_refresh("x", manual=True)
    service.run_job(automation.claim_next_job("test-worker"))  # type: ignore[arg-type]
    assert automation.get_job(signal_id)["status"] == "succeeded"
    opportunity = automation.list_opportunities(platform_id="x")[0]
    assert opportunity["native_object_type"] == "tweet"
    action = automation.list_actions(platform_id="x")[0]
    assert action["status"] == "needs_review"
    service.approve_action(int(action["id"]), actor="reviewer")
    assert automation.get_action(int(action["id"]))["status"] == "scheduled"
    assert service.dispatch_action_once(platform_id="x") is False
    assert automation.get_action(int(action["id"]))["status"] == "scheduled"


def test_manual_approval_executes_with_auto_policy_disabled_and_reader_is_read_only(
    tmp_path: Path,
) -> None:
    path = tmp_path / "manual-approved.db"
    settings = Settings(
        _env_file=None,
        KOL_DB_PATH=str(path),
        TWITTER_BACKEND="mock",
        KOL_ENABLE_MOCK_BACKEND=True,
        KOL_AUTO_EXECUTION_ENABLED=False,
        KOL_LIVE_WRITE_ENABLED=True,
    )
    def no_op(_context):  # noqa: ANN001, ANN202
        return PlatformTaskResult.completed()

    def execute(_context):  # noqa: ANN001, ANN202
        return ActionExecutionResult(
            success=True,
            confirmed=True,
            external_id="reply-1",
            receipt_url="https://x.com/brand/status/reply-1",
        )

    registry = PlatformRegistry(
        [
            PlatformPlugin(
                manifest=PlatformManifest(
                    platform_id="x",
                    name="X",
                    version="test",
                    capabilities=frozenset(
                        {PlatformCapability.CONTENT_SEARCH, PlatformCapability.COMMENT}
                    ),
                ),
                handlers={
                    PlatformTaskName.DISCOVER: no_op,
                    PlatformTaskName.SCAN_SIGNALS: no_op,
                    PlatformTaskName.EXECUTE_ACTION: execute,
                },
            )
        ]
    )
    automation = AutomationStore(path)
    service = PlatformAutomationService(automation, registry, settings)
    service.initialize_connections()
    service.register_connection(
        "x",
        connection_key="brand-browser",
        display_name="Brand",
        capabilities=[PlatformCapability.COMMENT],
        metadata={
            "kind": "managed_account",
            "external_account_id": "brand-1",
            "username": "brand",
            "browser_profile": "test-profile",
        },
    )
    connections = automation.list_platform_connections(platform_id="x")
    reader = next(row for row in connections if row["connection_key"] == "reader")
    managed = next(row for row in connections if row["connection_key"] == "brand-browser")
    assert reader["capabilities"] == ["content_search"]
    assert managed["capabilities"] == ["comment"]

    opportunity_id = automation.upsert_opportunity(
        "x",
        "reply",
        "tweet",
        "tweet-1",
        priority=90,
        score=90,
        title="Reply target",
        payload={
            "target_author_id": "creator-1",
            "target_url": "https://x.com/creator/status/1",
        },
    )
    action_id = automation.create_action(
        "x",
        "comment",
        "tweet",
        "tweet-1",
        opportunity_id=opportunity_id,
        connection_id=managed["id"],
        draft="Useful framing.",
        initial_status="needs_review",
        idempotency_key="manual-comment-1",
    )
    service.approve_action(action_id, actor="admin")
    assert automation.get_action(action_id)["status"] == "scheduled"
    assert service.dispatch_action_once(platform_id="x") is True
    assert automation.get_action(action_id)["status"] == "succeeded"


def test_forged_conversation_flag_cannot_bypass_cold_dm_review(
    tmp_path: Path,
) -> None:
    service, automation, _adapter = _build_x_dm_service(tmp_path)

    opportunity_id = service.propose_dm(
        "x",
        "creator-1",
        "Would you like to compare notes?",
        conversation_id="forged-conversation-id",
        has_valid_conversation=True,
    )

    opportunity = automation.get_opportunity(opportunity_id)
    assert opportunity is not None
    assert opportunity["score"] == 95
    assert opportunity["native_object_type"] == "account"
    assert opportunity["native_object_id"] == "creator-1"
    assert opportunity["payload"]["conversation_id"] is None
    assert opportunity["payload"]["has_valid_conversation"] is False
    assert opportunity["payload"]["is_initial_dm"] is True
    actions = automation.list_actions(platform_id="x")
    assert len(actions) == 1
    assert actions[0]["status"] == ActionStatus.NEEDS_REVIEW.value


def test_dm_rejects_xiaohongshu_author_without_native_account_id(
    tmp_path: Path,
) -> None:
    path = tmp_path / "unresolved-xhs-dm.db"
    settings = Settings(_env_file=None, KOL_DB_PATH=str(path))
    adapter = XiaohongshuAutomationAdapter(settings)
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
    registry = PlatformRegistry(
        [
            PlatformPlugin(
                manifest=PlatformManifest(
                    platform_id="xiaohongshu",
                    name="Xiaohongshu",
                    version="test",
                    capabilities=frozenset({PlatformCapability.DM}),
                ),
                automation_adapter=adapter,
            )
        ]
    )
    service = PlatformAutomationService(AutomationStore(path), registry, settings)

    with pytest.raises(
        ValueError, match="Platform-native account identifier is unavailable"
    ):
        service.propose_dm(
            "xiaohongshu", "unresolved:author-hash", "Would you like to talk?"
        )


def test_human_verified_conversation_allows_high_confidence_followup_dm(
    tmp_path: Path,
) -> None:
    service, automation, _adapter = _build_x_dm_service(tmp_path)
    service.set_conversation_validity(
        "x",
        "creator-1",
        "conversation-1",
        valid=True,
        evidence="Reviewed an inbound reply in the native conversation",
        actor="reviewer",
    )

    opportunity_id = service.propose_dm(
        "x",
        "creator-1",
        "Following up on your reply.",
        conversation_id="conversation-1",
        has_valid_conversation=True,
    )

    opportunity = automation.get_opportunity(opportunity_id)
    assert opportunity is not None
    assert opportunity["native_object_type"] == "conversation"
    assert opportunity["native_object_id"] == "conversation-1"
    assert opportunity["payload"]["has_valid_conversation"] is True
    action = automation.list_actions(platform_id="x")[0]
    assert action["status"] == ActionStatus.SCHEDULED.value
    state = automation.get_conversation_state("x", "conversation-1")
    assert state is not None
    assert state["evidence"]["kind"] == "human_verified"


def test_successful_outbound_dm_does_not_prove_an_inbound_conversation(
    tmp_path: Path,
) -> None:
    service, automation, _adapter = _build_x_dm_service(tmp_path)
    first_opportunity = service.propose_dm("x", "creator-1", "Cold outreach")
    first_action = automation.list_actions(platform_id="x")[0]
    service.approve_action(int(first_action["id"]), actor="reviewer")
    claimed = automation.claim_next_action("test-worker", platform_id="x")
    assert claimed is not None
    automation.record_action_result(
        int(first_action["id"]), success=True, confirmed=True, external_id="dm-1"
    )

    second_opportunity = service.propose_dm(
        "x",
        "creator-1",
        "Unverified follow-up",
        conversation_id="conversation-not-verified",
        has_valid_conversation=True,
    )

    assert second_opportunity == first_opportunity
    opportunity = automation.get_opportunity(second_opportunity)
    assert opportunity is not None
    assert opportunity["payload"]["has_valid_conversation"] is False
    assert opportunity["native_object_type"] == "account"


@pytest.mark.parametrize(
    ("success", "confirmed", "terminal_status"),
    [
        (False, False, ActionStatus.FAILED),
        (True, False, ActionStatus.CONFIRMATION_REQUIRED),
    ],
)
def test_uncertain_or_failed_write_blocks_changed_draft_retry_for_same_target(
    tmp_path: Path,
    success: bool,
    confirmed: bool,
    terminal_status: ActionStatus,
) -> None:
    service, automation, _adapter = _build_x_dm_service(tmp_path)
    opportunity_id = service.propose_dm(
        "x", "creator-1", "First outreach draft."
    )
    first_action = automation.list_actions(platform_id="x")[0]
    service.approve_action(int(first_action["id"]), actor="reviewer")
    claimed = automation.claim_next_action("test-worker", platform_id="x")
    assert claimed is not None
    assert int(claimed["id"]) == int(first_action["id"])
    automation.record_action_result(
        int(first_action["id"]),
        success=success,
        confirmed=confirmed,
        error=None if success else "platform write failed",
    )
    assert automation.get_action(int(first_action["id"]))["status"] == terminal_status.value

    retried_opportunity_id = service.propose_dm(
        "x", "creator-1", "A materially different outreach draft."
    )

    assert retried_opportunity_id == opportunity_id
    actions = automation.list_actions(platform_id="x")
    assert [int(action["id"]) for action in actions] == [int(first_action["id"])]
    decisions = automation.list_policy_decisions(
        opportunity_id=retried_opportunity_id
    )
    assert decisions[0]["outcome"] == "rejected"
    assert "duplicate target or idempotency key" in decisions[0]["rules"]


def test_relationship_evidence_persists_and_promotes_on_later_content_scan(
    tmp_path: Path,
) -> None:
    path = tmp_path / "relationship-promotion.db"
    settings = Settings(_env_file=None, KOL_DB_PATH=str(path))
    adapter = XAutomationAdapter(settings)
    adapter.add_seed("seed-account", "Seed Account")
    now = datetime.now(timezone.utc).isoformat()
    candidate = XAccount(
        external_id="candidate-1",
        username="candidate",
        followers_count=1_000_000,
        source_provider="test",
        captured_at=now,
    )
    relevant_tweet = XTweet(
        external_id="candidate-tweet-1",
        author_external_id="candidate-1",
        author_username="candidate",
        text="A relevant RWA research update",
        created_at=now,
        captured_at=now,
        source_provider="test",
    )

    def discover(context):  # noqa: ANN001, ANN202
        if context.payload.get("source") == "relations":
            return PlatformTaskResult.completed(
                (
                    XAccountRelation(
                        source_external_id="seed-account",
                        source_status=KolStatus.SEED.value,
                        target=candidate,
                    ),
                )
            )
        return PlatformTaskResult.completed((relevant_tweet,))

    registry = PlatformRegistry(
        [
            PlatformPlugin(
                manifest=PlatformManifest(
                    platform_id="x",
                    name="X",
                    version="test",
                    capabilities=frozenset(
                        {
                            PlatformCapability.ACCOUNT_SEARCH,
                            PlatformCapability.CONTENT_SEARCH,
                            PlatformCapability.RELATIONS,
                        }
                    ),
                ),
                handlers={PlatformTaskName.DISCOVER: discover},
                automation_adapter=adapter,
            )
        ]
    )
    automation = AutomationStore(path)
    service = PlatformAutomationService(automation, registry, settings)
    service.initialize_connections()

    scheduled_payload = service._discovery_payload("x", {})  # noqa: SLF001
    assert scheduled_payload["query"] == "crypto"
    assert scheduled_payload["seed_accounts"][0]["id"] == "seed-account"

    relation_job_id = service.enqueue_discovery(
        "x", source="relations", manual=True
    )
    service.run_job(automation.claim_next_job("test-worker"))  # type: ignore[arg-type]
    assert automation.get_job(relation_job_id)["status"] == "succeeded"
    first = next(row for row in adapter.list_kols() if row["id"] == "candidate-1")
    assert first["status"] == KolStatus.CANDIDATE.value
    assert adapter.repository.relationship_evidence_count("candidate-1") == 1

    content_job_id = service.enqueue_discovery(
        "x", query="RWA", source="content", manual=True
    )
    service.run_job(automation.claim_next_job("test-worker"))  # type: ignore[arg-type]
    assert automation.get_job(content_job_id)["status"] == "succeeded"
    promoted = next(row for row in adapter.list_kols() if row["id"] == "candidate-1")
    assert promoted["status"] == KolStatus.ACTIVE.value
    assert "relationship evidence passed" in " ".join(promoted["reasons"])


def test_video_style_platform_runs_through_core_without_x_or_xhs_branches(
    tmp_path: Path,
) -> None:
    path = tmp_path / "video-platform.db"
    settings = Settings(_env_file=None, KOL_DB_PATH=str(path))
    class Video(BaseModel):
        video_id: str
        channel_id: str
        title: str

    class VideoAdapter:
        platform_id = "video_lab"

        def project_discovery(self, items, *, payload):  # noqa: ANN001, ANN202
            del payload
            values = [item for item in items if isinstance(item, Video)]
            with sqlite3.connect(path) as connection:
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS video_lab_videos("
                    "video_id TEXT PRIMARY KEY, channel_id TEXT, title TEXT)"
                )
                connection.executemany(
                    "INSERT OR REPLACE INTO video_lab_videos VALUES(?, ?, ?)",
                    [(item.video_id, item.channel_id, item.title) for item in values],
                )
            return PlatformProjectionResult(native_counts={"videos": len(values)})

        def project_signals(self, items, *, payload):  # noqa: ANN001, ANN202
            projection = self.project_discovery(items, payload=payload)
            values = [item for item in items if isinstance(item, Video)]
            opportunities = tuple(
                PlatformOpportunityProposal(
                    opportunity_type="video_research",
                    native_object=NativeObjectReference(
                        "video_lab", "video", item.video_id
                    ),
                    priority=77,
                    score=77,
                    title=item.title,
                    evidence=({"kind": "platform_video"},),
                )
                for item in values
            )
            return PlatformProjectionResult(
                native_counts=projection.native_counts,
                opportunities=opportunities,
            )

        def list_kols(self, *, status="all", limit=100):  # noqa: ANN001, ANN202
            del status, limit
            return []

        def get_kol(self, native_id):  # noqa: ANN001, ANN202
            del native_id
            return None

        def list_scan_targets(
            self, *, statuses, after, limit  # noqa: ANN001, ANN202
        ):
            del statuses, after, limit
            return [], None

        def summary(self):  # noqa: ANN202
            with sqlite3.connect(path) as connection:
                count = connection.execute(
                    "SELECT COUNT(*) FROM video_lab_videos"
                ).fetchone()[0]
            return {"content": count}

        def set_kol_status(self, native_id, status):  # noqa: ANN001, ANN202
            raise KeyError((native_id, status))

        def add_seed(self, native_id, display_name=None):  # noqa: ANN001, ANN202
            raise KeyError((native_id, display_name))

    adapter = VideoAdapter()
    assert isinstance(adapter, PlatformAutomationAdapter)
    video = Video(video_id="v-1", channel_id="channel-1", title="RWA explained")

    def read(_context):  # noqa: ANN001, ANN202
        return PlatformTaskResult.completed((video,))

    registry = PlatformRegistry(
        [
            PlatformPlugin(
                manifest=PlatformManifest(
                    platform_id="video_lab",
                    name="Video Lab",
                    version="test",
                    capabilities=frozenset(
                        {PlatformCapability.CONTENT_SEARCH, PlatformCapability.ANALYTICS}
                    ),
                ),
                handlers={
                    PlatformTaskName.DISCOVER: read,
                    PlatformTaskName.SCAN_SIGNALS: read,
                    PlatformTaskName.BUILD_OPPORTUNITIES: lambda context: (
                        build_opportunity_task(adapter, context)
                    ),
                    PlatformTaskName.REFRESH_OUTCOMES: read,
                },
                native_models={"video": Video},
                automation_adapter=adapter,
            )
        ]
    )
    automation = AutomationStore(path)
    service = PlatformAutomationService(automation, registry, settings)
    service.initialize_connections()

    job_id = service.enqueue_signal_refresh("video_lab", manual=True)
    service.run_job(automation.claim_next_job("video-worker"))  # type: ignore[arg-type]
    assert automation.get_job(job_id)["status"] == "succeeded"
    opportunity = automation.list_opportunities(platform_id="video_lab")[0]
    assert opportunity["native_object_type"] == "video"
    assert opportunity["native_object_id"] == "v-1"
    assert service.native_summary("video_lab") == {"content": 1}


def test_worker_isolates_failed_platform_job_and_continues_other_platform(
    tmp_path: Path,
) -> None:
    path = tmp_path / "worker-failure-isolation.db"
    settings = Settings(_env_file=None, KOL_DB_PATH=str(path))
    class ProjectionAdapter:
        def __init__(self, platform_id: str) -> None:
            self.platform_id = platform_id

        def project_discovery(self, items, *, payload):  # noqa: ANN001, ANN202
            del payload
            return PlatformProjectionResult(native_counts={"items": len(tuple(items))})

        def project_signals(self, items, *, payload):  # noqa: ANN001, ANN202
            return self.project_discovery(items, payload=payload)

        def list_kols(self, *, status="all", limit=100):  # noqa: ANN001, ANN202
            del status, limit
            return []

        def get_kol(self, native_id):  # noqa: ANN001, ANN202
            del native_id
            return None

        def list_scan_targets(
            self, *, statuses, after, limit  # noqa: ANN001, ANN202
        ):
            del statuses, after, limit
            return [], None

        def summary(self):  # noqa: ANN202
            return {"items": 0}

        def set_kol_status(self, native_id, status):  # noqa: ANN001, ANN202
            raise KeyError((native_id, status))

        def add_seed(self, native_id, display_name=None):  # noqa: ANN001, ANN202
            raise KeyError((native_id, display_name))

    def broken_discovery(_context):  # noqa: ANN001, ANN202
        raise RuntimeError("broken provider is unavailable")

    def healthy_discovery(_context):  # noqa: ANN001, ANN202
        return PlatformTaskResult.completed((object(),))

    def plugin(platform_id, handler):  # noqa: ANN001, ANN202
        return PlatformPlugin(
            manifest=PlatformManifest(
                platform_id=platform_id,
                name=platform_id.title(),
                version="test",
                capabilities=frozenset({PlatformCapability.CONTENT_SEARCH}),
            ),
            handlers={PlatformTaskName.DISCOVER: handler},
            automation_adapter=ProjectionAdapter(platform_id),
        )

    registry = PlatformRegistry(
        [plugin("broken", broken_discovery), plugin("healthy", healthy_discovery)]
    )
    automation = AutomationStore(path)
    service = PlatformAutomationService(automation, registry, settings)
    service.initialize_connections()
    broken_job_id = service.enqueue_discovery("broken", query="RWA")
    healthy_job_id = service.enqueue_discovery("healthy", query="RWA")

    worker = AutomationWorker(automation, service)
    worker.start()
    deadline = time.monotonic() + 2
    try:
        while time.monotonic() < deadline:
            states = {
                broken_job_id: automation.get_job(broken_job_id)["status"],
                healthy_job_id: automation.get_job(healthy_job_id)["status"],
            }
            if states == {broken_job_id: "failed", healthy_job_id: "succeeded"}:
                break
            time.sleep(0.01)
    finally:
        worker.stop()

    assert automation.get_job(broken_job_id)["status"] == "failed"
    assert automation.get_job(broken_job_id)["error"] == "broken provider is unavailable"
    assert automation.get_job(healthy_job_id)["status"] == "succeeded"
    connections = {
        row["platform_id"]: row
        for row in automation.list_platform_connections()
        if row["connection_key"] == "reader"
    }
    assert connections["broken"]["status"] == "degraded"
    assert connections["healthy"]["status"] == "connected"


def test_successful_partial_read_preserves_warning_and_degrades_only_that_reader(
    tmp_path: Path,
) -> None:
    path = tmp_path / "partial-warning.db"
    settings = Settings(_env_file=None, KOL_DB_PATH=str(path))
    adapter = XAutomationAdapter(settings)

    def partial_discovery(_context):  # noqa: ANN001, ANN202
        return PlatformTaskResult.completed(
            warnings=("one relationship endpoint was unavailable",)
        )

    registry = PlatformRegistry(
        [
            PlatformPlugin(
                manifest=PlatformManifest(
                    platform_id="x",
                    name="X",
                    version="test",
                    capabilities=frozenset({PlatformCapability.CONTENT_SEARCH}),
                ),
                handlers={PlatformTaskName.DISCOVER: partial_discovery},
                automation_adapter=adapter,
            )
        ]
    )
    automation = AutomationStore(path)
    service = PlatformAutomationService(automation, registry, settings)
    service.initialize_connections()
    job_id = service.enqueue_discovery("x", query="RWA")

    result = service.run_job(automation.claim_next_job("warning-worker"))  # type: ignore[arg-type]

    assert result["warnings"] == ["one relationship endpoint was unavailable"]
    assert automation.get_job(job_id)["status"] == "succeeded"
    reader = next(
        row
        for row in automation.list_platform_connections(platform_id="x")
        if row["connection_key"] == "reader"
    )
    assert reader["status"] == "degraded"
    assert reader["last_health_error"] == "one relationship endpoint was unavailable"


def test_discovery_rotation_cursor_is_not_hidden_by_high_volume_dispatch_jobs(
    tmp_path: Path,
) -> None:
    path = tmp_path / "job-cursor-history.db"
    settings = Settings(_env_file=None, KOL_DB_PATH=str(path))
    automation = AutomationStore(path)
    service = PlatformAutomationService(
        automation,
        PlatformRegistry(),
        settings,
    )
    discovery_id = automation.create_job(
        "discovery_refresh",
        "x",
        idempotency_key="discovery-cursor-source",
    )
    discovery = automation.claim_next_job("discovery-worker")
    assert discovery is not None and discovery["id"] == discovery_id
    automation.complete_job(discovery_id, result={"target_cursor": "account-050"})

    manual_id = automation.create_job(
        "manual_discovery",
        "x",
        idempotency_key="manual-query-without-rotation-cursor",
    )
    manual = automation.claim_next_job("manual-worker")
    assert manual is not None and manual["id"] == manual_id
    automation.complete_job(manual_id, result={"accounts": 3})

    for index in range(150):
        job_id = automation.create_job(
            "dispatch_actions",
            "x",
            idempotency_key=f"dispatch-noise-{index}",
        )
        claimed = automation.claim_next_job("dispatch-worker")
        assert claimed is not None and claimed["id"] == job_id
        automation.complete_job(job_id, result={"actions_dispatched": 0})

    assert service._previous_job_result_value(  # noqa: SLF001 - cursor regression
        "x",
        {CoreJobType.DISCOVERY_REFRESH, CoreJobType.MANUAL_DISCOVERY},
        "target_cursor",
    ) == "account-050"


def test_scheduler_and_service_skip_action_dispatch_without_capability_or_handler(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "scheduler-capabilities.db"
    settings = Settings(_env_file=None, KOL_DB_PATH=str(path))
    def execute(_context):  # noqa: ANN001, ANN202
        return ActionExecutionResult(success=True, confirmed=True)

    registry = PlatformRegistry(
        [
            PlatformPlugin(
                manifest=PlatformManifest(
                    "handler_only",
                    "Handler Only",
                    "test",
                    capabilities=frozenset({PlatformCapability.CONTENT_SEARCH}),
                ),
                handlers={PlatformTaskName.EXECUTE_ACTION: execute},
            ),
            PlatformPlugin(
                manifest=PlatformManifest(
                    "capability_only",
                    "Capability Only",
                    "test",
                    capabilities=frozenset({PlatformCapability.COMMENT}),
                )
            ),
            PlatformPlugin(
                manifest=PlatformManifest(
                    "writable",
                    "Writable",
                    "test",
                    capabilities=frozenset({PlatformCapability.COMMENT}),
                ),
                handlers={PlatformTaskName.EXECUTE_ACTION: execute},
            ),
        ]
    )
    automation = AutomationStore(path)
    service = PlatformAutomationService(automation, registry, settings)
    worker = AutomationWorker(automation, service)
    scheduler = PlatformAutomationScheduler(service, worker, settings)
    monkeypatch.setattr(scheduler.scheduler, "start", lambda: None)

    scheduler.start()

    scheduled_ids = {job.id for job in scheduler.scheduler.get_jobs()}
    assert scheduled_ids == {"writable-action-dispatch"}
    with pytest.raises(ValueError, match="does not declare"):
        service.enqueue_action_dispatch("handler_only")
    with pytest.raises(ValueError, match="does not support action execution"):
        service.enqueue_action_dispatch("capability_only")
    assert service.enqueue_action_dispatch("writable") > 0
