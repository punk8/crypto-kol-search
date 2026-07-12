from __future__ import annotations

import re

from kol_search.models import Account, Post


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower()).strip()


def domain_relevance_score(
    account: Account,
    recent_posts: list[Post],
    lexicon: list[str],
) -> float:
    """
    Keyword / lexicon overlap in bio + recent tweets.
    Returns 0..1 (soft capped).
    """
    if not lexicon:
        return 0.0

    bio = _normalize(account.description or "")
    tweet_blob = _normalize(" ".join(p.text for p in recent_posts))
    combined = f"{bio} {tweet_blob}".strip()
    if not combined:
        return 0.0

    hits = 0
    weighted = 0.0
    for term in lexicon:
        t = term.lower().strip()
        if not t:
            continue
        # word-ish match for short tickers; substring for multi-word
        if " " in t or len(t) > 4:
            pattern = re.escape(t)
        else:
            pattern = rf"(?<![a-z0-9]){re.escape(t)}(?![a-z0-9])"
        n = len(re.findall(pattern, combined, flags=re.IGNORECASE))
        if n:
            hits += 1
            # bio hits count more
            bio_n = len(re.findall(pattern, bio, flags=re.IGNORECASE))
            weighted += min(3, n) * (1.5 if bio_n else 1.0)

    if hits == 0:
        return 0.0

    # Normalize: more unique terms + density
    unique_ratio = hits / max(1, min(len(lexicon), 25))
    density = min(1.0, weighted / 12.0)
    score = 0.55 * unique_ratio + 0.45 * density
    return float(min(1.0, score))


def passes_domain_filter(
    domain_score: float,
    followers: int,
    min_domain_score: float,
    min_followers: int,
) -> bool:
    return followers >= min_followers and domain_score >= min_domain_score
