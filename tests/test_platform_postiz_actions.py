from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from kol_search.platforms import (
    ActionExecutionResult,
    PlatformOutcomeUpdate,
    PlatformRegistry,
    PlatformTaskContext,
    PlatformTaskName,
)
from kol_search.platforms.x import create_x_plugin
from kol_search.platform_modules.x_postiz import XPostizRequestUncertain


class RecordingPostiz:
    def __init__(
        self,
        *,
        create_result: object | None = None,
        recent_result: list[dict[str, object]] | None = None,
        create_error: Exception | None = None,
    ) -> None:
        self.create_result = create_result
        self.recent_result = recent_result or []
        self.create_error = create_error
        self.created: list[dict[str, object]] = []
        self.recent: list[dict[str, object]] = []

    def create_x_post(self, **kwargs):  # noqa: ANN003, ANN201 - test double
        self.created.append(kwargs)
        if self.create_error is not None:
            raise self.create_error
        return self.create_result

    def recent_posts(self, **kwargs):  # noqa: ANN003, ANN201 - test double
        self.recent.append(kwargs)
        return self.recent_result

    def close(self) -> None:
        return None


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        live_write_enabled=True,
        postiz_api_key="test-only",
        postiz_api_url="https://postiz.example.test/public/v1",
    )


def _execute(
    publisher: RecordingPostiz,
    *,
    mode: str,
    scheduled_at_utc: str | None = None,
) -> ActionExecutionResult:
    settings = _settings()
    registry = PlatformRegistry([create_x_plugin(settings)])
    return registry.dispatch(
        "x",
        PlatformTaskName.EXECUTE_ACTION,
        context=PlatformTaskContext(
            platform_id="x",
            settings=settings,
            services={"postiz_publisher": publisher},
            task_id=71,
            payload={
                "action_type": "owned_post",
                "core_action_id": 71,
                "native_object_type": "trend",
                "native_object_id": "trend-rwa",
                "draft": "A platform-native X post",
                "connection": {
                    "metadata": {
                        "kind": "postiz",
                        "integration_id": "integration-x-1",
                    }
                },
                "platform_payload": {
                    "integration_id": "integration-x-1",
                    "mode": mode,
                    "scheduled_at_utc": scheduled_at_utc,
                    "who_can_reply": "following",
                    "made_with_ai": True,
                },
            },
        ),
    )


def test_x_platform_schedules_postiz_action_with_confirmed_receipt() -> None:
    publisher = RecordingPostiz(create_result=[{"postId": "postiz-scheduled-1"}])
    scheduled_at = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()

    result = _execute(
        publisher,
        mode="schedule",
        scheduled_at_utc=scheduled_at,
    )

    assert result.success is True
    assert result.confirmed is True
    assert result.external_id == "postiz-scheduled-1"
    assert len(publisher.created) == 1
    call = publisher.created[0]
    assert call["integration_id"] == "integration-x-1"
    assert call["content"] == "A platform-native X post"
    assert call["mode"] == "schedule"
    assert call["publish_at"] == datetime.fromisoformat(scheduled_at)
    assert call["settings"] == {
        "who_can_reply_post": "following",
        "made_with_ai": True,
    }


def test_x_platform_immediate_post_without_release_url_requires_confirmation() -> None:
    publisher = RecordingPostiz(create_result=[{"postId": "postiz-now-1"}])

    result = _execute(publisher, mode="now")

    assert len(publisher.created) == 1
    assert result.success is True
    assert result.confirmed is False
    assert result.confirmation_required is True
    assert result.external_id == "postiz-now-1"
    assert result.receipt_url is None


def test_x_platform_uncertain_postiz_create_is_never_retried() -> None:
    publisher = RecordingPostiz(
        create_error=XPostizRequestUncertain("request may have reached Postiz")
    )

    result = _execute(publisher, mode="now")

    assert len(publisher.created) == 1
    assert result.success is True
    assert result.confirmed is False
    assert result.confirmation_required is True
    assert "may have reached Postiz" in str(result.error)


