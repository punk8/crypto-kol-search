from __future__ import annotations

import csv
import hashlib
import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

from kol_search.config_loader import load_domain_config
from kol_search.contacts import PublicContactCrawler
from kol_search.db import Store
from kol_search.discovery.classify import classify_account, profile_completeness, spam_penalty
from kol_search.discovery.filter_domain import domain_relevance_score
from kol_search.discovery.query import all_aliases
from kol_search.discovery.score import score_candidate
from kol_search.models import Account, Candidate, DiscoveryEdge, Post
from kol_search.settings import PROJECT_ROOT, Settings
from kol_search.twitter.base import TwitterBackendError, TwitterClient, merge_client_diagnostics
from kol_search.twitter.factory import create_twitter_client


ProgressCallback = Callable[[int, str, dict, list[str]], None]

TOPIC_QUOTAS = {
    "bitcoin": 9,
    "ethereum_l2": 11,
    "solana": 8,
    "defi": 12,
    "stablecoin_rwa": 8,
    "nft_gamefi": 6,
    "memecoin": 5,
    "airdrop": 5,
    "onchain_security": 10,
    "trading_macro": 10,
    "regulation": 7,
    "infra_ai_depin": 9,
}
LANGUAGE_QUOTAS = {"en": 70, "zh": 20, "bilingual": 10}
TYPE_QUOTAS = {"person": 80, "organization": 20}
INSTITUTION_QUOTAS = {"media": 6, "research_data": 6, "fund": 4, "protocol_ecosystem": 4}
SEED_QUOTAS = {
    "topics": TOPIC_QUOTAS,
    "languages": LANGUAGE_QUOTAS,
    "account_types": TYPE_QUOTAS,
    "institutions": INSTITUTION_QUOTAS,
}

EDGE_WEIGHTS = {
    "mention": 1.0,
    "following": 0.75,
    "verified_follower": 0.45,
    "topic_search": 0.25,
}

ENDPOINT_UNIT_COST = {
    "profile": 0.00018,
    "timeline": 0.00015,
    "followings": 0.00018,
    "verified_followers": 0.00018,
    "user_search": 0.00018,
}


class BudgetExceeded(RuntimeError):
    pass


@dataclass
class CallBudget:
    hard_limit_usd: float = 5.0
    optional_stop_ratio: float = 0.8
    estimated_usd: float = 0.0
    endpoint_calls: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    returned_items: dict[str, int] = field(default_factory=lambda: defaultdict(int))

    @property
    def optional_allowed(self) -> bool:
        return self.estimated_usd < self.hard_limit_usd * self.optional_stop_ratio

    def record(self, endpoint: str, returned_items: int) -> None:
        projected = self.estimated_usd + ENDPOINT_UNIT_COST.get(endpoint, 0) * max(
            1, returned_items
        )
        if projected > self.hard_limit_usd:
            raise BudgetExceeded(f"任务达到 ${self.hard_limit_usd:.2f} 硬预算")
        self.endpoint_calls[endpoint] += 1
        self.returned_items[endpoint] += returned_items
        self.estimated_usd = projected

    def stats(self) -> dict:
        return {
            "hard_budget_usd": self.hard_limit_usd,
            "estimated_usd": round(self.estimated_usd, 6),
            "endpoint_calls": dict(self.endpoint_calls),
            "returned_items": dict(self.returned_items),
        }


@dataclass
class ExpansionCandidate:
    account: Account | None = None
    usernames: set[str] = field(default_factory=set)
    edges: list[tuple[str | None, str, str, float, str | None]] = field(default_factory=list)

    @property
    def network_seed_count(self) -> int:
        return len({source for source, kind, *_ in self.edges if source and kind != "topic_search"})

    @property
    def network_weight(self) -> float:
        per_seed: dict[str, float] = {}
        for source, _kind, _evidence, weight, _url in self.edges:
            if source:
                per_seed[source] = max(per_seed.get(source, 0), weight)
        return min(1.0, sum(per_seed.values()) / 3.0)

    @property
    def has_network_edge(self) -> bool:
        return any(kind != "topic_search" for _, kind, *_ in self.edges)


