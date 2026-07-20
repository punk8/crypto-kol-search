from __future__ import annotations

import math
import re
from collections import defaultdict
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any, Callable

from kol_search.db import Store, utc_now
from kol_search.models import (
    Account,
    AccountEnrichment,
    Candidate,
    DiscoveryEdge,
    Post,
    ScoreComponents,
)
from kol_search.platforms import create_platform_readers
from kol_search.platforms.base import PlatformReader
from kol_search.settings import Settings


ProgressCallback = Callable[[int, str, dict, list[str]], None]

QUERY_EXPANSIONS: dict[str, list[str]] = {
    "rwa": ["RWA", "real world assets", "tokenization", "现实世界资产", "资产代币化"],
    "加密货币": ["加密货币", "crypto", "区块链", "web3", "数字资产"],
    "crypto": ["crypto", "cryptocurrency", "加密货币", "blockchain", "web3"],
    "ai": ["AI", "artificial intelligence", "人工智能", "大模型", "AI agent"],
}


def expand_query(query: str) -> list[str]:
    clean = query.strip()
    values = [clean]
    lower = clean.lower()
    for key, aliases in QUERY_EXPANSIONS.items():
        if key in lower or lower in key:
            values.extend(aliases)
    return list(dict.fromkeys(value for value in values if value))[:5]


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _age_days(post: Post) -> float | None:
    parsed = _parse_datetime(post.created_at)
    if not parsed:
        return None
    return max(0.0, (datetime.now(timezone.utc) - parsed).total_seconds() / 86400)


def select_window(posts: list[Post], target: int) -> tuple[list[Post], int, int]:
    """Prefer 24h, then 7d and 30d; undated rows never satisfy a strict window."""
    unique = {post.id: post for post in posts if post.id and post.url}
    ordered = sorted(
        unique.values(),
        key=lambda post: (_parse_datetime(post.created_at) or datetime.min.replace(tzinfo=timezone.utc)),
        reverse=True,
    )
    undated = sum(1 for post in ordered if _age_days(post) is None)
    for days in (1, 7, 30):
        selected = [
            post for post in ordered
            if (age := _age_days(post)) is not None and age <= days
        ]
        if len(selected) >= target or days == 30:
            return selected, days, undated
    return [], 30, undated


def _relevance(text: str, aliases: list[str]) -> float:
    lower = text.lower()
    matched = sum(1 for alias in aliases if alias.lower() in lower)
    return min(1.0, matched / max(1, min(3, len(aliases))))


def _language(text: str) -> str:
    return "zh" if re.search(r"[\u4e00-\u9fff]", text) else "en"


def classify_direction(text: str, categories: list[dict[str, Any]]) -> str:
    lower = text.lower()
    scores = {
        category["slug"]: sum(
            1 for keyword in category.get("keywords", []) if keyword.lower() in lower
        )
        for category in categories
    }
    winner = max(scores, key=scores.get, default="other")
    return winner if scores.get(winner, 0) else "other"


