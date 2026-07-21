from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from kol_search.platform_modules.browser_writer import (
    OutboundConfirmationRequired,
    OutboundError,
    OutboundReceipt,
    TargetNotMessageable,
    XOpenCliWriter,
    XiaohongshuOpenCliWriter,
)
from kol_search.platforms.kernel import (
    ActionExecutionResult,
    PlatformOutcomeUpdate,
    PlatformTaskContext,
    PlatformTaskResult,
)


def execute_x_browser_action(context: PlatformTaskContext) -> ActionExecutionResult:
    return _execute_browser_action(context, XOpenCliWriter)


def execute_xiaohongshu_browser_action(
    context: PlatformTaskContext,
) -> ActionExecutionResult:
    return _execute_browser_action(context, XiaohongshuOpenCliWriter)


def _execute_browser_action(
    context: PlatformTaskContext, writer_type: type[object]
) -> ActionExecutionResult:
    """Execute one platform-native comment or DM without retrying the write.

    Policy, quotas, and channel state are evaluated by the shared automation
    service before this handler is called.  This boundary owns the browser
    command and converts its receipt into the platform-neutral result envelope.
    """

    settings = context.settings
    if settings is None or not bool(getattr(settings, "live_write_enabled", False)):
        return ActionExecutionResult.failed("live writes are disabled")

    payload = context.payload
    action_type = str(payload.get("action_type") or payload.get("kind") or "")
    if action_type not in {"comment", "dm"}:
        return ActionExecutionResult.failed(
            f"unsupported browser action type: {action_type or 'missing'}"
        )

    connection = _mapping(payload.get("connection"))
    connection_metadata = _mapping(connection.get("metadata"))
    if not connection:
        return ActionExecutionResult.failed("managed platform connection is missing")
    if not connection_metadata.get("browser_profile"):
        return ActionExecutionResult.failed("managed platform connection has no browser profile")

    opportunity = _mapping(payload.get("opportunity"))
    opportunity_payload = _mapping(opportunity.get("payload"))
    platform_payload = _mapping(payload.get("platform_payload"))
    native_object_id = str(
        platform_payload.get("target_external_id")
        or payload.get("native_object_id")
        or opportunity.get("native_object_id")
        or ""
    )
    if not native_object_id:
        return ActionExecutionResult.failed("platform action target is missing")

    # Platform action payloads may refine native delivery details produced when
    # the action was proposed.  The opportunity remains the durable fallback.
    native_payload = {**opportunity_payload, **platform_payload}
    writer_action: dict[str, Any] = {
        "id": payload.get("core_action_id") or context.task_id,
        "platform": context.platform_id,
        "kind": action_type,
        "browser_profile": connection_metadata.get("browser_profile"),
        "managed_username": connection_metadata.get("username"),
        "managed_external_account_id": connection_metadata.get(
            "external_account_id"
        ),
        "target_external_id": native_object_id,
        "target_author_id": native_payload.get("target_author_id"),
        "target_url": native_payload.get("target_url"),
        "final_text": str(payload.get("draft") or ""),
    }

    writer = context.services.get("browser_writer")
    if writer is None:
        writer = writer_type(
            command=str(getattr(settings, "opencli_command", "opencli")),
            timeout=float(getattr(settings, "opencli_timeout_seconds", 90.0)),
        )

    try:
        receipt = (
            writer.reply(writer_action)  # type: ignore[attr-defined]
            if action_type == "comment"
            else writer.send_dm(writer_action)  # type: ignore[attr-defined]
        )
    except OutboundConfirmationRequired as exc:
        return ActionExecutionResult(success=True, confirmed=False, error=str(exc))
    except (TargetNotMessageable, OutboundError) as exc:
        return ActionExecutionResult.failed(str(exc))

    try:
        confirmed = bool(writer.verify_receipt(writer_action, receipt))  # type: ignore[attr-defined]
    except Exception as exc:
        # The write call already returned.  A failed verification is therefore
        # uncertain, never a safe signal to retry the write automatically.
        return ActionExecutionResult(
            success=True,
            external_id=getattr(receipt, "external_id", None),
            receipt_url=getattr(receipt, "url", None),
            raw_receipt=getattr(receipt, "raw", None),
            confirmed=False,
            error=f"platform receipt could not be confirmed: {exc}",
        )

    return ActionExecutionResult(
        success=True,
        external_id=getattr(receipt, "external_id", None),
        receipt_url=getattr(receipt, "url", None),
        raw_receipt=getattr(receipt, "raw", None),
        confirmed=confirmed,
        error=None if confirmed else "platform receipt could not be confirmed",
    )