def load_seed_catalog(path: Path | None = None) -> list[dict[str, str]]:
    path = path or PROJECT_ROOT / "seeds" / "crypto_seed_library.csv"
    with path.open(encoding="utf-8", newline="") as file:
        return list(csv.DictReader(file))


def language_bucket(value: str | Iterable[str]) -> str:
    values = set(value.split("|") if isinstance(value, str) else value)
    if {"en", "zh"}.issubset(values):
        return "bilingual"
    return "zh" if "zh" in values else "en"


def type_bucket(account_type: str) -> str:
    return "person" if account_type == "person" else "organization"


def balanced_seed_sample(members: list[dict], limit: int) -> list[dict]:
    """Round-robin topics so relationship calls do not overrepresent early catalog rows."""
    grouped: dict[str, list[dict]] = defaultdict(list)
    for member in sorted(members, key=lambda item: item.get("rank") or 9999):
        grouped[member["primary_topic"]].append(member)
    selected: list[dict] = []
    topics = list(TOPIC_QUOTAS)
    while len(selected) < limit and any(grouped.values()):
        for topic in topics:
            if grouped[topic] and len(selected) < limit:
                selected.append(grouped[topic].pop(0))
    return selected


def related_usernames(post: Post) -> set[str]:
    """Extract mention, reply target, and quoted author handles from normalized/vendor data."""
    names = {name.lstrip("@").lower() for name in post.mentioned_usernames if name}
    raw = post.raw
    for key in (
        "inReplyToUsername",
        "in_reply_to_username",
        "in_reply_to_screen_name",
        "replyToUsername",
    ):
        value = raw.get(key)
        if isinstance(value, str) and value:
            names.add(value.lstrip("@").lower())
    for key in ("quotedTweet", "quoted_tweet", "quoted_status"):
        quoted = raw.get(key)
        if not isinstance(quoted, dict):
            continue
        author = quoted.get("author") or quoted.get("user") or {}
        if isinstance(author, dict):
            value = author.get("userName") or author.get("username") or author.get("screen_name")
            if isinstance(value, str) and value:
                names.add(value.lstrip("@").lower())
    return names


def validate_seed_quotas(rows: list[dict[str, str]]) -> list[str]:
    errors: list[str] = []
    if len(rows) != 100:
        errors.append(f"approved catalog must contain 100 rows, got {len(rows)}")
    unique_handles = {row.get("handle", "").lower() for row in rows}
    if len(unique_handles) != len(rows):
        errors.append("catalog contains duplicate handles")
    actual_topics = defaultdict(int)
    actual_languages = defaultdict(int)
    actual_types = defaultdict(int)
    actual_institutions = defaultdict(int)
    for row in rows:
        actual_topics[row.get("primary_topic") or (row.get("primary_topics") or "").split("|")[0]] += 1
        actual_languages[language_bucket(row.get("languages") or "en")] += 1
        bucket = type_bucket(row.get("account_type") or "unknown")
        actual_types[bucket] += 1
        if bucket == "organization":
            actual_institutions[row.get("institution_kind") or ""] += 1
    for label, expected, actual in (
        ("topics", TOPIC_QUOTAS, actual_topics),
        ("languages", LANGUAGE_QUOTAS, actual_languages),
        ("account types", TYPE_QUOTAS, actual_types),
        ("institutions", INSTITUTION_QUOTAS, actual_institutions),
    ):
        for key, count in expected.items():
            if actual[key] != count:
                errors.append(f"{label}.{key}: expected {count}, got {actual[key]}")
    return errors


