from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from statistics import median

from kol_search.backend.models import AccountEvidence, AccountScoreComponent
from kol_search.twitter.models import XAccount, XTweet


SCORE_VERSION = "x-kol-internal-v1"
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
    evidence_depth = min(1.0, len(relevant_posts) / 3)
    relevance = 100 * (
        0.30 * profile_match + 0.45 * content_ratio + 0.25 * evidence_depth
    )
    if domain_posts:
        relevance = max(relevance, 45 + 20 * evidence_depth)

    authority = 80 * min(
        1.0, math.log1p(account.followers_count) / math.log1p(1_000_000)
    ) + 20 * min(
        1.0, math.log1p(account.listed_count) / math.log1p(10_000)
    )

    engagements = [post.engagement for post in unique_posts.values()]
    median_engagement = float(median(engagements)) if engagements else 0.0
    engagement = 100 * min(
        1.0, math.log1p(median_engagement) / math.log1p(10_000)
    )
    if account.followers_count and median_engagement:
        engagement_rate = median_engagement / account.followers_count
        engagement = min(100, engagement + min(20, engagement_rate * 2_000))

    consistency = min(100, len(relevant_posts) * 22 + len(recent_posts) * 8)
    published = [
        value for value in (_parse_time(post.created_at) for post in recent_posts) if value
    ]
    if published:
        age_days = max(0.0, (observed_at - max(published)).total_seconds() / 86_400)
        freshness = 100 if age_days <= 1 else 85 if age_days <= 7 else 60 if age_days <= 30 else 25
    else:
        freshness = 20

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
            0.20,
            f"{account.followers_count:,} followers and {account.listed_count:,} list memberships",
        ),
        _component(
            "engagement",
            "Engagement",
            engagement,
            0.20,
            f"Median public engagement {median_engagement:,.0f} in the observed sample",
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
    )
    score = round(sum(item.score * item.weight for item in components))
    if len(relevant_posts) < 2:
        score = min(score, 64)
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
        if len(relevant_posts) >= 3 and len(recent_posts) >= 3
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