def refresh_x_browser_outcomes(context: PlatformTaskContext) -> PlatformTaskResult:
    return _refresh_browser_outcomes(context, XOpenCliWriter)


def refresh_xiaohongshu_browser_outcomes(
    context: PlatformTaskContext,
) -> PlatformTaskResult:
    return _refresh_browser_outcomes(context, XiaohongshuOpenCliWriter)


def _refresh_browser_outcomes(
    context: PlatformTaskContext, writer_type: type[object]
) -> PlatformTaskResult:
    """Re-check uncertain browser receipts without repeating the original write."""

    actions = tuple(
        action
        for action in (context.payload.get("actions") or ())
        if isinstance(action, Mapping)
        and str(action.get("status") or "") == "confirmation_required"
        and str(action.get("action_type") or "") in {"comment", "dm"}
    )
    if not actions:
        return PlatformTaskResult.completed(
            metadata={"actions_checked": 0, "outcomes_updated": 0}
        )

    settings = context.settings
    writer = context.services.get("browser_writer")
    if writer is None:
        writer = writer_type(
            command=str(getattr(settings, "opencli_command", "opencli")),
            timeout=float(getattr(settings, "opencli_timeout_seconds", 90.0)),
        )

    updates: list[PlatformOutcomeUpdate] = []
    warnings: list[str] = []
    checked = 0
    for action in actions:
        action_id = _positive_int(action.get("id"))
        if action_id is None:
            warnings.append("ignored a browser outcome without a valid action id")
            continue
        connection = _mapping(action.get("connection"))
        metadata = _mapping(connection.get("metadata"))
        browser_profile = str(metadata.get("browser_profile") or "").strip()
        if not browser_profile:
            warnings.append(f"action {action_id} has no browser profile for receipt lookup")
            continue

        receipt_data = _mapping(action.get("receipt"))
        receipt = OutboundReceipt(
            status="sent",
            external_id=_optional_string(action.get("external_id")),
            url=_optional_string(action.get("receipt_url")),
            raw=receipt_data or None,
        )
        writer_action: dict[str, Any] = {
            "id": action_id,
            "platform": context.platform_id,
            "kind": str(action.get("action_type") or ""),
            "browser_profile": browser_profile,
            "managed_username": metadata.get("username"),
            "managed_external_account_id": metadata.get("external_account_id"),
            "target_external_id": str(action.get("native_object_id") or ""),
            "target_author_id": None,
            "target_url": receipt.url,
            "final_text": str(action.get("final_text") or action.get("draft") or ""),
        }
        checked += 1
        try:
            confirmed = bool(writer.verify_receipt(writer_action, receipt))  # type: ignore[attr-defined]
        except Exception as exc:
            warnings.append(f"action {action_id} receipt lookup failed: {exc}")
            continue
        if not confirmed:
            warnings.append(f"action {action_id} remains unconfirmed")
            continue
        updates.append(
            PlatformOutcomeUpdate(
                action_id=action_id,
                result=ActionExecutionResult(
                    success=True,
                    external_id=receipt.external_id,
                    receipt_url=receipt.url,
                    raw_receipt=receipt.raw,
                    confirmed=True,
                ),
            )
        )

    return PlatformTaskResult.completed(
        updates,
        warnings=warnings,
        metadata={
            "actions_checked": checked,
            "outcomes_updated": len(updates),
        },
    )


def _mapping(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _optional_string(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _positive_int(value: object) -> int | None:
    try:
        parsed = int(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


__all__ = [
    "execute_x_browser_action",
    "execute_xiaohongshu_browser_action",
    "refresh_x_browser_outcomes",
    "refresh_xiaohongshu_browser_outcomes",
]
