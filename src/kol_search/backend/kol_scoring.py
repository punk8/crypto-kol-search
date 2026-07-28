from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from statistics import median

from kol_search.backend.models import AccountEvidence, AccountScoreComponent
from kol_search.twitter.models import XAccount, XTweet


SCORE_VERSION = "x-kol-internal-v2"
_IGNORED_TERMS = {
    "and",
    "or",
    "the",
    "researcher",
    "researchers",
    "expert",
    "experts",
    "kol",
}


@dataclass(frozen=True, slots=True)
class ScoredAccount:
    score: int
    confidence: str
    components: tuple[AccountScoreComponent, ...]
    evidence: tuple[AccountEvidence, ...]


def domain_terms(query: str) -> tuple[str, ...]:
    terms = [
        value.casefold()
        for value in re.findall(r"[\w+#.-]{2,}", query, flags=re.UNICODE)
        if not value.startswith(("min_", "filter", "lang"))
    ]
    return tuple(dict.fromkeys(value for value in terms if value not in _IGNORED_TERMS))


def text_relevance(text: str, terms: tuple[str, ...]) -> float:
    if not terms:
        return 0.0
    normalized = text.casefold()
    matches = 0
    for term in terms:
        if not term.isascii():
            matched = term in normalized
        elif term.isalpha() and len(term) >= 4:
            root = term[:-1] if term.endswith("s") else term
            matched = bool(
                re.search(rf"(?<![\w]){re.escape(root)}s?(?![\w])", normalized)
            )
        else:
            matched = bool(
                re.search(rf"(?<![\w]){re.escape(term)}(?![\w])", normalized)
            )
        matches += int(matched)
    return min(1.0, matches / max(1, min(3, len(terms))))


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _component(
    key: str,
    label: str,
    score: float,
    weight: float,
    reason: str,
) -> AccountScoreComponent:
    return AccountScoreComponent(
        key=key,
        label=label,
        score=max(0, min(100, round(score))),
        weight=weight,
        reason=reason,
    )