def _mock_account(row: dict[str, str], index: int) -> Account:
    topic = row.get("primary_topic") or (row.get("primary_topics") or "crypto").split("|")[0]
    return Account(
        id=str(9_000_000 + index),
        username=row["handle"],
        name=row.get("name"),
        description=f"Crypto {topic} researcher, analyst and ecosystem contributor",
        followers_count=5_000 + index * 137,
        following_count=400,
        tweet_count=2_000,
        listed_count=30,
        verified=index % 4 == 0,
        created_at="2018-01-01T00:00:00+00:00",
        profile_image_url="https://example.test/avatar.png",
        url=f"https://x.com/{row['handle']}",
        location="Global",
    )


def _mock_posts(account: Account, topic: str) -> list[Post]:
    now = datetime.now(timezone.utc).isoformat()
    return [
        Post(
            id=f"{account.id}-{index}",
            author_id=account.id,
            author_username=account.username,
            text=f"Original analysis about {topic} crypto markets and onchain adoption #{index}",
            created_at=now,
            like_count=50 + index,
            retweet_count=10,
            reply_count=5,
        )
        for index in range(2)
    ]


def seed_eligibility_reason(
    account: Account,
    posts: list[Post],
    topic: str,
    aliases: list[str],
    *,
    specialist: bool = False,
) -> str | None:
    if account.protected:
        return "非公开账号"
    if account.followers_count < (500 if specialist else 1000):
        return "未达到粉丝门槛"
    dated: list[datetime] = []
    for post in posts:
        if not post.created_at:
            continue
        try:
            value = datetime.fromisoformat(post.created_at.replace("Z", "+00:00"))
            dated.append(value if value.tzinfo else value.replace(tzinfo=timezone.utc))
        except ValueError:
            continue
    if not dated or (datetime.now(timezone.utc) - max(dated)).days > 30:
        return "30 天内不活跃"
    terms = {topic.lower().replace("_", " "), *(alias.lower() for alias in aliases)}
    evidence = [post for post in posts if any(term in post.text.lower() for term in terms if term)]
    if len(evidence) < 2:
        return "近期相关内容证据不足 2 条"
    originals = [
        post
        for post in posts
        if not post.text.lstrip().lower().startswith("rt @")
        and not post.raw.get("retweetedTweet")
        and not post.raw.get("retweeted_tweet")
    ]
    if len(originals) / max(1, len(posts)) < 0.4:
        return "原创内容比例低于 40%"
    promotion_terms = ("sponsored", "paid partnership", "use code", "referral", "推广", "返佣")
    promotional = [post for post in posts if any(term in post.text.lower() for term in promotion_terms)]
    if len(promotional) / max(1, len(posts)) > 0.6:
        return "推广内容比例超过 60%"
    if spam_penalty(account, posts) >= 0.2:
        return "疑似机器人、搬运或异常互动"
    return None


