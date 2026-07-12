from __future__ import annotations

import math
import statistics
from datetime import datetime, timezone

from kol_search.models import Account, Post, ScoreComponents


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        # handle Z
        v = value.replace("Z", "+00:00")
        return datetime.fromisoformat(v)
    except ValueError:
        return None


def engagement_rate(account: Account, posts: list[Post]) -> tuple[float, float]:
    """
    Returns (engagement_rate, avg_engagement).
    rate ≈ avg_engagement / max(followers, 1), capped.
    """
    if not posts:
        return 0.0, 0.0
    avg = statistics.median(p.engagement for p in posts)
    rate = avg / max(account.followers_count, 1)
    return float(min(rate, 0.5)), float(avg)


def recency_score(posts: list[Post], now: datetime | None = None) -> float:
    """1.0 if posted in last day, decays over ~14 days."""
    now = now or datetime.now(timezone.utc)
    latest: datetime | None = None
    for p in posts:
        ts = _parse_ts(p.created_at)
        if ts is None:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if latest is None or ts > latest:
            latest = ts
    if latest is None:
        return 0.2  # unknown
    days = max(0.0, (now - latest).total_seconds() / 86400.0)
    return float(max(0.0, 1.0 - days / 14.0))


def score_candidate(
    account: Account,
    posts: list[Post],
    domain_score: float,
    is_seed: bool,
    seed_proximity: float,
    weights: dict[str, float],
    max_followers_ref: float = 5_000_000,
    completeness_score: float = 0.0,
    spam_penalty: float = 0.0,
) -> tuple[ScoreComponents, float, float]:
    """
    Explainable weighted score.
    seed_proximity: 0..1 (1 = seed or direct mention from seed).
    """
    eng_rate, eng_avg = engagement_rate(account, posts)
    followers_norm = math.log1p(account.followers_count) / math.log1p(max_followers_ref)
    followers_norm = float(min(1.0, max(0.0, followers_norm)))
    eng_norm = float(min(1.0, eng_rate / 0.05))  # 5% eng rate ~ full marks
    rec = recency_score(posts)
    dom = float(min(1.0, max(0.0, domain_score)))
    prox = float(min(1.0, max(0.0, seed_proximity)))
    if is_seed:
        prox = max(prox, 1.0)

    w_f = weights.get("followers", 0.15)
    w_e = weights.get("engagement", 0.25)
    w_r = weights.get("recency", 0.1)
    w_d = weights.get("domain", 0.35)
    w_s = weights.get("seed_proximity", 0.1)
    w_c = weights.get("completeness", 0.05)

    total = (
        w_f * followers_norm
        + w_e * eng_norm
        + w_r * rec
        + w_d * dom
        + w_s * prox
        + w_c * max(0.0, min(1.0, completeness_score))
    )
    total = max(0.0, total - max(0.0, spam_penalty))
    components = ScoreComponents(
        followers=w_f * followers_norm,
        engagement=w_e * eng_norm,
        recency=w_r * rec,
        domain=w_d * dom,
        seed_proximity=w_s * prox,
        completeness=w_c * max(0.0, min(1.0, completeness_score)),
        spam_penalty=max(0.0, spam_penalty),
        total=float(total),
    )
    return components, eng_rate, eng_avg
