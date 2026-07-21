from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class KolStatus(StrEnum):
    SEED = "seed"
    CANDIDATE = "candidate"
    ACTIVE = "active"
    REVIEW = "review"
    PAUSED = "paused"
    REJECTED = "rejected"


@dataclass(frozen=True)
class PromotionInput:
    score: float
    relevant_content_count_30d: int
    relationship_evidence_count: int
    protected: bool = False
    disabled: bool = False
    spam_risk: float = 0.0
    current_status: KolStatus = KolStatus.CANDIDATE
    inactive_days: int = 0


@dataclass(frozen=True)
class PromotionDecision:
    status: KolStatus
    reasons: tuple[str, ...]
    qualified: bool = False


class BalancedPromotionPolicy:
    """Platform-neutral admission guard; platforms remain responsible for scoring."""

    def __init__(
        self,
        *,
        promote_score: float = 0.70,
        review_score: float = 0.55,
        review_enabled: bool = True,
        max_spam_risk: float = 0.60,
        pause_after_days: int = 90,
    ) -> None:
        self.promote_score = promote_score
        self.review_score = review_score
        self.review_enabled = review_enabled
        self.max_spam_risk = max_spam_risk
        self.pause_after_days = pause_after_days

    def decide(self, value: PromotionInput) -> PromotionDecision:
        if value.current_status == KolStatus.SEED:
            return PromotionDecision(KolStatus.SEED, ("curated seed",))
        if value.disabled or value.protected or value.spam_risk > self.max_spam_risk:
            reasons = []
            if value.disabled:
                reasons.append("account is disabled")
            if value.protected:
                reasons.append("account is protected")
            if value.spam_risk > self.max_spam_risk:
                reasons.append("spam risk exceeds threshold")
            return PromotionDecision(KolStatus.REJECTED, tuple(reasons))
        qualifies = (
            value.score >= self.promote_score
            and value.relevant_content_count_30d > 0
            and value.relationship_evidence_count > 0
        )
        if qualifies:
            return PromotionDecision(
                KolStatus.ACTIVE,
                ("score, recent relevance, and relationship evidence passed",),
                qualified=True,
            )
        if value.current_status == KolStatus.ACTIVE:
            if value.inactive_days >= self.pause_after_days:
                return PromotionDecision(
                    KolStatus.PAUSED,
                    (f"no qualifying activity for {value.inactive_days} days",),
                )
            return PromotionDecision(
                KolStatus.ACTIVE,
                ("active KOL retained during the inactivity grace period",),
            )
        if (
            self.review_enabled
            and value.score >= self.review_score
            and value.relevant_content_count_30d > 0
        ):
            return PromotionDecision(
                KolStatus.REVIEW,
                ("candidate is near the automatic promotion boundary",),
            )
        if value.score >= self.review_score and value.relevant_content_count_30d > 0:
            return PromotionDecision(
                KolStatus.CANDIDATE,
                ("automatic scoring will continue until the promotion boundary is met",),
            )
        return PromotionDecision(
            KolStatus.CANDIDATE,
            ("more relevance or relationship evidence is required",),
        )
