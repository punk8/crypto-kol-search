from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from kol_search.platform_modules.browser_writer import (
    OutboundConfirmationRequired,
    OutboundReceipt,
    XOpenCliWriter,
    XiaohongshuOpenCliWriter,
    _session_receipt,
)
from kol_search.platforms import (
    PlatformOutcomeUpdate,
    PlatformRegistry,
    PlatformTaskContext,
    PlatformTaskName,
)
from kol_search.platforms.x import create_x_plugin
from kol_search.platforms.xiaohongshu import create_xiaohongshu_plugin


class RecordingWriter:
    def __init__(self, *, confirmed: bool = True, verify_error: Exception | None = None):
        self.confirmed = confirmed
        self.verify_error = verify_error
        self.calls: list[tuple[str, dict[str, object]]] = []

    def reply(self, action):  # noqa: ANN001, ANN201 - platform-writer test double
        self.calls.append(("comment", action))
        return OutboundReceipt(
            status="sent",
            external_id="reply-1",
            url="https://example.test/receipt/reply-1",
            raw={"accepted": True},
        )

    def send_dm(self, action):  # noqa: ANN001, ANN201 - platform-writer test double
        self.calls.append(("dm", action))
        return OutboundReceipt(
            status="sent",
            external_id="dm-1",
            url="https://example.test/receipt/dm-1",
            raw={"accepted": True},
        )

    def verify_receipt(self, _action, _receipt):  # noqa: ANN001, ANN201
        if self.verify_error is not None:
            raise self.verify_error
        return self.confirmed


def test_irreversible_browser_click_is_issued_only_once_on_bridge_error() -> None:
    calls: list[list[str]] = []

    def runner(command, **_kwargs):  # noqa: ANN001, ANN003, ANN202
        calls.append(command)
        return subprocess.CompletedProcess(
            command, returncode=1, stdout="", stderr="bridge response lost"
        )

    writer = XOpenCliWriter(command="/test/opencli", runner=runner)
    with pytest.raises(OutboundConfirmationRequired, match="人工确认"):
        writer._click_write_once(  # noqa: SLF001 - exact-once safety regression
            "profile",
            "session",
            ("[data-testid='dmComposerSendButton']",),
        )

    assert len(calls) == 1


def test_selector_fallback_stops_after_uncertain_navigation_click() -> None:
    calls: list[list[str]] = []

    def runner(command, **_kwargs):  # noqa: ANN001, ANN003, ANN202
        calls.append(command)
        raise subprocess.TimeoutExpired(command, timeout=1)

    writer = XOpenCliWriter(command="/test/opencli", runner=runner)
    with pytest.raises(OutboundConfirmationRequired):
        writer._click_first(  # noqa: SLF001 - uncertainty regression
            "profile",
            "session",
            (("--text", "first"), ("--text", "second")),
        )

    assert len(calls) == 1


def test_composer_text_is_not_accepted_as_a_platform_receipt() -> None:
    calls: list[list[str]] = []

    def runner(command, **_kwargs):  # noqa: ANN001, ANN003, ANN202
        calls.append(command)
        value = "still in composer"
        return subprocess.CompletedProcess(
            command,
            returncode=0,
            stdout=f'{{"value": "{value}"}}',
            stderr="",
        )

    writer = XiaohongshuOpenCliWriter(
        command="/test/opencli", runner=runner
    )
    receipt = OutboundReceipt(
        status="sent",
        raw={"browser_session": "session-with-unsubmitted-composer"},
    )

    assert (
        writer.verify_receipt(
            {
                "kind": "comment",
                "browser_profile": "profile",
                "final_text": "still in composer",
            },
            receipt,
        )
        is False
    )
    assert all("body" not in command for command in calls)


def test_precise_message_element_and_cleared_composer_confirm_receipt() -> None:
    def runner(command, **_kwargs):  # noqa: ANN001, ANN003, ANN202
        selector = command[-1]
        value = "sent message" if "messageEntry" in selector else ""
        return subprocess.CompletedProcess(
            command,
            returncode=0,
            stdout=f'{{"value": "{value}"}}',
            stderr="",
        )

    writer = XOpenCliWriter(command="/test/opencli", runner=runner)
    receipt = OutboundReceipt(
        status="sent",
        raw={
            "browser_session": "confirmed-session",
            "posted_text_before": "",
        },
    )

    assert writer.verify_receipt(
        {
            "kind": "dm",
            "browser_profile": "profile",
            "final_text": "sent message",
        },
        receipt,
    )