def test_x_platform_refreshes_uncertain_receipt_without_creating_again() -> None:
    release_url = "https://x.com/brand/status/987"
    publisher = RecordingPostiz(
        recent_result=[
            {
                "id": "postiz-now-42",
                "postId": "postiz-now-42",
                "content": "Need reconcile",
                "integration": {"id": "integration-x-1"},
                "releaseURL": release_url,
            }
        ]
    )
    settings = _settings()
    registry = PlatformRegistry([create_x_plugin(settings)])

    result = registry.dispatch(
        "x",
        PlatformTaskName.REFRESH_OUTCOMES,
        context=PlatformTaskContext(
            platform_id="x",
            settings=settings,
            services={"postiz_publisher": publisher},
            payload={
                "actions": [
                    {
                        "id": 42,
                        "platform_id": "x",
                        "status": "confirmation_required",
                        "action_type": "owned_post",
                        "external_id": "postiz-now-42",
                        "draft": "Need reconcile",
                        "created_at": "2026-07-21T03:30:00+00:00",
                        "payload": {
                            "integration_id": "integration-x-1",
                            "mode": "now",
                        },
                    }
                ]
            },
        ),
    )

    assert result.success is True
    assert result.items_processed == 1
    assert len(result.items) == 1
    update = result.items[0]
    assert isinstance(update, PlatformOutcomeUpdate)
    assert update.action_id == 42
    assert update.result.success is True
    assert update.result.confirmed is True
    assert update.result.external_id == "postiz-now-42"
    assert update.result.receipt_url == release_url
    assert publisher.created == []
    assert len(publisher.recent) == 1
    assert publisher.recent[0]["around"] == datetime(
        2026, 7, 21, 3, 30, tzinfo=timezone.utc
    )


def test_x_postiz_reconciliation_does_not_match_old_identical_content() -> None:
    release_url = "https://x.com/brand/status/current"
    publisher = RecordingPostiz(
        recent_result=[
            {
                "postId": "old-identical",
                "content": "Repeated brand message",
                "integration": {"id": "integration-x-1"},
                "createdAt": "2026-07-21T01:00:00+00:00",
                "releaseURL": "https://x.com/brand/status/old",
            },
            {
                "postId": "current-write",
                "content": "Repeated brand message",
                "integration": {"id": "integration-x-1"},
                "createdAt": "2026-07-21T03:31:00+00:00",
                "releaseURL": release_url,
            },
        ]
    )
    registry = PlatformRegistry([create_x_plugin(_settings())])

    result = registry.dispatch(
        "x",
        PlatformTaskName.REFRESH_OUTCOMES,
        context=PlatformTaskContext(
            platform_id="x",
            settings=_settings(),
            services={"postiz_publisher": publisher},
            payload={
                "actions": [
                    {
                        "id": 43,
                        "status": "confirmation_required",
                        "action_type": "owned_post",
                        "draft": "Repeated brand message",
                        "started_at": "2026-07-21T03:30:00+00:00",
                        "payload": {"integration_id": "integration-x-1"},
                    }
                ]
            },
        ),
    )

    assert len(result.items) == 1
    assert result.items[0].result.external_id == "current-write"
    assert result.items[0].result.receipt_url == release_url


def test_x_postiz_reconciliation_leaves_ambiguous_identical_posts_unconfirmed() -> None:
    publisher = RecordingPostiz(
        recent_result=[
            {
                "postId": f"candidate-{index}",
                "content": "Repeated brand message",
                "integration": {"id": "integration-x-1"},
                "createdAt": f"2026-07-21T03:{minute}:00+00:00",
                "releaseURL": f"https://x.com/brand/status/{index}",
            }
            for index, minute in enumerate((29, 31), start=1)
        ]
    )
    registry = PlatformRegistry([create_x_plugin(_settings())])

    result = registry.dispatch(
        "x",
        PlatformTaskName.REFRESH_OUTCOMES,
        context=PlatformTaskContext(
            platform_id="x",
            settings=_settings(),
            services={"postiz_publisher": publisher},
            payload={
                "actions": [
                    {
                        "id": 44,
                        "status": "confirmation_required",
                        "action_type": "owned_post",
                        "draft": "Repeated brand message",
                        "started_at": "2026-07-21T03:30:00+00:00",
                        "payload": {"integration_id": "integration-x-1"},
                    }
                ]
            },
        ),
    )

    assert result.items == ()
    assert "ambiguous Postiz receipts" in result.warnings[0]