class SeedPipeline:
    def __init__(self, store: Store, settings: Settings) -> None:
        self.store = store
        self.settings = settings

    def run(self, run: dict, progress: ProgressCallback | None = None) -> list[Candidate]:
        if run.get("kind") == "seed_build":
            return self._build(run, progress)
        if run.get("kind") == "seed_expand":
            return self._expand(run, progress)
        raise ValueError("Unsupported seed task")

    def _cached_accounts(
        self,
        key: str,
        max_age_seconds: int,
        loader: Callable[[], list[Account]],
        budget: CallBudget,
        endpoint: str,
        stats: dict,
    ) -> list[Account]:
        cached = self.store.get_api_cache(key, max_age_seconds)
        if isinstance(cached, list):
            stats["cache_hits"] = int(stats.get("cache_hits", 0)) + 1
            return [Account.model_validate(item) for item in cached]
        try:
            values = loader()
        except Exception:
            budget.record(endpoint, 0)
            raise
        budget.record(endpoint, len(values))
        self.store.set_api_cache(key, [item.model_dump() for item in values])
        return values

    def _cached_posts(
        self,
        user_id: str,
        loader: Callable[[], list[Post]],
        budget: CallBudget,
        stats: dict,
        backend_name: str = "",
    ) -> list[Post]:
        key = f"{backend_name}:timeline:{user_id}:20"
        cached = self.store.get_api_cache(key, 7 * 86400)
        if isinstance(cached, list):
            stats["cache_hits"] = int(stats.get("cache_hits", 0)) + 1
            return [Post.model_validate(item) for item in cached]
        try:
            values = loader()
        except Exception:
            budget.record("timeline", 0)
            raise
        budget.record("timeline", len(values))
        self.store.set_api_cache(key, [item.model_dump() for item in values])
        return values

    def _crawl_selected_contacts(
        self, candidates: list[Candidate], stats: dict, warnings: list[str], backend: str
    ) -> None:
        if backend == "mock":
            return
        crawler = PublicContactCrawler(
            max_pages=self.settings.max_site_pages,
            cache_get=self.store.get_crawl_cache,
            cache_set=self.store.set_crawl_cache,
        )
        try:
            for candidate in candidates[: self.settings.max_contact_accounts]:
                result = crawler.crawl_account(candidate.account)
                stats["contact_pages"] = int(stats.get("contact_pages", 0)) + result.pages_fetched
                stats["contacts_found"] = int(stats.get("contacts_found", 0)) + len(result.contacts)
                warnings.extend(result.warnings)
                self.store.save_contacts(result.contacts)
        finally:
            crawler.close()

    @staticmethod
    def _report(
        progress: ProgressCallback | None,
        percent: int,
        phase: str,
        stats: dict,
        warnings: list[str],
    ) -> None:
        if progress:
            progress(percent, phase, stats, warnings)

    def _build(self, run: dict, progress: ProgressCallback | None) -> list[Candidate]:
        rows = [row for row in load_seed_catalog() if row.get("status") == "approved"]
        errors = validate_seed_quotas(rows)
        if errors:
            raise RuntimeError("种子目录配额无效：" + "; ".join(errors))
        version = str((run.get("config") or {}).get("version") or datetime.now().strftime("v%Y%m%d"))
        seed_set_id = self.store.create_seed_set(
            name="Crypto KOL 基础种子",
            version=version,
            target_size=100,
            quotas=SEED_QUOTAS,
            status="draft",
            config={"run_id": run["id"], "backend": run["backend"]},
        )
        stats: dict = {"seed_set_id": seed_set_id, "validated": 0}
        warnings: list[str] = []
        budget = CallBudget()
        self._report(progress, 5, "seed_catalog", stats | budget.stats(), warnings)
        client = create_twitter_client(
            run["backend"], self.settings, self.store.resolve_account_id
        )
        validated: list[tuple[dict[str, str], Account, list[Post]]] = []
        config = load_domain_config("crypto")
        aliases_by_topic = all_aliases(config)
        try:
            for index, row in enumerate(rows, 1):
                if run["backend"] == "mock":
                    account = _mock_account(row, index)
                    posts = _mock_posts(account, row["primary_topic"])
                else:
                    accounts = self._cached_accounts(
                        f"{client.name}:profile:{row['handle'].lower()}",
                        7 * 86400,
                        lambda row=row: [value] if (value := client.get_user_by_username(row["handle"])) else [],
                        budget,
                        "profile",
                        stats,
                    )
                    account = accounts[0] if accounts else None
                    if account is None:
                        warnings.append(f"@{row['handle']} 已失效或不存在")
                        continue
                    posts = self._cached_posts(
                        account.id,
                        lambda account=account: client.get_user_tweets(
                            account.id, max_results=20, username=account.username
                        ),
                        budget,
                        stats,
                        client.name,
                    )
                reason = seed_eligibility_reason(
                    account,
                    posts,
                    row["primary_topic"],
                    aliases_by_topic.get(row["primary_topic"], []),
                    specialist=row.get("seed_tier") == "specialist",
                )
                if reason:
                    warnings.append(f"@{row['handle']} {reason}")
                    continue
                validated.append((row, account, posts))
                stats["validated"] = len(validated)
                if index % 5 == 0:
                    self._report(
                        progress,
                        5 + int(index * 0.8),
                        "seed_validation",
                        stats | budget.stats(),
                        warnings,
                    )
            if len(validated) != 100:
                self.store.update_seed_set_status(seed_set_id, "incomplete")
                raise RuntimeError(f"仅核验通过 {len(validated)}/100；未批准部分结果")

            candidates: list[Candidate] = []
            for rank, (row, account, posts) in enumerate(validated, 1):
                enrichment = classify_account(account, posts, config)
                enrichment.account_type = row["account_type"]  # curated type wins
                enrichment.languages = row["languages"].split("|")
                enrichment.topics = [row["primary_topic"]]
                candidate = Candidate(account=account, enrichment=enrichment, is_seed=True, rank=rank)
                candidates.append(candidate)
                account.id = self.store.upsert_account(
                    account.model_dump(), enrichment.model_dump()
                )
                self.store.save_posts(posts)
                self.store.upsert_seed_member(
                    seed_set_id,
                    account.id,
                    role="base",
                    review_status="approved",
                    primary_topic=row["primary_topic"],
                    language_bucket=language_bucket(row["languages"]),
                    account_type_bucket=type_bucket(row["account_type"]),
                    institution_kind=row.get("institution_kind") or None,
                    rank=rank,
                    source_run_id=run["id"],
                )
            self.store.save_candidates(run["id"], candidates)
            self._crawl_selected_contacts(candidates, stats, warnings, run["backend"])
            self.store.update_seed_set_status(seed_set_id, "ready")
            stats.update(budget.stats())
            merge_client_diagnostics(client, stats, warnings)
            self._report(progress, 98, "seed_ready", stats, warnings)
            return candidates
        except TwitterBackendError as exc:
            stats.update(budget.stats())
            self._report(progress, 0, "incomplete", stats, warnings)
            self.store.update_seed_set_status(seed_set_id, "incomplete")
            if exc.status_code == 402:
                raise RuntimeError("TwitterAPI.io 余额不足（HTTP 402）；种子集保持 incomplete，未批准部分结果") from exc
            raise
        except Exception:
            current = self.store.get_seed_set(seed_set_id)
            if current and current["status"] == "draft":
                self.store.update_seed_set_status(seed_set_id, "incomplete")
            raise
        finally:
            close = getattr(client, "close", None)
            if close:
                close()

    def _expand(self, run: dict, progress: ProgressCallback | None) -> list[Candidate]:
        source_id = int((run.get("config") or {}).get("seed_set_id") or 0)
        source = self.store.get_seed_set(source_id)
        source_members = self.store.list_seed_members(source_id) if source else []
        approved = [m for m in source_members if m["review_status"] == "approved"]
        if not source or len(approved) != 100:
            raise RuntimeError("seed_expand 需要恰好 100 个 approved 基础种子")
        seed_set_id = self.store.create_seed_set(
            name="Crypto KOL 100→200 扩散集",
            version=f"{source['version']}-expanded",
            target_size=200,
            quotas=source["quotas"],
            source_seed_set_id=source_id,
            status="draft",
            config={"run_id": run["id"], "backend": run["backend"]},
        )
        stats: dict = {"seed_set_id": seed_set_id, "source_seed_set_id": source_id, "raw_candidates": 0}
        warnings: list[str] = []
        budget = CallBudget()
        client = create_twitter_client(
            run["backend"], self.settings, self.store.resolve_account_id
        )
        try:
            pool, posts_by_id = self._discover_expansion(
                client, approved, run, budget, stats, warnings, progress
            )
            seed_ids = {m["account_id"] for m in approved}
            pool = {key: value for key, value in pool.items() if key not in seed_ids}
            stats["raw_candidates"] = len(pool)
            if len(pool) < 100:
                self.store.update_seed_set_status(seed_set_id, "incomplete")
                raise RuntimeError(f"扩散候选仅 {len(pool)} 个，不足 100")

            prefiltered = sorted(
                pool.values(),
                key=lambda item: (
                    item.network_weight,
                    math.log1p(item.account.followers_count if item.account else 0),
                ),
                reverse=True,
            )[:500]
            config = load_domain_config("crypto")
            aliases = [alias for values in all_aliases(config).values() for alias in values]
            for item in prefiltered[:200]:
                if item.account is None:
                    continue
                if run["backend"] == "mock":
                    posts_by_id[item.account.id] = _mock_posts(item.account, "crypto")
                else:
                    posts = self._cached_posts(
                        item.account.id,
                        lambda account=item.account: client.get_user_tweets(
                            account.id, max_results=20, username=account.username
                        ),
                        budget,
                        stats,
                        client.name,
                    )
                    posts_by_id[item.account.id] = posts
                    self.store.save_posts(posts)
            candidates: list[Candidate] = []
            item_by_id: dict[str, ExpansionCandidate] = {}
            for item in prefiltered[:200]:
                account = item.account
                if account is None or account.protected:
                    continue
                posts = posts_by_id.get(account.id, [])
                relevance = domain_relevance_score(account, posts, aliases)
                enrichment = classify_account(account, posts, config)
                score, rate, average = score_candidate(
                    account,
                    posts,
                    relevance,
                    False,
                    item.network_weight,
                    config.score_weights,
                    completeness_score=profile_completeness(account),
                    spam_penalty=spam_penalty(account, posts),
                )
                candidate = Candidate(
                    account=account,
                    domain_score=relevance,
                    engagement_rate=rate,
                    recent_engagement_avg=average,
                    score=score,
                    enrichment=enrichment,
                    recent_tweets_sample=[post.text for post in posts[:5]],
                    edges=[
                        DiscoveryEdge(
                            target_username=account.username,
                            source_type=kind,
                            from_username=source_username,
                            evidence=evidence,
                            weight=weight,
                        )
                        for _source_id, kind, evidence, weight, _url in item.edges
                        for source_username in [None]
                    ],
                )
                candidates.append(candidate)
                item_by_id[account.id] = item

            selected = self._select_expanded(candidates, item_by_id)
            network_count = sum(item_by_id[c.account.id].has_network_edge for c in selected)
            if len(selected) != 100 or network_count < 50:
                self.store.update_seed_set_status(seed_set_id, "incomplete")
                raise RuntimeError(
                    f"无法满足扩散约束：selected={len(selected)}, network={network_count}"
                )
            if network_count < 70:
                warnings.append(f"关系网络仅提供 {network_count}/100，已用主题搜索补齐")

            for member in approved:
                self.store.upsert_seed_member(
                    seed_set_id,
                    member["account_id"],
                    role="base",
                    review_status="approved",
                    primary_topic=member["primary_topic"],
                    language_bucket=member["language_bucket"],
                    account_type_bucket=member["account_type_bucket"],
                    institution_kind=member.get("institution_kind"),
                    rank=member.get("rank"),
                    source_run_id=run["id"],
                )
            for rank, candidate in enumerate(selected, 1):
                candidate.rank = rank
            self.store.save_candidates(run["id"], selected)
            self._crawl_selected_contacts(selected, stats, warnings, run["backend"])
            for candidate in selected:
                rank = candidate.rank or 0
                item = item_by_id.get(candidate.account.id)
                if item is None:
                    item = next(
                        value
                        for value in item_by_id.values()
                        if value.account
                        and value.account.username.lower() == candidate.account.username.lower()
                    )
                candidate.account.id = self.store.upsert_account(
                    candidate.account.model_dump(), candidate.enrichment.model_dump()
                )
                self.store.upsert_seed_member(
                    seed_set_id,
                    candidate.account.id,
                    role="expanded",
                    review_status="pending",
                    primary_topic=(candidate.enrichment.topics or ["infra_ai_depin"])[0],
                    language_bucket=language_bucket(candidate.enrichment.languages),
                    account_type_bucket=type_bucket(candidate.enrichment.account_type),
                    rank=rank,
                    source_run_id=run["id"],
                    network_seed_count=item.network_seed_count,
                    score=candidate.score.total,
                )
                for source_account_id, kind, evidence, weight, url in item.edges:
                    self.store.save_seed_edge(
                        run_id=run["id"],
                        source_account_id=source_account_id,
                        target_account_id=candidate.account.id,
                        target_username=candidate.account.username,
                        edge_type=kind,
                        weight=weight,
                        evidence=evidence,
                        evidence_url=url,
                    )
            self.store.update_seed_set_status(seed_set_id, "ready")
            stats.update(budget.stats())
            stats.update({"selected": 100, "network_selected": network_count})
            merge_client_diagnostics(client, stats, warnings)
            self._report(progress, 98, "expansion_ready", stats, warnings)
            return selected
        except TwitterBackendError as exc:
            stats.update(budget.stats())
            self._report(progress, 0, "incomplete", stats, warnings)
            self.store.update_seed_set_status(seed_set_id, "incomplete")
            if exc.status_code == 402:
                raise RuntimeError("TwitterAPI.io 余额不足（HTTP 402）；扩散集保持 incomplete，未写入部分结果") from exc
            raise
        except Exception:
            current = self.store.get_seed_set(seed_set_id)
            if current and current["status"] == "draft":
                self.store.update_seed_set_status(seed_set_id, "incomplete")
            raise
        finally:
            close = getattr(client, "close", None)
            if close:
                close()

    def _add_candidate(
        self,
        pool: dict[str, ExpansionCandidate],
        account: Account,
        source_id: str | None,
        kind: str,
        evidence: str,
        evidence_url: str | None = None,
    ) -> None:
        if not account.id or len(pool) >= 2000 and account.id not in pool:
            return
        item = pool.setdefault(account.id, ExpansionCandidate())
        item.account = account
        item.usernames.add(account.username.lower())
        edge = (source_id, kind, evidence[:500], EDGE_WEIGHTS[kind], evidence_url)
        if edge not in item.edges:
            item.edges.append(edge)

    def _discover_expansion(
        self,
        client: TwitterClient,
        approved: list[dict],
        run: dict,
        budget: CallBudget,
        stats: dict,
        warnings: list[str],
        progress: ProgressCallback | None,
    ) -> tuple[dict[str, ExpansionCandidate], dict[str, list[Post]]]:
        pool: dict[str, ExpansionCandidate] = {}
        posts_by_id: dict[str, list[Post]] = {}
        if run["backend"] == "mock":
            for index in range(650):
                topic = list(TOPIC_QUOTAS)[index % len(TOPIC_QUOTAS)]
                account_type = "person" if index % 5 else "company"
                account = Account(
                    id=str(20_000_000 + index),
                    username=f"mock_crypto_candidate_{index:03d}",
                    name=f"Mock Crypto Candidate {index}",
                    description=f"{topic} crypto onchain researcher analyst",
                    followers_count=1_500 + index * 97,
                    following_count=300,
                    tweet_count=800,
                    verified=index % 7 == 0,
                    created_at="2020-01-01T00:00:00+00:00",
                    profile_image_url="https://example.test/avatar.png",
                    url="https://example.test",
                    location="Global",
                    raw={"mock_account_type": account_type},
                )
                kind = ("mention", "following", "verified_follower", "topic_search")[index % 4]
                source = approved[index % len(approved)]["account_id"] if kind != "topic_search" else None
                self._add_candidate(pool, account, source, kind, f"mock {kind} evidence")
                if index % 11 == 0 and kind != "topic_search":
                    second = approved[(index + 17) % len(approved)]["account_id"]
                    self._add_candidate(pool, account, second, "following", "mock shared seed evidence")
            stats["raw_candidates"] = len(pool)
            self._report(progress, 52, "network_discovery", stats | budget.stats(), warnings)
            return pool, posts_by_id

        unresolved_mentions: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for index, member in enumerate(approved):
            posts = self._cached_posts(
                member["account_id"],
                lambda member=member: client.get_user_tweets(
                    member["account_id"], max_results=20, username=member["username"]
                ),
                budget,
                stats,
                client.name,
            )
            posts_by_id[member["account_id"]] = posts
            self.store.save_posts(posts)
            for post in posts:
                for username in related_usernames(post):
                    unresolved_mentions[username.lower()].append((member["account_id"], post.text))
            if index % 10 == 0:
                self._report(progress, 10 + index // 2, "seed_timelines", stats | budget.stats(), warnings)

        names = list(unresolved_mentions)
        batch = max(1, client.capabilities.batch_user_lookup_size)
        for start in range(0, len(names), batch):
            name_batch = names[start : start + batch]
            cache_key = f"{client.name}:profiles:" + hashlib.sha256(
                "|".join(name_batch).encode()
            ).hexdigest()
            accounts = self._cached_accounts(
                cache_key,
                7 * 86400,
                lambda name_batch=name_batch: client.get_users_by_usernames(name_batch),
                budget,
                "profile",
                stats,
            )
            for account in accounts:
                for source_id, evidence in unresolved_mentions.get(account.username.lower(), []):
                    self._add_candidate(pool, account, source_id, "mention", evidence)

        if client.capabilities.followings:
            for member in balanced_seed_sample(approved, 40):
                accounts = self._cached_accounts(
                    f"{client.name}:followings:{member['account_id']}:20",
                    14 * 86400,
                    lambda member=member: client.get_followings(member["username"], max_results=20),
                    budget,
                    "followings",
                    stats,
                )
                for account in accounts:
                    self._add_candidate(
                        pool, account, member["account_id"], "following", f"@{member['username']} follows account"
                    )
        else:
            warnings.append("所选后端不支持 followings，关系覆盖会降低")

        if client.capabilities.verified_followers and budget.optional_allowed:
            for member in approved[:20]:
                accounts = self._cached_accounts(
                    f"{client.name}:verified_followers:{member['account_id']}:20",
                    14 * 86400,
                    lambda member=member: client.get_verified_followers(
                        member["account_id"], max_results=20, username=member["username"]
                    ),
                    budget,
                    "verified_followers",
                    stats,
                )
                for account in accounts:
                    self._add_candidate(
                        pool,
                        account,
                        member["account_id"],
                        "verified_follower",
                        f"verified follower of @{member['username']}",
                    )
        elif not budget.optional_allowed:
            warnings.append("预算达到 80%，已停止 verified followers 非必要扩展")
        else:
            warnings.append("所选后端不支持 verified followers，已跳过该扩散源")

        config = load_domain_config("crypto")
        if client.capabilities.user_search:
            for topic, aliases in all_aliases(config).items():
                query = " OR ".join(aliases[:3])
                query_key = hashlib.sha256(query.encode()).hexdigest()
                accounts = self._cached_accounts(
                    f"{client.name}:user_search:{query_key}:40",
                    7 * 86400,
                    lambda query=query: client.search_users(query, max_results=40),
                    budget,
                    "user_search",
                    stats,
                )
                for account in accounts:
                    self._add_candidate(pool, account, None, "topic_search", f"profile matched {topic}")
        else:
            warnings.append("所选后端不支持关键词用户搜索")
        stats["raw_candidates"] = len(pool)
        self._report(progress, 52, "network_discovery", stats | budget.stats(), warnings)
        return pool, posts_by_id

    @staticmethod
    def _select_expanded(
        candidates: list[Candidate], items: dict[str, ExpansionCandidate]
    ) -> list[Candidate]:
        ordered = sorted(candidates, key=lambda c: c.score.total, reverse=True)
        network = [c for c in ordered if items[c.account.id].has_network_edge]
        search_only = [c for c in ordered if not items[c.account.id].has_network_edge]
        selected = network[:100]
        if len(selected) < 100:
            selected.extend(search_only[: min(50, 100 - len(selected))])
        return selected[:100]
