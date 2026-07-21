from __future__ import annotations

from enum import StrEnum
from typing import TypeVar

from .policy import ActionType, PolicyOutcome


class PlatformConnectionStatus(StrEnum):
    CONNECTED = "connected"
    DEGRADED = "degraded"
    DISCONNECTED = "disconnected"
    PAUSED = "paused"


class CoreJobType(StrEnum):
    MANUAL_DISCOVERY = "manual_discovery"
    DISCOVERY_REFRESH = "discovery_refresh"
    SIGNAL_REFRESH = "signal_refresh"
    DISPATCH_ACTIONS = "dispatch_actions"
    REFRESH_OUTCOMES = "refresh_outcomes"


class CoreJobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class OpportunityStatus(StrEnum):
    OPEN = "open"
    PLANNED = "planned"
    DISMISSED = "dismissed"
    EXPIRED = "expired"


ActionKind = ActionType


class ActionStatus(StrEnum):
    PROPOSED = "proposed"
    NEEDS_REVIEW = "needs_review"
    SCHEDULED = "scheduled"
    EXECUTING = "executing"
    SUCCEEDED = "succeeded"
    CONFIRMATION_REQUIRED = "confirmation_required"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ChannelControlScope(StrEnum):
    GLOBAL = "global"
    PLATFORM = "platform"
    ACCOUNT = "account"
    ACTION_TYPE = "action_type"


class ChannelState(StrEnum):
    ENABLED = "enabled"
    PAUSED = "paused"


OPPORTUNITY_TRANSITIONS: dict[OpportunityStatus, frozenset[OpportunityStatus]] = {
    OpportunityStatus.OPEN: frozenset(
        {
            OpportunityStatus.PLANNED,
            OpportunityStatus.DISMISSED,
            OpportunityStatus.EXPIRED,
        }
    ),
    OpportunityStatus.PLANNED: frozenset(
        {OpportunityStatus.DISMISSED, OpportunityStatus.EXPIRED}
    ),
    OpportunityStatus.DISMISSED: frozenset(),
    OpportunityStatus.EXPIRED: frozenset(),
}


ACTION_TRANSITIONS: dict[ActionStatus, frozenset[ActionStatus]] = {
    ActionStatus.PROPOSED: frozenset(
        {
            ActionStatus.NEEDS_REVIEW,
            ActionStatus.SCHEDULED,
            ActionStatus.CANCELLED,
        }
    ),
    ActionStatus.NEEDS_REVIEW: frozenset(
        {
            ActionStatus.SCHEDULED,
            ActionStatus.CANCELLED,
        }
    ),
    ActionStatus.SCHEDULED: frozenset(
        {ActionStatus.EXECUTING, ActionStatus.CANCELLED}
    ),
    ActionStatus.EXECUTING: frozenset(
        {
            ActionStatus.SUCCEEDED,
            ActionStatus.CONFIRMATION_REQUIRED,
            ActionStatus.FAILED,
        }
    ),
    ActionStatus.CONFIRMATION_REQUIRED: frozenset(
        {ActionStatus.SUCCEEDED, ActionStatus.FAILED, ActionStatus.CANCELLED}
    ),
    ActionStatus.SUCCEEDED: frozenset(),
    ActionStatus.FAILED: frozenset(),
    ActionStatus.CANCELLED: frozenset(),
}


EnumT = TypeVar("EnumT", bound=StrEnum)


def enum_value(value: EnumT | str, enum_type: type[EnumT]) -> str:
    """Validate a public string value and return its stable database representation."""
    try:
        return enum_type(value).value
    except ValueError as exc:
        allowed = ", ".join(member.value for member in enum_type)
        raise ValueError(f"Invalid {enum_type.__name__} {value!r}; expected one of: {allowed}") from exc
