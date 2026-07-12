from __future__ import annotations

import re
from collections import Counter

from kol_search.discovery.query import all_aliases
from kol_search.models import Account, AccountEnrichment, DomainConfig, Post


TYPE_TERMS: dict[str, tuple[str, ...]] = {
    "media": ("media", "news", "daily", "magazine", "coindesk", "cointelegraph", "媒体", "资讯", "新闻"),
    "fund": ("capital", "ventures", "fund", "investment", "vc", "基金", "创投", "资本"),
    "project": ("protocol", "network", "foundation", "labs", "dao", "official", "协议", "基金会", "官方"),
    "company": ("company", "exchange", "platform", "analytics", "studio", "交易所", "平台", "公司"),
    "community": ("community", "club", "collective", "社区", "社群"),
}

PERSON_TERMS = (
    "founder", "co-founder", "researcher", "analyst", "trader", "investor", "writer", "host",
    "创始人", "研究员", "分析师", "交易员", "投资人", "作者", "主持人", "个人观点",
)


def detect_languages(text: str) -> list[str]:
    languages: list[str] = []
    if re.search(r"[\u4e00-\u9fff]", text):
        languages.append("zh")
    if re.search(r"[A-Za-z]", text):
        languages.append("en")
    return languages or ["unknown"]


def classify_account(account: Account, posts: list[Post], config: DomainConfig) -> AccountEnrichment:
    bio = account.description or ""
    text = f"{account.name or ''} {account.username} {bio} " + " ".join(p.text for p in posts[:10])
    lower = text.lower()

    counts = Counter(
        {
            account_type: sum(lower.count(term) for term in terms)
            for account_type, terms in TYPE_TERMS.items()
        }
    )
    person_hits = sum(lower.count(term) for term in PERSON_TERMS)
    best_type, best_hits = counts.most_common(1)[0] if counts else ("unknown", 0)
    if person_hits >= best_hits and person_hits > 0:
        account_type = "person"
    elif best_hits > 0:
        account_type = best_type
    elif account.name and not re.search(r"\b(official|labs|capital|foundation|media)\b", lower):
        account_type = "person"
    else:
        account_type = "unknown"

    topics: list[str] = []
    for topic, aliases in all_aliases(config).items():
        if any(alias.lower() in lower for alias in aliases):
            topics.append(topic)

    confidence = min(1.0, 0.45 + 0.1 * max(person_hits, best_hits))
    summary = bio.strip()[:280] or None
    return AccountEnrichment(
        account_type=account_type,  # type: ignore[arg-type]
        languages=detect_languages(text),
        topics=topics,
        summary=summary,
        confidence=confidence,
        source="rules",
    )


def profile_completeness(account: Account) -> float:
    checks = [
        bool(account.name),
        bool(account.description),
        bool(account.profile_image_url),
        bool(account.url or account.entities),
        bool(account.location),
    ]
    return sum(checks) / len(checks)


def spam_penalty(account: Account, posts: list[Post]) -> float:
    penalty = 0.0
    if account.followers_count < 100 and account.following_count > 2000:
        penalty += 0.12
    if account.following_count > max(5000, account.followers_count * 5):
        penalty += 0.08
    texts = [re.sub(r"\s+", " ", post.text.lower()).strip() for post in posts if post.text]
    if len(texts) >= 4 and len(set(texts)) / len(texts) < 0.5:
        penalty += 0.12
    if account.protected:
        penalty += 0.04
    return min(0.25, penalty)

