from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Iterable
from urllib.parse import urlparse


class PolicyOutcome(StrEnum):
    AUTO_EXECUTE = "auto_execute"
    NEEDS_REVIEW = "needs_review"
    REJECTED = "rejected"


class ActionType(StrEnum):
    COMMENT = "comment"
    DM = "dm"
    OWNED_POST = "owned_post"


@dataclass(frozen=True)
class PolicyLimits:
    """Deterministic automation thresholds shared by every platform."""

    comment_auto_score: float = 80.0
    dm_followup_auto_score: float = 80.0
    owned_post_auto_score: float = 80.0
    comment_hourly_limit: int = 3
    comment_daily_limit: int = 10
    dm_hourly_limit: int = 2
    dm_daily_limit: int = 5
    owned_post_daily_limit: int = 2


@dataclass(frozen=True)
class PolicyContext:
    platform_id: str
    action_type: ActionType
    score: float
    text: str
    capability_available: bool
    account_status: str = "active"
    is_initial_dm: bool = False
    has_valid_conversation: bool = False
    expires_at: str | None = None
    global_paused: bool = False
    platform_paused: bool = False
    account_paused: bool = False
    action_type_paused: bool = False
    duplicate: bool = False
    author_in_cooldown: bool = False
    hourly_usage: int = 0
    daily_usage: int = 0
    forbidden_terms: tuple[str, ...] = ()
    approved_link_domains: tuple[str, ...] = ()
    comment_links_allowed: bool = False


@dataclass(frozen=True)
class PolicyDecision:
    outcome: PolicyOutcome
    reasons: tuple[str, ...]
    policy_version: str = "2026-07-v1"
    checks: dict[str, bool | int | float | str] = field(default_factory=dict)


_URL_PATTERN = re.compile(r"https?://|www\.", re.IGNORECASE)


def _expired(value: str | None, now: datetime) -> bool:
    if not value:
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return True
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc) <= now


def _contains_forbidden(text: str, terms: Iterable[str]) -> str | None:
    normalized = text.casefold()
    return next((term for term in terms if term and term.casefold() in normalized), None)


class AutomationPolicy:
    """Evaluate an action without performing I/O or platform-specific inference."""

    def __init__(self, limits: PolicyLimits | None = None) -> None:
        self.limits = limits or PolicyLimits()

    def evaluate(
        self,
        context: PolicyContext,
        *,
        now: datetime | None = None,
    ) -> PolicyDecision:
        current = now or datetime.now(timezone.utc)
        checks: dict[str, bool | int | float | str] = {
            "score": context.score,
            "capability_available": context.capability_available,
            "account_status": context.account_status,
            "hourly_usage": context.hourly_usage,
            "daily_usage": context.daily_usage,
        }

        blockers: list[str] = []
        if not context.capability_available:
            blockers.append("platform capability is unavailable")
        if context.account_status != "active":
            blockers.append("managed account is not active")
        if context.global_paused:
            blockers.append("global kill switch is enabled")
        if context.platform_paused:
            blockers.append("platform channel is paused")
        if context.account_paused:
            blockers.append("account channel is paused")
        if context.action_type_paused:
            blockers.append("action type is paused")
        if context.duplicate:
            blockers.append("duplicate target or idempotency key")
        if context.author_in_cooldown:
            blockers.append("target author is in cooldown")
        if _expired(context.expires_at, current):
            blockers.append("opportunity expired")
        forbidden = _contains_forbidden(context.text, context.forbidden_terms)
        if forbidden:
            blockers.append(f"content contains forbidden term: {forbidden}")
        if (
            context.action_type == ActionType.COMMENT
            and not context.comment_links_allowed
            and _URL_PATTERN.search(context.text)
        ):
            blockers.append("comment links are disabled")
        if context.action_type == ActionType.DM:
            approved = {domain.strip().lower() for domain in context.approved_link_domains}
            for value in re.findall(r"https?://[^\s<>]+", context.text, flags=re.IGNORECASE):
                hostname = (urlparse(value.rstrip(".,;:!?)]}")).hostname or "").lower()
                if not hostname or not any(
                    hostname == domain or hostname.endswith(f".{domain}")
                    for domain in approved
                ):
                    blockers.append(f"DM link domain is not approved: {hostname or value}")

        hourly_limit, daily_limit = self._usage_limits(context.action_type)
        if context.hourly_usage >= hourly_limit:
            blockers.append("hourly limit reached")
        if context.daily_usage >= daily_limit:
            blockers.append("daily limit reached")
        checks.update({"hourly_limit": hourly_limit, "daily_limit": daily_limit})

        if blockers:
            return PolicyDecision(PolicyOutcome.REJECTED, tuple(blockers), checks=checks)

        if context.action_type == ActionType.DM:
            if context.is_initial_dm or not context.has_valid_conversation:
                return PolicyDecision(
                    PolicyOutcome.NEEDS_REVIEW,
                    ("initial outreach requires human approval",),
                    checks=checks,
                )
            threshold = self.limits.dm_followup_auto_score
        elif context.action_type == ActionType.OWNED_POST:
            threshold = self.limits.owned_post_auto_score
        else:
            threshold = self.limits.comment_auto_score

        checks["auto_score_threshold"] = threshold
        if context.score < threshold:
            return PolicyDecision(
                PolicyOutcome.NEEDS_REVIEW,
                (f"score {context.score:.1f} is below auto threshold {threshold:.1f}",),
                checks=checks,
            )
        return PolicyDecision(
            PolicyOutcome.AUTO_EXECUTE,
            ("all deterministic automation checks passed",),
            checks=checks,
        )

    def _usage_limits(self, action_type: ActionType) -> tuple[int, int]:
        if action_type == ActionType.COMMENT:
            return self.limits.comment_hourly_limit, self.limits.comment_daily_limit
        if action_type == ActionType.DM:
            return self.limits.dm_hourly_limit, self.limits.dm_daily_limit
        return self.limits.owned_post_daily_limit, self.limits.owned_post_daily_limit