class MultiPlatformDiscoveryPipeline:
    def __init__(self, store: Store, settings: Settings) -> None:
        self.store = store
        self.settings = settings

    def run(self, run: dict, progress: ProgressCallback | None = None) -> list[Candidate]:
        requested = tuple(run.get("config", {}).get("platforms") or ("x", "xiaohongshu"))
        readers = create_platform_readers(
            self.settings,
            x_backend=run.get("backend"),
            resolve_account_id=self.store.resolve_account_id,
            platforms=requested,
        )
        warnings: list[str] = []
        stats: dict[str, Any] = {"platforms": {}, "queries": 0, "posts": 0, "undated": 0}

        def report(percent: int, phase: str) -> None:
            if progress:
                progress(percent, phase, stats, list(dict.fromkeys(warnings)))

        try:
            report(5, "platform_health")
            aliases = expand_query(run.get("query") or "crypto")
            target = max(1, min(int(run.get("result_limit") or 20), 50))
            all_candidates: list[Candidate] = []
            all_posts: list[Post] = []
            for index, (platform, reader) in enumerate(readers.items(), 1):
                health = reader.health_check()
                platform_stats: dict[str, Any] = {
                    "ready": health.ready,
                    "account": health.account,
                    "window_days": 30,
                    "candidates": 0,
                }
                stats["platforms"][platform] = platform_stats
                if not health.ready:
                    warnings.append(f"{platform}: {health.detail or '登录状态不可用'}")
                    continue
                posts: list[Post] = []
                for alias_index, alias in enumerate(aliases, 1):
                    try:
                        posts.extend(reader.search_posts(alias, max(20, target * 3)))
                        stats["queries"] += 1
                    except Exception as exc:
                        warnings.append(f"{platform} search {alias!r}: {exc}")
                    platform_stats["queries_complete"] = alias_index
                    report(
                        8 + int(
                            ((index - 1) + alias_index / max(1, len(aliases)))
                            / max(1, len(readers))
                            * 62
                        ),
                        f"searching_{platform}",
                    )
                relevant_posts = [
                    post for post in posts if _relevance(post.text, aliases) > 0
                ]
                selected, window_days, undated = select_window(relevant_posts, target)
                platform_stats["window_days"] = window_days
                platform_stats["raw_posts"] = len(posts)
                platform_stats["selected_posts"] = len(selected)
                stats["undated"] += undated
                all_posts.extend(selected)
                candidates = self._candidates(
                    platform, reader, selected, aliases, max_candidates=target
                )
                candidates.sort(key=lambda candidate: candidate.score.total, reverse=True)
                candidates = candidates[:target]
                for rank, candidate in enumerate(candidates, 1):
                    candidate.rank = rank
                platform_stats["candidates"] = len(candidates)
                if len(candidates) < target:
                    warnings.append(
                        f"{platform}: 30 天内仅找到 {len(candidates)}/{target} 个可追溯 KOL"
                    )
                all_candidates.extend(candidates)
                report(10 + int(index / max(1, len(readers)) * 70), f"discovering_{platform}")
            self.store.save_candidates(int(run["id"]), all_candidates)
            self.store.save_posts(all_posts)
            stats["posts"] = len({post.id for post in all_posts})
            report(98, "persisting")
            return all_candidates
        finally:
            for reader in readers.values():
                reader.close()

    def _candidates(
        self,
        platform: str,
        reader: PlatformReader,
        posts: list[Post],
        aliases: list[str],
        max_candidates: int,
    ) -> list[Candidate]:
        grouped: dict[str, list[Post]] = defaultdict(list)
        for post in posts:
            grouped[post.author_id or post.author_username or "unknown"].append(post)
        candidates: list[Candidate] = []
        max_engagement = max((sum(post.engagement for post in rows) for rows in grouped.values()), default=1)
        max_count = max((len(rows) for rows in grouped.values()), default=1)
        preliminary = sorted(
            grouped.items(),
            key=lambda item: (
                len(item[1]),
                sum(post.engagement for post in item[1]),
                max((_parse_datetime(post.created_at) or datetime.min.replace(tzinfo=timezone.utc)) for post in item[1]),
            ),
            reverse=True,
        )[:max_candidates]
        for account_key, rows in preliminary:
            account: Account | None = None
            try:
                if platform == "xiaohongshu" and hasattr(reader, "_account"):
                    raw = rows[0].raw.get("opencli") or {}
                    account = reader._account(raw)  # type: ignore[attr-defined]
                else:
                    account = reader.get_account(rows[0].author_username or account_key)
            except Exception:
                account = None
            if account is None:
                account = Account(
                    id=account_key,
                    platform=platform,  # type: ignore[arg-type]
                    external_id=account_key.split(":", 1)[-1],
                    source_provider=rows[0].source_provider,
                    captured_at=rows[0].captured_at,
                    username=rows[0].author_username or account_key,
                    name=rows[0].author_username,
                    url=(
                        f"https://x.com/{rows[0].author_username}"
                        if platform == "x" and rows[0].author_username
                        else None
                    ),
                )
            text = "\n".join(post.text for post in rows)
            relevance = _relevance(text, aliases)
            engagement_raw = sum(post.engagement for post in rows)
            engagement = math.log1p(engagement_raw) / math.log1p(max_engagement)
            activity = len(rows) / max_count
            ages = [age for post in rows if (age := _age_days(post)) is not None]
            recency = 1.0 / (1.0 + min(ages, default=30) / 7)
            completeness = sum(
                bool(value)
                for value in (account.name, account.description, account.url, account.followers_count)
            ) / 4
            total = 0.4 * relevance + 0.25 * engagement + 0.2 * recency + 0.1 * activity + 0.05 * completeness
            candidates.append(
                Candidate(
                    account=account,
                    domain_score=relevance,
                    engagement_rate=engagement,
                    recent_engagement_avg=engagement_raw / max(1, len(rows)),
                    score=ScoreComponents(
                        followers=min(1.0, math.log1p(account.followers_count) / math.log1p(1_000_000)),
                        engagement=engagement,
                        recency=recency,
                        domain=relevance,
                        completeness=completeness,
                        total=total,
                    ),
                    edges=[
                        DiscoveryEdge(
                            target_username=account.username,
                            source_type=f"{platform}_search",
                            evidence=f"{len(rows)} recent matching posts",
                            query=", ".join(aliases[:3]),
                            weight=relevance,
                        )
                    ],
                    recent_tweets_sample=[post.text for post in rows[:3]],
                    enrichment=AccountEnrichment(
                        account_type="person",
                        languages=sorted({_language(post.text) for post in rows}),
                        topics=[aliases[0]],
                        summary=f"{len(rows)} 条近期相关内容，平台内互动 {engagement_raw}",
                        relevance_score=relevance,
                        confidence=min(1.0, 0.4 + len(rows) * 0.1),
                        source="rules",
                    ),
                )
            )
        return candidates