def test_old_identical_message_is_not_accepted_as_a_new_browser_receipt() -> None:
    def runner(command, **_kwargs):  # noqa: ANN001, ANN003, ANN202
        selector = command[-1]
        value = "same old message" if "messageEntry" in selector else ""
        return subprocess.CompletedProcess(
            command,
            returncode=0,
            stdout=f'{{"value": "{value}"}}',
            stderr="",
        )

    writer = XOpenCliWriter(command="/test/opencli", runner=runner)
    receipt = OutboundReceipt(
        status="sent",
        raw={
            "browser_session": "same-text-session",
            "posted_text_before": "same old message",
        },
    )

    assert not writer.verify_receipt(
        {
            "kind": "dm",
            "browser_profile": "profile",
            "final_text": "same old message",
        },
        receipt,
    )


def test_browser_session_receipt_keeps_native_id_empty_and_preserves_baseline() -> None:
    receipt = _session_receipt(
        "browser-session",
        "https://example.test/native-target",
        posted_text_before="previous message",
    )

    assert receipt.external_id is None
    assert receipt.raw == {
        "browser_session": "browser-session",
        "posted_text_before": "previous message",
    }


def test_x_comment_receipt_must_identify_a_new_status_not_the_target() -> None:
    writer = XOpenCliWriter(command="/test/opencli")
    action = {
        "kind": "comment",
        "target_external_id": "target-42",
    }

    assert not writer.verify_receipt(
        action,
        OutboundReceipt(
            status="sent",
            url="https://x.com/creator/status/target-42",
            external_id="target-42",
        ),
    )
    assert writer.verify_receipt(
        action,
        OutboundReceipt(
            status="sent",
            url="https://x.com/brand/status/reply-43",
            external_id="reply-43",
        ),
    )


def test_x_comment_resolves_missing_opencli_url_from_exact_thread_reply() -> None:
    calls: list[list[str]] = []

    def runner(command, **_kwargs):  # noqa: ANN001, ANN003, ANN202
        calls.append(command)
        if "whoami" in command:
            payload = {"logged_in": True, "username": "brand"}
        elif "reply" in command:
            payload = {
                "status": "success",
                "message": "Reply posted successfully.",
                "text": "Useful question?",
            }
        elif "thread" in command:
            payload = [
                {
                    "id": "target-42",
                    "author": "creator",
                    "text": "Source post",
                    "url": "https://x.com/creator/status/target-42",
                },
                {
                    "id": "reply-43",
                    "author": "brand",
                    "text": "@creator Useful question?",
                    "in_reply_to": "target-42",
                    "url": "https://x.com/brand/status/reply-43",
                },
            ]
        else:  # pragma: no cover - guards the fake command contract
            raise AssertionError(command)
        return subprocess.CompletedProcess(
            command,
            returncode=0,
            stdout=__import__("json").dumps(payload),
            stderr="",
        )

    writer = XOpenCliWriter(command="/test/opencli", runner=runner)
    receipt = writer.reply(
        {
            "id": 7,
            "kind": "comment",
            "browser_profile": "profile",
            "managed_username": "brand",
            "managed_external_account_id": "brand",
            "target_external_id": "target-42",
            "target_url": "https://x.com/creator/status/target-42",
            "final_text": "Useful question?",
        }
    )

    assert receipt.external_id == "reply-43"
    assert receipt.url == "https://x.com/brand/status/reply-43"
    assert writer.verify_receipt(
        {"kind": "comment", "target_external_id": "target-42"}, receipt
    )
    assert sum("reply" in command for command in calls) == 1


