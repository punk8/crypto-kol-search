from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3

import pytest

from kol_search.automation import (
    ActionStatus,
    ActionType,
    AutomationDatabase,
    AutomationStore,
    ChannelControlScope,
    CoreJobType,
    OpportunityStatus,
    PlatformConnectionStatus,
    PolicyOutcome,
    StateTransitionError,
)


def test_explicit_migrations_are_idempotent_and_coexist_with_legacy_tables(tmp_path: Path):
    path = tmp_path / "shared.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE jobs(id INTEGER PRIMARY KEY, legacy_value TEXT)")
    connection.commit()
    connection.close()

    database = AutomationDatabase(path)
    database.migrate()

    assert [item["version"] for item in database.applied_migrations()] == [
        1,
        2,
        3,
        4,
        5,
        6,
        7,
    ]
    with database.connection() as connection:
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    assert "jobs" in tables
    assert {
        "automation_jobs",
        "automation_opportunities",
        "automation_actions",
        "automation_channel_controls",
        "automation_worker_heartbeats",
    }.issubset(tables)


def test_worker_restart_replaces_heartbeat_start_time(tmp_path: Path) -> None:
    store = AutomationStore(tmp_path / "heartbeat.db")
    store.record_worker_heartbeat(
        "mac-worker",
        host_label="mac-mini",
        started_at="2026-07-21T10:00:00+00:00",
        now="2026-07-21T10:01:00+00:00",
    )
    store.record_worker_heartbeat(
        "mac-worker",
        host_label="mac-mini",
        started_at="2026-07-21T11:00:00+00:00",
        now="2026-07-21T11:01:00+00:00",
    )

    heartbeat = store.latest_worker_heartbeat()
    assert heartbeat is not None
    assert heartbeat["started_at"] == "2026-07-21T11:00:00+00:00"
    assert heartbeat["last_seen_at"] == "2026-07-21T11:01:00+00:00"


def test_shared_brand_config_and_per_account_quota(tmp_path: Path) -> None:
    store = AutomationStore(tmp_path / "brand-and-quota.db")
    connection_id = store.upsert_platform_connection(
        "x",
        connection_key="managed-1",
        status="connected",
        capabilities=["comment"],
    )
    store.save_brand_config(
        brand_name="Signal Labs",
        platform_handles={"x": "signal_labs"},
        forbidden_terms=["guaranteed"],
        approved_domains=["example.com"],
    )
    store.upsert_account_quota(
        connection_id,
        "comment",
        hourly_limit=2,
        daily_limit=7,
        cooldown_seconds=3600,
    )

    brand = store.get_brand_config()
    assert brand["platform_handles"] == {"x": "signal_labs"}
    assert brand["forbidden_terms"] == ["guaranteed"]
    assert brand["approved_domains"] == ["example.com"]
    quota = store.get_account_quota(connection_id, "comment")
    assert quota is not None
    assert (quota["hourly_limit"], quota["daily_limit"], quota["cooldown_seconds"]) == (
        2,
        7,
        3600,
    )


def test_unknown_or_failed_write_pauses_account_atomically_and_stale_write_is_not_retried(
    tmp_path: Path,
) -> None:
    store = AutomationStore(tmp_path / "write-recovery.db")
    connection_id = store.upsert_platform_connection(
        "x", status="connected", capabilities=["comment"]
    )
    opportunity_id = store.upsert_opportunity(
        "x", "reply", "tweet", "target-1", score=90, priority=90
    )
    action_id = store.create_action(
        "x",
        "comment",
        "tweet",
        "target-1",
        opportunity_id=opportunity_id,
        connection_id=connection_id,
        draft="draft",
        initial_status="scheduled",
        idempotency_key="write-1",
        now="2026-07-21T00:00:00+00:00",
    )
    assert store.claim_next_action(
        "worker", now="2026-07-21T00:01:00+00:00"
    )["id"] == action_id
    assert store.recover_stale_actions(
        before="2026-07-21T00:10:00+00:00",
        now="2026-07-21T00:11:00+00:00",
    ) == 1
    assert store.get_action(action_id)["status"] == "confirmation_required"
    assert store.claim_next_action("worker-2", now="2026-07-21T00:12:00+00:00") is None

    second_id = store.create_action(
        "x",
        "comment",
        "tweet",
        "target-2",
        connection_id=connection_id,
        draft="draft",
        initial_status="scheduled",
        idempotency_key="write-2",
        now="2026-07-21T00:20:00+00:00",
    )
    # Clear the expected recovery pause before claiming the second independent action.
    store.set_kill_switch(
        "account",
        False,
        platform_id="x",
        connection_id=connection_id,
        updated_by="admin",
        now="2026-07-21T00:20:30+00:00",
    )
    assert store.claim_next_action(
        "worker", now="2026-07-21T00:21:00+00:00"
    )["id"] == second_id
    store.record_action_result(
        second_id,
        success=True,
        confirmed=False,
        error="receipt uncertain",
        pause_connection=True,
        now="2026-07-21T00:22:00+00:00",
    )
    assert store.get_action(second_id)["status"] == "confirmation_required"
    state = store.get_effective_channel_state("x", connection_id=connection_id)
    assert state["allowed"] is False