class MultiPlatformTrendService:
    cache_key = "multiplatform:trends:v1"

    def __init__(self, store: Store, settings: Settings) -> None:
        self.store = store
        self.settings = settings

    def refresh(self, *, force: bool = False, x_backend: str | None = None) -> dict[str, Any]:
        if not force:
            cached = self.store.get_api_cache(
                self.cache_key, max(60, self.settings.trend_cache_minutes * 60)
            )
            if cached is not None:
                return {**cached, "cached": True}
        readers = create_platform_readers(
            self.settings,
            x_backend=x_backend,
            resolve_account_id=self.store.resolve_account_id,
        )
        categories = self.store.list_trend_categories()
        warnings: list[str] = []
        clusters: list[int] = []
        platform_windows: dict[str, int] = {}
        try:
            for platform, reader in readers.items():
                health = reader.health_check()
                if not health.ready:
                    warnings.append(f"{platform}: {health.detail or '未登录'}")
                    continue
                try:
                    native_names = {trend.name.lower() for trend in reader.get_trends(20)}
                except Exception as exc:
                    native_names = set()
                    warnings.append(f"{platform} native trends: {exc}")
                platform_posts: list[Post] = []
                for category in categories:
                    keywords = category.get("keywords", [])
                    query = (
                        " OR ".join(keywords[:5])
                        if platform == "x"
                        else " ".join(keywords[:3])
                    )
                    try:
                        posts = reader.search_posts(query, 40)
                    except Exception as exc:
                        warnings.append(f"{platform} {category['slug']}: {exc}")
                        continue
                    posts = [
                        post
                        for post in posts
                        if classify_direction(post.text, categories) == category["slug"]
                    ]
                    selected, window_days, undated = select_window(posts, 20)
                    selected = selected[:20]
                    platform_windows[platform] = max(platform_windows.get(platform, 1), window_days)
                    if undated:
                        warnings.append(f"{platform} {category['slug']}: 忽略 {undated} 条无发布时间内容")
                    if not selected:
                        continue
                    platform_posts.extend(selected)
                    self._ensure_accounts(platform, selected)
                    self.store.save_posts(selected)
                    engagement = sum(post.engagement for post in selected)
                    authors = {post.author_id for post in selected}
                    top = max(selected, key=lambda post: post.engagement)
                    title = (top.text.splitlines() or [category["display_name"]])[0][:80]
                    fingerprint = sha256(
                        f"{platform}:{category['slug']}:{title.lower()}".encode("utf-8")
                    ).hexdigest()
                    heat = min(
                        100.0,
                        20 + len(selected) * 2 + math.log1p(engagement) * 5,
                    )
                    native = any(name in title.lower() or title.lower() in name for name in native_names)
                    cluster_id = self.store.upsert_topic_cluster(
                        fingerprint=fingerprint,
                        title=title,
                        summary=f"{category['display_name']} · {platform} · 最近 {window_days} 天真实内容",
                        language=_language("\n".join(post.text for post in selected)),
                        lifecycle="breakout" if heat >= 80 else ("rising" if heat >= 50 else "emerging"),
                        heat_score=heat,
                        metrics={
                            "post_count": len(selected),
                            "unique_authors": len(authors),
                            "engagement_total": engagement,
                            "kol_count": sum(1 for post in selected if post.engagement >= 10),
                            "growth_6h": 0,
                            "platform": platform,
                            "window_days": window_days,
                            "captured_at": utc_now(),
                        },
                        outline="",
                        draft="",
                        draft_source="rules",
                        native_trend=native,
                        direction=category["slug"],
                        post_ids=[(post.id, _relevance(post.text, keywords)) for post in selected[:20]],
                        replace_posts=True,
                    )
                    clusters.append(cluster_id)
            payload = {
                "cluster_ids": clusters,
                "platform_windows": platform_windows,
                "warnings": list(dict.fromkeys(warnings)),
                "captured_at": utc_now(),
                "cached": False,
            }
            self.store.set_api_cache(self.cache_key, payload)
            return payload
        finally:
            for reader in readers.values():
                reader.close()

    def cached(self) -> dict[str, Any] | None:
        return self.store.get_api_cache(
            self.cache_key, max(60, self.settings.trend_cache_minutes * 60)
        )

    def _ensure_accounts(self, platform: str, posts: list[Post]) -> None:
        for post in posts:
            if not post.author_id:
                continue
            self.store.upsert_account(
                Account(
                    id=post.author_id,
                    platform=platform,  # type: ignore[arg-type]
                    external_id=post.author_id.split(":", 1)[-1],
                    source_provider=post.source_provider,
                    captured_at=post.captured_at,
                    username=post.author_username or post.author_id,
                    name=post.author_username,
                    url=(
                        f"https://x.com/{post.author_username}"
                        if platform == "x" and post.author_username
                        else None
                    ),
                    raw={"derived_from_post": post.id},
                ).model_dump(),
                {"account_type": "person", "languages": [_language(post.text)]},
            )
