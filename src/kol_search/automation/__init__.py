"""Shared automation persistence and deterministic policy primitives."""

from .database import AutomationDatabase
from .models import (
    ActionKind,
    ActionStatus,
    ChannelControlScope,
    ChannelState,
    CoreJobStatus,
    CoreJobType,
    OpportunityStatus,
    PlatformConnectionStatus,
)
from .policy import (
    ActionType,
    AutomationPolicy,
    PolicyContext,
    PolicyDecision,
    PolicyLimits,
    PolicyOutcome,
)
from .repository import AutomationStore, RecordNotFoundError, StateTransitionError

__all__ = [
    "ActionKind",
    "ActionStatus",
    "ActionType",
    "AutomationDatabase",
    "AutomationPolicy",
    "AutomationStore",
    "ChannelControlScope",
    "ChannelState",
    "CoreJobStatus",
    "CoreJobType",
    "OpportunityStatus",
    "PlatformConnectionStatus",
    "PolicyContext",
    "PolicyDecision",
    "PolicyLimits",
    "PolicyOutcome",
    "RecordNotFoundError",
    "StateTransitionError",
]
