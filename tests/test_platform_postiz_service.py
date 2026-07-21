from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

from kol_search.automation import AutomationStore
from kol_search.automation.service import PlatformAutomationService
from kol_search.platforms import (
    ActionExecutionResult,
    PlatformCapability,
    PlatformManifest,
    PlatformOutcomeUpdate,
    PlatformPlugin,
    PlatformRegistry,
    PlatformTaskName,
    PlatformTaskResult,
)
from kol_search.platforms.x import create_x_plugin
from kol_search.settings import Settings


class _FakeXClient:
    name = "test"

    def health_check(self):  # noqa: ANN201 - platform test double
        return SimpleNamespace(ready=True, account="reader", detail=None)

    def get_user_tweets(
        self,
        _account_id: str,
        _limit: int,
        *,
        username: str | None = None,
    ):  # noqa: ANN201
        del username
        return []

    def get_trends(self, _limit: int):  # noqa: ANN201
        return [
            {
                "name": "RWA",
                "rank": 1,
                "post_count": 1_000_000,
                "url": "https://x.com/explore/tabs/trending",
            }
        ]

    def close(self) -> None:
        return None


class _FakePostizPublisher:
    create_calls: list[dict[str, object]] = []
    recent_calls: list[dict[str, object]] = []

    def __init__(self, **_kwargs: object) -> None:
        return None

    def create_x_post(self, **kwargs: object):  # noqa: ANN201
        type(self).create_calls.append(kwargs)
        return [{"postId": "postiz-native-1"}]

    def recent_posts(self, **kwargs: object):  # noqa: ANN201
        type(self).recent_calls.append(kwargs)
        return [
            {
                "postId": "postiz-native-1",
                "content": "RWA: the useful question is which measurable signal confirms the trend next.",
                "integration": {"id": "integration-native-x"},
                "releaseURL": "https://x.com/brand/status/123",
            }
        ]

    def close(self) -> None:
        return None


def test_clean_core_x_postiz_flow_never_installs_legacy_schema(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "clean-platform.db"
    settings = Settings(
        _env_file=None,
        KOL_DB_PATH=str(path),
        KOL_AUTO_EXECUTION_ENABLED=True,
        KOL_LIVE_WRITE_ENABLED=True,
        KOL_AUTO_PUBLISH_SCORE=80,
        KOL_PUBLISH_WINDOWS="00:00-24:00",
        POSTIZ_API_KEY="test-only",
    )
    registry = PlatformRegistry(
        [create_x_plugin(settings, client_factory=_FakeXClient)]
    )
    automation = AutomationStore(path)
    service = PlatformAutomationService(automation, registry, settings)
    service.initialize_connections()
    service.register_connection(
        "x",
        connection_key="postiz-native",
        display_name="Brand X",
        capabilities=[PlatformCapability.OWNED_PUBLISH],
        metadata={
            "kind": "postiz",
            "integration_id": "integration-native-x",
            "is_default": True,
        },
    )
    _FakePostizPublisher.create_calls.clear()
    _FakePostizPublisher.recent_calls.clear()
    monkeypatch.setattr(
        "kol_search.platform_modules.x_postiz.XPostizPublisher",
        _FakePostizPublisher,
    )

    service.enqueue_signal_refresh("x", manual=True)
    signal_job = automation.claim_next_job("signal-test")
    assert signal_job is not None
    service.run_job(signal_job)
    action = automation.list_actions(platform_id="x")[0]
    assert action["status"] == "scheduled"
    assert action["payload"] == {}

    assert service.dispatch_action_once(platform_id="x") is True
    action = automation.get_action(int(action["id"]))
    assert action is not None
    assert action["status"] == "confirmation_required"
    assert action["external_id"] == "postiz-native-1"
    assert len(_FakePostizPublisher.create_calls) == 1

    service.enqueue_outcome_refresh("x")
    outcome_job = automation.claim_next_job("outcome-test")
    assert outcome_job is not None
    result = service.run_job(outcome_job)
    assert result["outcomes_updated"] == 1
    action = automation.get_action(int(action["id"]))
    assert action is not None
    assert action["status"] == "succeeded"
    assert action["receipt_url"] == "https://x.com/brand/status/123"
    assert len(_FakePostizPublisher.create_calls) == 1
    assert len(_FakePostizPublisher.recent_calls) == 1

    with sqlite3.connect(path) as connection:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    assert "runs" not in tables
    assert "owned_posts" not in tables
    assert "postiz_integrations" not in tables


def test_core_skips_still_unconfirmed_outcome_and_preserves_receipt(
    tmp_path: Path,
) -> None:
    path = tmp_path / "unresolved-outcome.db"
    settings = Settings(_env_file=None, KOL_DB_PATH=str(path))
    automation = AutomationStore(path)
    action_id = 0

    def refresh(_context):  # noqa: ANN001, ANN202 - contract test handler
        return PlatformTaskResult.completed(
            (
                PlatformOutcomeUpdate(
                    action_id=action_id,
                    result=ActionExecutionResult(
                        success=True,
                        confirmed=False,
                    ),
                ),
            )
        )

    registry = PlatformRegistry(
        [
            PlatformPlugin(
                manifest=PlatformManifest(
                    platform_id="video",
                    name="Video Test",
                    version="test",
                    capabilities=frozenset({PlatformCapability.COMMENT}),
                ),
                handlers={PlatformTaskName.REFRESH_OUTCOMES: refresh},
            )
        ]
    )
    service = PlatformAutomationService(automation, registry, settings)
    connection_id = service.register_connection(
        "video",
        connection_key="writer",
        display_name="Writer",
        capabilities=[PlatformCapability.COMMENT],
        metadata={"kind": "managed_account"},
    )
    opportunity_id = automation.upsert_opportunity(
        "video",
        "reply",
        "video",
        "video-1",
        priority=90,
        score=90,
    )
    action_id = automation.create_action(
        "video",
        "comment",
        "video",
        "video-1",
        opportunity_id=opportunity_id,
        connection_id=connection_id,
        draft="Useful signal.",
        initial_status="scheduled",
        idempotency_key="unresolved-video-action",
    )
    claimed = automation.claim_next_action("write-test")
    assert claimed is not None
    automation.record_action_result(
        action_id,
        success=True,
        confirmed=False,
        external_id="external-1",
        receipt={"original": True},
    )

    service.enqueue_outcome_refresh("video")
    job = automation.claim_next_job("outcome-test")
    assert job is not None
    result = service.run_job(job)

    assert result["outcomes_updated"] == 0
    assert result["warnings"] == [f"action {action_id} remains unconfirmed"]
    action = automation.get_action(action_id)
    assert action is not None
    assert action["status"] == "confirmation_required"
    assert action["external_id"] == "external-1"
    assert action["receipt"] == {"original": True}
