from datetime import datetime, timedelta, timezone

from kol_search.automation.policy import (
    ActionType,
    AutomationPolicy,
    PolicyContext,
    PolicyOutcome,
)
from kol_search.automation.service import _policy_pause_flags
from kol_search.discovery.promotion import (
    BalancedPromotionPolicy,
    KolStatus,
    PromotionInput,
)


def test_high_confidence_comment_can_auto_execute() -> None:
    decision = AutomationPolicy().evaluate(
        PolicyContext(
            platform_id="x",
            action_type=ActionType.COMMENT,
            score=86,
            text="Useful framing. The next onchain metric should make this testable.",
            capability_available=True,
            expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        )
    )
    assert decision.outcome == PolicyOutcome.AUTO_EXECUTE


def test_initial_dm_always_requires_review() -> None:
    decision = AutomationPolicy().evaluate(
        PolicyContext(
            platform_id="instagram",
            action_type=ActionType.DM,
            score=99,
            text="Official introduction",
            capability_available=True,
            is_initial_dm=True,
        )
    )
    assert decision.outcome == PolicyOutcome.NEEDS_REVIEW


def test_dm_links_require_an_approved_domain() -> None:
    rejected = AutomationPolicy().evaluate(
        PolicyContext(
            platform_id="x",
            action_type=ActionType.DM,
            score=99,
            text="See https://unapproved.example/path",
            capability_available=True,
            is_initial_dm=True,
            approved_link_domains=("product.example.com",),
        )
    )
    assert rejected.outcome == PolicyOutcome.REJECTED
    assert "DM link domain is not approved" in rejected.reasons[0]

    reviewed = AutomationPolicy().evaluate(
        PolicyContext(
            platform_id="x",
            action_type=ActionType.DM,
            score=99,
            text="See https://docs.product.example.com/path",
            capability_available=True,
            is_initial_dm=True,
            approved_link_domains=("product.example.com",),
        )
    )
    assert reviewed.outcome == PolicyOutcome.NEEDS_REVIEW


def test_safety_blocker_rejects_instead_of_retrying() -> None:
    decision = AutomationPolicy().evaluate(
        PolicyContext(
            platform_id="x",
            action_type=ActionType.COMMENT,
            score=90,
            text="Read https://example.com",
            capability_available=True,
        )
    )
    assert decision.outcome == PolicyOutcome.REJECTED
    assert "comment links are disabled" in decision.reasons


def test_balanced_promotion_and_pause() -> None:
    policy = BalancedPromotionPolicy()
    promoted = policy.decide(
        PromotionInput(
            score=0.75,
            relevant_content_count_30d=3,
            relationship_evidence_count=1,
        )
    )
    assert promoted.status == KolStatus.ACTIVE

    paused = policy.decide(
        PromotionInput(
            score=0.9,
            relevant_content_count_30d=0,
            relationship_evidence_count=2,
            current_status=KolStatus.ACTIVE,
            inactive_days=100,
        )
    )
    assert paused.status == KolStatus.PAUSED


def test_balanced_promotion_can_run_without_a_manual_review_queue() -> None:
    policy = BalancedPromotionPolicy(review_enabled=False)

    observed = policy.decide(
        PromotionInput(
            score=0.68,
            relevant_content_count_30d=2,
            relationship_evidence_count=0,
        )
    )

    assert observed.status == KolStatus.CANDIDATE
    assert observed.qualified is False
    assert "automatic scoring will continue" in observed.reasons[0]


def test_kill_switch_scope_is_preserved_in_policy_context() -> None:
    flags = _policy_pause_flags(
        {
            "allowed": False,
            "blocked_by": [
                {"scope": "platform"},
                {"scope": "action_type"},
            ],
        }
    )

    assert flags == {
        "global_paused": False,
        "platform_paused": True,
        "account_paused": False,
        "action_type_paused": True,
    }