def score_x_account(
    account: XAccount,
    *,
    query: str,
    domain_posts: list[XTweet],
    recent_posts: list[XTweet],
    observed_at: datetime,
) -> ScoredAccount:
    """Return an explainable, platform-local score for the internal X workflow."""

    terms = domain_terms(query)
    profile_text = f"{account.name or ''} {account.description or ''}"
    profile_match = text_relevance(profile_text, terms)
    unique_posts = {
        post.id: post for post in [*domain_posts, *recent_posts] if post.author_id == account.id
    }
    relevant_posts = [
        post for post in unique_posts.values() if text_relevance(post.text, terms) >= 0.34
    ]
    recent_relevant = [
        post for post in recent_posts if text_relevance(post.text, terms) >= 0.34
    ]

    content_ratio = len(recent_relevant) / max(1, len(recent_posts))
    evidence_depth = min(1.0, len(relevant_posts) / 4)
    relevance = 100 * (
        0.25 * profile_match + 0.50 * content_ratio + 0.25 * evidence_depth
    )
    if domain_posts:
        relevance = max(relevance, 40 + 25 * evidence_depth)

    authority = 70 * min(
        1.0, math.log1p(account.followers_count) / math.log1p(1_000_000)
    ) + 30 * min(
        1.0, math.log1p(account.listed_count) / math.log1p(10_000)
    )

    engagements = [post.engagement for post in unique_posts.values()]
    median_engagement = float(median(engagements)) if engagements else 0.0
    absolute_engagement = min(
        1.0, math.log1p(median_engagement) / math.log1p(5_000)
    )
    engagement_rate = 0.0
    if account.followers_count and median_engagement:
        engagement_rate = median_engagement / account.followers_count
        rate_quality = min(
            1.0, math.log1p(engagement_rate * 1_000) / math.log1p(50)
        )
        engagement = 100 * (0.60 * absolute_engagement + 0.40 * rate_quality)
    else:
        engagement = 60 * absolute_engagement

    consistency = 100 * (
        0.45 * min(1.0, len(relevant_posts) / 5)
        + 0.35 * content_ratio
        + 0.20 * min(1.0, len(recent_posts) / 5)
    )
    published = [
        value for value in (_parse_time(post.created_at) for post in recent_posts) if value
    ]
    if published:
        age_days = max(0.0, (observed_at - max(published)).total_seconds() / 86_400)
        freshness = 100 if age_days <= 1 else 85 if age_days <= 7 else 60 if age_days <= 30 else 25
    else:
        freshness = 20

    credibility = 45.0
    credibility_reasons: list[str] = []
    if account.description:
        credibility += 10
        credibility_reasons.append("complete profile")
    if account.verified:
        credibility += 10
        credibility_reasons.append("verified account")
    if account.tweet_count >= 100:
        credibility += 10
        credibility_reasons.append("established posting history")
    elif account.tweet_count >= 20:
        credibility += 5
        credibility_reasons.append("visible posting history")
    account_created = _parse_time(account.created_at)
    if account_created:
        account_age_days = max(
            0.0, (observed_at - account_created).total_seconds() / 86_400
        )
        if account_age_days >= 365:
            credibility += 15
            credibility_reasons.append("account older than one year")
        elif account_age_days >= 90:
            credibility += 8
            credibility_reasons.append("account older than 90 days")
        elif account_age_days < 30:
            credibility -= 20
            credibility_reasons.append("very new account")
    if (
        account.following_count >= 5_000
        and account.followers_count / max(1, account.following_count) < 0.05
    ):
        credibility -= 20
        credibility_reasons.append("suspicious follower/following ratio")
    if account.protected:
        credibility -= 40
        credibility_reasons.append("protected account")
    credibility = max(0, min(100, credibility))

    components = (
        _component(
            "relevance",
            "Relevance",
            relevance,
            0.35,
            f"{len(relevant_posts)} domain-matching posts and profile match {profile_match:.0%}",
        ),
        _component(
            "authority",
            "Authority",
            authority,
            0.15,
            f"{account.followers_count:,} followers and {account.listed_count:,} list memberships",
        ),
        _component(
            "engagement",
            "Engagement",
            engagement,
            0.20,
            f"Median public engagement {median_engagement:,.0f}; rate {engagement_rate:.2%}",
        ),
        _component(
            "consistency",
            "Consistency",
            consistency,
            0.15,
            f"{len(recent_posts)} recent posts sampled; {len(recent_relevant)} match the domain",
        ),
        _component(
            "freshness",
            "Freshness",
            freshness,
            0.10,
            "Based on the newest sampled public post",
        ),
        _component(
            "credibility",
            "Credibility",
            credibility,
            0.05,
            ", ".join(credibility_reasons) or "Limited public account-history signals",
        ),
    )
    score = round(sum(item.score * item.weight for item in components))
    if len(relevant_posts) < 2:
        score = min(score, 64)
    if len(recent_posts) < 3 or credibility < 30:
        score = min(score, 69)
    if account.protected:
        score = min(score, 39)

    evidence: list[AccountEvidence] = []
    if profile_match:
        evidence.append(
            AccountEvidence(
                kind="profile",
                summary=f"Profile matches {', '.join(terms[:3])}",
                url=f"https://x.com/{account.handle}",
                observed_at=observed_at,
            )
        )
    for post in sorted(relevant_posts, key=lambda value: value.engagement, reverse=True)[:4]:
        excerpt = " ".join(post.text.split())[:220]
        evidence.append(
            AccountEvidence(
                kind="domain_content",
                summary=excerpt,
                native_content_id=post.id,
                url=post.url,
                observed_at=observed_at,
            )
        )
    if recent_posts:
        evidence.append(
            AccountEvidence(
                kind="recent_activity",
                summary=f"Sampled {len(recent_posts)} recent public posts",
                url=f"https://x.com/{account.handle}",
                observed_at=observed_at,
            )
        )

    confidence = (
        "High"
        if len(relevant_posts) >= 3 and len(recent_posts) >= 3 and credibility >= 40
        else "Medium"
        if relevant_posts
        else "Low"
    )
    return ScoredAccount(
        score=max(0, min(100, score)),
        confidence=confidence,
        components=components,
        evidence=tuple(evidence),
    )


__all__ = ["SCORE_VERSION", "ScoredAccount", "domain_terms", "score_x_account"]