def test_stale_running_job_fails_without_being_requeued(tmp_path: Path) -> None:
    store = AutomationStore(tmp_path / "stale-job.db")
    job_id = store.create_job(
        "dispatch_actions",
        "x",
        now="2026-07-21T00:00:00+00:00",
    )
    claimed = store.claim_next_job(
        "worker", now="2026-07-21T00:01:00+00:00"
    )
    assert claimed is not None and claimed["id"] == job_id

    assert store.recover_stale_jobs(
        before="2026-07-21T00:10:00+00:00",
        now="2026-07-21T00:11:00+00:00",
    ) == 1

    recovered = store.get_job(job_id)
    assert recovered is not None
    assert recovered["status"] == "failed"
    assert "inspect platform state" in recovered["error"]
    assert store.claim_next_job("worker-2", now="2026-07-21T00:12:00+00:00") is None


def test_connection_health_and_job_claim_are_transaction_safe(tmp_path: Path):
    store = AutomationStore(tmp_path / "automation.db")
    connection_id = store.upsert_platform_connection(
        "x",
        display_name="Main X account",
        status=PlatformConnectionStatus.CONNECTED,
        capabilities=["comment", "content_search"],
    )
    job_id = store.create_job(
        CoreJobType.SIGNAL_REFRESH,
        "x",
        connection_id=connection_id,
        priority=90,
        payload={"cursor": "123"},
        idempotency_key="x:signal:2026-07-21T00",
    )
    assert (
        store.create_job(
            CoreJobType.SIGNAL_REFRESH,
            "x",
            connection_id=connection_id,
            idempotency_key="x:signal:2026-07-21T00",
        )
        == job_id
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        claims = list(executor.map(store.claim_next_job, ("worker-a", "worker-b")))

    claimed = [item for item in claims if item is not None]
    assert len(claimed) == 1
    assert claimed[0]["id"] == job_id
    assert claimed[0]["payload"] == {"cursor": "123"}
    assert claimed[0]["attempts"] == 1

    store.update_job_progress(job_id, progress=70, phase="ranking")
    store.complete_job(job_id, result={"opportunities": 3})
    completed = store.get_job(job_id)
    assert completed is not None
    assert completed["status"] == "succeeded"
    assert completed["progress"] == 100
    assert completed["result"] == {"opportunities": 3}
    with pytest.raises(StateTransitionError):
        store.fail_job(job_id, "late failure")

    store.record_connection_health(
        connection_id, PlatformConnectionStatus.DEGRADED, error="rate limited"
    )
    degraded = store.get_platform_connection(connection_id)
    assert degraded is not None
    assert degraded["consecutive_failures"] == 1
    store.record_connection_health(connection_id, PlatformConnectionStatus.CONNECTED)
    assert store.get_platform_connection(connection_id)["consecutive_failures"] == 0

    store.record_connection_health(
        connection_id, PlatformConnectionStatus.DISCONNECTED, error="login expired"
    )
    effective = store.get_effective_channel_state(
        "x", connection_id=connection_id, action_type=ActionType.COMMENT
    )
    assert effective["allowed"] is False
    assert effective["blocked_by"][0]["source"] == "connection_health"


def test_opportunity_action_and_policy_lifecycle_is_atomic(tmp_path: Path):
    store = AutomationStore(tmp_path / "automation.db")
    connection_id = store.upsert_platform_connection(
        "x", status=PlatformConnectionStatus.CONNECTED
    )
    opportunity_id = store.upsert_opportunity(
        "x",
        "reply",
        "tweet",
        "tweet-1",
        priority=85,
        score=91.5,
        evidence=[{"source": "seed_relation", "account_id": "kol-1"}],
        payload={"draft": "Useful context"},
    )
    action_id = store.create_action(
        "x",
        ActionType.COMMENT,
        "tweet",
        "tweet-1",
        opportunity_id=opportunity_id,
        connection_id=connection_id,
        draft="Useful context",
        initial_status=ActionStatus.NEEDS_REVIEW,
        idempotency_key="comment:x:tweet-1:brand-v1",
    )

    assert store.get_opportunity(opportunity_id)["status"] == OpportunityStatus.PLANNED
    assert (
        store.create_action(
            "x",
            ActionType.COMMENT,
            "tweet",
            "tweet-1",
            opportunity_id=opportunity_id,
            connection_id=connection_id,
            idempotency_key="comment:x:tweet-1:brand-v1",
        )
        == action_id
    )
    decision_id = store.record_policy_decision(
        PolicyOutcome.NEEDS_REVIEW,
        "2026-07-v1",
        action_id=action_id,
        score=91.5,
        rules=[{"rule": "initial_review", "passed": False}],
        explanation="First send requires approval",
    )
    assert decision_id > 0
    assert store.list_policy_decisions(action_id=action_id)[0]["rules"] == [
        {"rule": "initial_review", "passed": False}
    ]
    store.record_policy_decision(
        PolicyOutcome.AUTO_EXECUTE,
        "2026-07-v1",
        opportunity_id=opportunity_id,
        score=91.5,
    )
    assert store.list_policy_decisions(opportunity_id=opportunity_id)[0][
        "outcome"
    ] == PolicyOutcome.AUTO_EXECUTE

    store.transition_action(
        action_id,
        ActionStatus.SCHEDULED,
        expected_status=ActionStatus.NEEDS_REVIEW,
        approved_by="operator",
        final_text="Approved context",
    )
    claimed = store.claim_next_action("dispatcher")
    assert claimed is not None
    assert claimed["id"] == action_id
    assert claimed["status"] == ActionStatus.EXECUTING
    result = store.record_action_result(
        action_id,
        success=True,
        confirmed=True,
        external_id="reply-1",
        receipt_url="https://x.com/brand/status/reply-1",
        receipt={"ok": True},
    )
    assert result == ActionStatus.SUCCEEDED
    saved = store.get_action(action_id)
    assert saved["status"] == ActionStatus.SUCCEEDED
    assert saved["receipt"] == {"ok": True}
    with pytest.raises(StateTransitionError):
        store.transition_action(action_id, ActionStatus.SCHEDULED)

    events = store.list_audit_events(object_type="action", object_id=str(action_id))
    assert {event["event_type"] for event in events} >= {
        "action.created",
        "action.scheduled",
        "action.executing",
        "action.succeeded",
    }


def test_kill_switch_hierarchy_blocks_only_matching_action_channels(tmp_path: Path):
    store = AutomationStore(tmp_path / "automation.db")
    x_connection = store.upsert_platform_connection(
        "x", status=PlatformConnectionStatus.CONNECTED
    )
    red_connection = store.upsert_platform_connection(
        "xiaohongshu", status=PlatformConnectionStatus.CONNECTED
    )

    def scheduled_action(platform_id: str, connection_id: int, suffix: str) -> int:
        return store.create_action(
            platform_id,
            ActionType.COMMENT,
            "content",
            suffix,
            connection_id=connection_id,
            initial_status=ActionStatus.SCHEDULED,
            idempotency_key=f"{platform_id}:comment:{suffix}",
        )

    x_action = scheduled_action("x", x_connection, "x-1")
    red_action = scheduled_action("xiaohongshu", red_connection, "red-1")
    store.set_kill_switch(
        ChannelControlScope.PLATFORM,
        True,
        platform_id="x",
        reason="login challenge",
        updated_by="health-monitor",
    )

    x_state = store.get_effective_channel_state(
        "x", connection_id=x_connection, action_type=ActionType.COMMENT
    )
    assert x_state["allowed"] is False
    assert x_state["blocked_by"][0]["control_key"] == "platform:x"
    claimed = store.claim_next_action("dispatcher")
    assert claimed is not None and claimed["id"] == red_action
    assert store.get_action(x_action)["status"] == ActionStatus.SCHEDULED

    store.set_kill_switch(
        ChannelControlScope.PLATFORM,
        False,
        platform_id="x",
        updated_by="operator",
    )
    assert store.claim_next_action("dispatcher")["id"] == x_action

    store.set_kill_switch(
        ChannelControlScope.ACCOUNT,
        True,
        platform_id="x",
        connection_id=x_connection,
        reason="account cooldown",
        updated_by="operator",
    )
    assert (
        store.get_effective_channel_state(
            "x", connection_id=x_connection, action_type=ActionType.DM
        )["allowed"]
        is False
    )
    assert store.get_effective_channel_state(
        "xiaohongshu", connection_id=red_connection, action_type=ActionType.DM
    )["allowed"]
    store.set_kill_switch(
        ChannelControlScope.ACCOUNT,
        False,
        platform_id="x",
        connection_id=x_connection,
        updated_by="operator",
    )

    store.set_kill_switch(
        ChannelControlScope.ACTION_TYPE,
        True,
        action_type=ActionType.DM,
        reason="pause all direct messages",
        updated_by="operator",
    )
    assert (
        store.get_effective_channel_state(
            "xiaohongshu", connection_id=red_connection, action_type=ActionType.DM
        )["allowed"]
        is False
    )
    assert store.get_effective_channel_state(
        "xiaohongshu", connection_id=red_connection, action_type=ActionType.COMMENT
    )["allowed"]

    store.set_kill_switch(
        ChannelControlScope.GLOBAL,
        True,
        reason="emergency stop",
        updated_by="operator",
    )
    assert store.get_global_summary()["global_kill_switch"] is True
    assert store.get_effective_channel_state("xiaohongshu")["allowed"] is False


def test_idempotency_and_cooldown_queries_cover_more_than_one_thousand_actions(
    tmp_path: Path,
) -> None:
    store = AutomationStore(tmp_path / "long-history.db")
    connection_id = store.upsert_platform_connection("x", status="connected")
    opportunity_id = store.upsert_opportunity(
        "x",
        "reply",
        "tweet",
        "target-latest",
        payload={"target_author_id": "author-latest"},
    )
    timestamp = "2026-07-21T12:00:00+00:00"
    with store.database.transaction(immediate=True) as connection:
        connection.executemany(
            """
            INSERT INTO automation_actions(
                platform_id, action_type, native_object_type, native_object_id,
                status, draft, idempotency_key, created_at, updated_at
            ) VALUES('x', 'comment', 'tweet', ?, 'cancelled', '', ?, ?, ?)
            """,
            [
                (f"filler-{index}", f"filler-key-{index}", timestamp, timestamp)
                for index in range(1_001)
            ],
        )
        connection.execute(
            """
            INSERT INTO automation_actions(
                opportunity_id, platform_id, connection_id, action_type,
                native_object_type, native_object_id, status, draft,
                idempotency_key, finished_at, created_at, updated_at
            ) VALUES(?, 'x', ?, 'comment', 'tweet', 'target-latest',
                'succeeded', 'sent once', 'latest-target-key', ?, ?, ?)
            """,
            (opportunity_id, connection_id, timestamp, timestamp, timestamp),
        )

    assert store.has_existing_action(
        "x", "comment", "target-latest", draft="different draft"
    )
    assert store.has_recent_author_action(
        "x",
        connection_id,
        "author-latest",
        since="2026-07-21T00:00:00+00:00",
    )


def test_unconfirmed_write_is_never_requeued_and_platform_summaries_stay_separate(
    tmp_path: Path,
):
    store = AutomationStore(tmp_path / "automation.db")
    x_connection = store.upsert_platform_connection(
        "x", status=PlatformConnectionStatus.CONNECTED
    )
    store.upsert_platform_connection(
        "xiaohongshu", status=PlatformConnectionStatus.DISCONNECTED
    )
    for platform_id in ("x", "xiaohongshu"):
        store.upsert_opportunity(
            platform_id,
            "reply",
            "content",
            "same-native-id",
            score=82,
        )
    action_id = store.create_action(
        "x",
        ActionType.COMMENT,
        "tweet",
        "tweet-2",
        connection_id=x_connection,
        initial_status=ActionStatus.SCHEDULED,
        idempotency_key="x:comment:tweet-2",
    )
    assert store.claim_next_action("dispatcher")["id"] == action_id
    assert (
        store.record_action_result(
            action_id,
            success=True,
            confirmed=False,
            receipt={"provider_status": "unknown"},
        )
        == ActionStatus.CONFIRMATION_REQUIRED
    )
    assert store.claim_next_action("dispatcher") is None
    assert store.get_action(action_id)["status"] == ActionStatus.CONFIRMATION_REQUIRED

    summaries = {item["platform_id"]: item for item in store.get_platform_summaries()}
    assert summaries["x"]["open_opportunity_count"] == 1
    assert summaries["xiaohongshu"]["open_opportunity_count"] == 1
    assert summaries["x"]["exception_action_count"] == 1
    assert summaries["xiaohongshu"]["exception_action_count"] == 0
    assert summaries["xiaohongshu"]["unhealthy_connection_count"] == 1


def test_dismissed_or_expired_opportunity_cancels_pending_actions(tmp_path: Path) -> None:
    store = AutomationStore(tmp_path / "opportunity-cascade.db")
    connection_id = store.upsert_platform_connection("x", status="connected")

    dismissed_id = store.upsert_opportunity(
        "x", "reply", "tweet", "dismiss-me", expires_at="2026-07-22T00:00:00+00:00"
    )
    dismissed_action = store.create_action(
        "x",
        "comment",
        "tweet",
        "dismiss-me",
        opportunity_id=dismissed_id,
        connection_id=connection_id,
        initial_status="scheduled",
        idempotency_key="dismiss-action",
    )
    store.transition_opportunity(dismissed_id, "dismissed")
    assert store.get_action(dismissed_action)["status"] == "cancelled"
    assert store.get_action(dismissed_action)["error"] == "opportunity dismissed"

    expired_id = store.upsert_opportunity(
        "x", "reply", "tweet", "expire-me", expires_at="2026-07-20T00:00:00+00:00"
    )
    expired_action = store.create_action(
        "x",
        "comment",
        "tweet",
        "expire-me",
        opportunity_id=expired_id,
        connection_id=connection_id,
        initial_status="needs_review",
        idempotency_key="expire-action",
    )
    assert store.expire_opportunities(now="2026-07-21T00:00:00+00:00") == 1
    assert store.get_opportunity(expired_id)["status"] == "expired"
    assert store.get_action(expired_action)["status"] == "cancelled"

    # A platform may observe the same native signal again after it expired.
    # A fresh future TTL reopens it; an explicit human dismissal remains final.
    reopened_id = store.upsert_opportunity(
        "x",
        "reply",
        "tweet",
        "expire-me",
        expires_at="2026-07-23T00:00:00+00:00",
        now="2026-07-21T01:00:00+00:00",
    )
    assert reopened_id == expired_id
    assert store.get_opportunity(expired_id)["status"] == "open"
    store.upsert_opportunity(
        "x",
        "reply",
        "tweet",
        "dismiss-me",
        expires_at="2026-07-23T00:00:00+00:00",
        now="2026-07-21T01:00:00+00:00",
    )
    assert store.get_opportunity(dismissed_id)["status"] == "dismissed"
    events = store.list_audit_events(object_type="action")
    assert sum(row["event_type"] == "action.cancelled" for row in events) == 2