@pytest.mark.parametrize(
    ("platform_id", "plugin_factory", "action_type", "expected_method"),
    [
        ("x", create_x_plugin, "comment", "comment"),
        ("xiaohongshu", create_xiaohongshu_plugin, "dm", "dm"),
    ],
)
def test_platform_handler_owns_browser_write_and_native_payload(
    platform_id,
    plugin_factory,
    action_type,
    expected_method,
) -> None:
    settings = SimpleNamespace(live_write_enabled=True)
    writer = RecordingWriter()
    registry = PlatformRegistry([plugin_factory(settings)])

    result = registry.dispatch(
        platform_id,
        PlatformTaskName.EXECUTE_ACTION,
        context=PlatformTaskContext(
            platform_id=platform_id,
            settings=settings,
            task_id=41,
            services={"browser_writer": writer},
            payload={
                "action_type": action_type,
                "core_action_id": 41,
                "native_object_id": "native-target",
                "draft": "A useful response",
                "connection": {
                    "metadata": {
                        "browser_profile": "brand-profile",
                        "username": "brand",
                        "external_account_id": "brand-1",
                    }
                },
                "opportunity": {
                    "payload": {
                        "target_url": "https://example.test/original",
                        "target_author_id": "creator-1",
                    }
                },
                "platform_payload": {
                    "target_url": "https://example.test/platform-native"
                },
            },
        ),
    )

    assert result.success is True
    assert result.confirmed is True
    assert result.external_id == ("reply-1" if action_type == "comment" else "dm-1")
    assert len(writer.calls) == 1
    method, native_action = writer.calls[0]
    assert method == expected_method
    assert native_action == {
        "id": 41,
        "platform": platform_id,
        "kind": action_type,
        "browser_profile": "brand-profile",
        "managed_username": "brand",
        "managed_external_account_id": "brand-1",
        "target_external_id": "native-target",
        "target_author_id": "creator-1",
        "target_url": "https://example.test/platform-native",
        "final_text": "A useful response",
    }


def test_platform_handler_treats_failed_post_write_verification_as_uncertain() -> None:
    settings = SimpleNamespace(live_write_enabled=True)
    writer = RecordingWriter(verify_error=RuntimeError("receipt page unavailable"))
    registry = PlatformRegistry([create_x_plugin(settings)])

    result = registry.dispatch(
        "x",
        PlatformTaskName.EXECUTE_ACTION,
        context=PlatformTaskContext(
            platform_id="x",
            settings=settings,
            services={"browser_writer": writer},
            payload={
                "action_type": "comment",
                "core_action_id": 42,
                "native_object_id": "tweet-42",
                "draft": "One write only",
                "connection": {"metadata": {"browser_profile": "brand-profile"}},
                "opportunity": {
                    "payload": {"target_url": "https://x.com/creator/status/42"}
                },
            },
        ),
    )

    assert len(writer.calls) == 1
    assert result.success is True
    assert result.confirmed is False
    assert result.confirmation_required is True
    assert result.external_id == "reply-1"
    assert "receipt page unavailable" in str(result.error)


def test_platform_handler_blocks_write_when_live_writes_are_disabled() -> None:
    settings = SimpleNamespace(live_write_enabled=False)
    writer = RecordingWriter()
    registry = PlatformRegistry([create_xiaohongshu_plugin(settings)])

    result = registry.dispatch(
        "xiaohongshu",
        PlatformTaskName.EXECUTE_ACTION,
        context=PlatformTaskContext(
            platform_id="xiaohongshu",
            settings=settings,
            services={"browser_writer": writer},
            payload={"action_type": "dm"},
        ),
    )

    assert result.success is False
    assert result.error == "live writes are disabled"
    assert writer.calls == []


@pytest.mark.parametrize(
    ("platform_id", "plugin_factory"),
    [("x", create_x_plugin), ("xiaohongshu", create_xiaohongshu_plugin)],
)
def test_platform_refreshes_browser_receipt_without_repeating_write(
    platform_id,
    plugin_factory,
) -> None:
    settings = SimpleNamespace(live_write_enabled=True)
    writer = RecordingWriter(confirmed=True)
    registry = PlatformRegistry([plugin_factory(settings)])

    result = registry.dispatch(
        platform_id,
        PlatformTaskName.REFRESH_OUTCOMES,
        context=PlatformTaskContext(
            platform_id=platform_id,
            settings=settings,
            services={"browser_writer": writer},
            payload={
                "actions": [
                    {
                        "id": 73,
                        "platform_id": platform_id,
                        "status": "confirmation_required",
                        "action_type": "comment",
                        "native_object_id": "native-73",
                        "draft": "Already sent once",
                        "external_id": "reply-73",
                        "receipt_url": "https://example.test/receipt/reply-73",
                        "receipt": {"browser_session": "receipt-session-73"},
                        "connection": {
                            "metadata": {
                                "browser_profile": "brand-profile",
                                "username": "brand",
                                "external_account_id": "brand-1",
                            }
                        },
                    }
                ]
            },
        ),
    )

    assert result.success is True
    assert result.metadata == {"actions_checked": 1, "outcomes_updated": 1}
    assert len(result.items) == 1
    update = result.items[0]
    assert isinstance(update, PlatformOutcomeUpdate)
    assert update.action_id == 73
    assert update.result.confirmed is True
    assert update.result.external_id == "reply-73"
    assert update.result.raw_receipt == {"browser_session": "receipt-session-73"}
    assert writer.calls == []
