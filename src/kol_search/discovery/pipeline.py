from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable

from kol_search.config_loader import load_domain_config, load_seed_handles
from kol_search.contacts import PublicContactCrawler
from kol_search.db import Store
from kol_search.discovery.ai_enrichment import enrich_with_openai
from kol_search.discovery.classify import classify_account, profile_completeness, spam_penalty
from kol_search.discovery.filter_domain import domain_relevance_score, passes_domain_filter
from kol_search.discovery.query import QueryPlan, all_aliases, plan_queries
from kol_search.discovery.score import score_candidate
from kol_search.models import Account, Candidate, DiscoveryEdge, Post
from kol_search.settings import Settings
from kol_search.twitter.base import TwitterBackendError, TwitterClient, merge_client_diagnostics
from kol_search.twitter.factory import create_twitter_client


ProgressCallback = Callable[[int, str, dict, list[str]], None]


@dataclass
class CandidateSeed:
    username: str
    account: Account | None = None
    is_seed: bool = False
    seed_proximity: float = 0.0
    edges: list[DiscoveryEdge] = field(default_factory=list)


def _key(username: str) -> str:
    return username.lstrip("@").lower()


def _global_plans(config, settings: Settings) -> list[QueryPlan]:  # noqa: ANN001
    plans: list[QueryPlan] = []
    for slug, aliases in all_aliases(config).items():
        topic = ", ".join([slug] + aliases[:3])
        plans.append(plan_queries(topic, config, max_user_queries=1, max_post_queries=1))
    return plans


class DiscoveryPipeline:
    def __init__(self, store: Store, settings: Settings) -> None:
        self.store = store
        self.settings = settings

    def run(self, run: dict, progress: ProgressCallback | None = None) -> list[Candidate]:
        config = load_domain_config("crypto")
        warnings: list[str] = []
        stats = {
            "user_queries": 0,
            "post_queries": 0,
            "user_lookups": 0,
            "timeline_requests": 0,
            "ai_calls": 0,
            "pages_fetched": 0,
            "contacts_found": 0,
        }

        client: TwitterClient | None = None

        def report(percent: int, phase: str) -> None:
            if client is not None:
                merge_client_diagnostics(client, stats, warnings)
            if progress:
                progress(percent, phase, stats, warnings)

        client = create_twitter_client(
            run["backend"], self.settings, self.store.resolve_account_id
        )
        try:
            plans = (
                _global_plans(config, self.settings)
                if run.get("kind") == "global"
                else [
                    plan_queries(
                        run.get("query") or "crypto",
                        config,
                        max_user_queries=self.settings.max_user_queries,
                        max_post_queries=self.settings.max_post_queries,
                    )
                ]
            )
            report(5, "planning")
            pool, discovered_posts = self._discover(client, plans, config, stats, warnings)
            if not pool:
                report(0, "discovery_failed")
                raise RuntimeError("未发现任何候选账号；请检查查询、后端权限或数据额度")
            report(28, "profile_lookup")

            self._fill_profiles(client, pool, stats, warnings)
            seeds = list(pool.values())
            seeds = [seed for seed in seeds if seed.account is not None]
            seeds.sort(
                key=lambda seed: (seed.is_seed, seed.account.followers_count if seed.account else 0),
                reverse=True,
            )
            seeds = seeds[: self.settings.max_candidates]
            report(38, "content_enrichment")

            posts_by_account: dict[str, list[Post]] = defaultdict(list)
            for post in discovered_posts:
                if post.author_id:
                    posts_by_account[post.author_id].append(post)
            for seed in seeds[: self.settings.max_enriched_candidates]:
                account = seed.account
                if account is None:
                    continue
                try:
                    timeline = client.get_user_tweets(
                        account.id,
                        max_results=config.recent_tweets_per_account,
                        username=account.username,
                    )
                    stats["timeline_requests"] += 1
                    self.store.save_posts(timeline)
                    posts_by_account[account.id].extend(timeline)
                except TwitterBackendError as exc:
                    warnings.append(f"@{account.username} timeline: {exc}")
            report(58, "classification")

            candidates = self._classify_and_score(
                seeds, posts_by_account, plans, config, run, stats, warnings
            )
            selected = self._select_results(candidates, int(run.get("result_limit") or 100))
            for rank, candidate in enumerate(
                sorted([c for c in selected if c.enrichment.account_type == "person"], key=lambda c: c.score.total, reverse=True),
                1,
            ):
                candidate.rank = rank
            for rank, candidate in enumerate(
                sorted([c for c in selected if c.enrichment.account_type != "person"], key=lambda c: c.score.total, reverse=True),
                1,
            ):
                candidate.rank = rank
            selected.sort(
                key=lambda candidate: (
                    candidate.enrichment.account_type != "person",
                    candidate.rank or 9999,
                )
            )
            self.store.save_candidates(run["id"], selected)
            report(75, "contact_discovery")

            crawler = PublicContactCrawler(
                max_pages=self.settings.max_site_pages,
                cache_get=self.store.get_crawl_cache,
                cache_set=self.store.set_crawl_cache,
            )
            try:
                for candidate in selected[: self.settings.max_contact_accounts]:
                    result = crawler.crawl_account(candidate.account)
                    stats["pages_fetched"] += result.pages_fetched
                    stats["contacts_found"] += len(result.contacts)
                    warnings.extend(result.warnings)
                    self.store.save_contacts(result.contacts)
            finally:
                crawler.close()
            report(98, "persisting")
            return selected
        finally:
            close = getattr(client, "close", None)
            if close:
                close()

    def _discover(
        self,
        client: TwitterClient,
        plans: list[QueryPlan],
        config,  # noqa: ANN001
        stats: dict,
        warnings: list[str],
    ) -> tuple[dict[str, CandidateSeed], list[Post]]:
        pool: dict[str, CandidateSeed] = {}
        posts: list[Post] = []

        def add(
            username: str,
            *,
            account: Account | None = None,
            edge: DiscoveryEdge,
            is_seed: bool = False,
            proximity: float = 0.0,
        ) -> None:
            if not username:
                return
            key = _key(username)
            if key not in pool and len(pool) >= self.settings.max_candidates:
                return
            seed = pool.setdefault(key, CandidateSeed(username=username.lstrip("@")))
            if account:
                seed.account = account
                seed.username = account.username
            seed.is_seed = seed.is_seed or is_seed
            seed.seed_proximity = max(seed.seed_proximity, proximity)
            if not any(existing.evidence == edge.evidence and existing.source_type == edge.source_type for existing in seed.edges):
                seed.edges.append(edge)

        seed_handles = (
            load_seed_handles(config.seed_file) if self.settings.use_configured_seeds else []
        )
        try:
            accounts = client.get_users_by_usernames(seed_handles)
            stats["user_lookups"] += len(seed_handles)
            accounts_by_name = {_key(account.username): account for account in accounts}
            for username in seed_handles:
                add(
                    username,
                    account=accounts_by_name.get(_key(username)),
                    edge=DiscoveryEdge(
                        target_username=username,
                        source_type="seed",
                        evidence="configured crypto seed",
                        weight=1.0,
                    ),
                    is_seed=True,
                    proximity=1.0,
                )
        except TwitterBackendError as exc:
            warnings.append(f"seed lookup: {exc}")
            for username in seed_handles:
                add(
                    username,
                    edge=DiscoveryEdge(target_username=username, source_type="seed", evidence="configured crypto seed"),
                    is_seed=True,
                    proximity=1.0,
                )

        for plan in plans:
            global_mode = len(plans) > 1
            if client.capabilities.user_search:
                for query in plan.user_queries:
                    try:
                        remaining = max(1, self.settings.max_candidates - len(pool))
                        found = client.search_users(
                            query,
                            max_results=min(
                                20 if global_mode else 100,
                                client.capabilities.user_search_page_size,
                                remaining,
                            ),
                        )
                        stats["user_queries"] += 1
                        for account in found:
                            add(
                                account.username,
                                account=account,
                                edge=DiscoveryEdge(
                                    target_username=account.username,
                                    source_type="user_search",
                                    evidence=f"profile matched {query}",
                                    query=query,
                                    weight=0.8,
                                ),
                                proximity=0.35,
                            )
                    except TwitterBackendError as exc:
                        warnings.append(f"user search {query}: {exc}")

            for query in plan.post_queries:
                try:
                    found_posts = client.search_tweets(
                        query,
                        max_results=min(
                            min(15, config.search_max_results_per_query) if global_mode else config.search_max_results_per_query,
                            client.capabilities.post_search_page_size,
                        ),
                    )
                    stats["post_queries"] += 1
                    posts.extend(found_posts)
                    for post in found_posts:
                        if post.author_username:
                            add(
                                post.author_username,
                                edge=DiscoveryEdge(
                                    target_username=post.author_username,
                                    source_type="search",
                                    evidence=post.text[:280],
                                    query=query,
                                    weight=0.7,
                                ),
                                proximity=0.25,
                            )
                        for mentioned in post.mentioned_usernames:
                            add(
                                mentioned,
                                edge=DiscoveryEdge(
                                    target_username=mentioned,
                                    source_type="mention",
                                    from_username=post.author_username,
                                    evidence=post.text[:280],
                                    query=query,
                                    weight=0.45,
                                ),
                                proximity=0.2,
                            )
                except TwitterBackendError as exc:
                    warnings.append(f"post search {query}: {exc}")
        self.store.save_posts(posts)
        return pool, posts

    def _fill_profiles(
        self,
        client: TwitterClient,
        pool: dict[str, CandidateSeed],
        stats: dict,
        warnings: list[str],
    ) -> None:
        missing = [seed.username for seed in pool.values() if seed.account is None]
        batch_size = max(1, client.capabilities.batch_user_lookup_size)
        for index in range(0, len(missing), batch_size):
            batch = missing[index : index + batch_size]
            try:
                accounts = client.get_users_by_usernames(batch)
                stats["user_lookups"] += len(batch)
                for account in accounts:
                    if _key(account.username) in pool:
                        pool[_key(account.username)].account = account
            except TwitterBackendError as exc:
                warnings.append(f"profile lookup batch: {exc}")

    def _classify_and_score(
        self,
        seeds: list[CandidateSeed],
        posts_by_account: dict[str, list[Post]],
        plans: list[QueryPlan],
        config,  # noqa: ANN001
        run: dict,
        stats: dict,
        warnings: list[str],
    ) -> list[Candidate]:
        aliases = list(dict.fromkeys(alias for plan in plans for alias in plan.aliases))
        broad_lexicon = ["crypto", "web3", "blockchain", "加密", "区块链"]
        candidates: list[Candidate] = []
        ai_enabled = bool(run.get("use_ai") and self.settings.openai_api_key)
        if run.get("use_ai") and not self.settings.openai_api_key:
            warnings.append("已请求模型增强，但未配置 OPENAI_API_KEY；已使用规则分类")

        for seed in seeds:
            account = seed.account
            if account is None:
                continue
            posts = posts_by_account.get(account.id, [])
            topic_relevance = domain_relevance_score(account, posts, aliases)
            broad_relevance = domain_relevance_score(account, posts, broad_lexicon)
            relevance = 0.85 * topic_relevance + 0.15 * broad_relevance
            if run.get("kind") != "global" and topic_relevance == 0:
                continue
            if not passes_domain_filter(
                relevance,
                account.followers_count,
                max(0.01, min(config.min_domain_score, 0.12)),
                int(run.get("min_followers") or config.min_followers),
            ) and not (seed.is_seed and run.get("kind") == "global"):
                continue
            enrichment = classify_account(account, posts, config)
            enrichment.relevance_score = relevance

            if ai_enabled and len(candidates) < self.settings.max_enriched_candidates:
                try:
                    ai_result = enrich_with_openai(
                        account,
                        posts,
                        run.get("query") or "crypto",
                        api_key=self.settings.openai_api_key or "",
                        model=run.get("model") or self.settings.openai_model,
                        cache_get=self.store.cache_get,
                        cache_set=self.store.cache_set,
                    )
                    stats["ai_calls"] += 1
                    ai_result.relevance_score = 0.6 * relevance + 0.4 * ai_result.relevance_score
                    enrichment = ai_result
                    relevance = ai_result.relevance_score
                except Exception as exc:
                    if len([warning for warning in warnings if warning.startswith("OpenAI")]) < 5:
                        warnings.append(f"OpenAI @{account.username}: {exc}")

            if run.get("language") not in {None, "", "all"} and run["language"] not in enrichment.languages:
                continue
            if run.get("account_type") not in {None, "", "all"}:
                desired = run["account_type"]
                if desired == "organization" and enrichment.account_type == "person":
                    continue
                if desired != "organization" and enrichment.account_type != desired:
                    continue

            completeness = profile_completeness(account)
            penalty = spam_penalty(account, posts)
            score, engagement_rate, engagement_avg = score_candidate(
                account,
                posts,
                relevance,
                seed.is_seed,
                seed.seed_proximity,
                config.score_weights,
                completeness_score=completeness,
                spam_penalty=penalty,
            )
            candidates.append(Candidate(
                account=account,
                domain_score=relevance,
                engagement_rate=engagement_rate,
                recent_engagement_avg=engagement_avg,
                is_seed=seed.is_seed,
                score=score,
                edges=seed.edges,
                recent_tweets_sample=[post.text for post in posts[:5]],
                enrichment=enrichment,
            ))
        return candidates

    @staticmethod
    def _select_results(candidates: list[Candidate], limit: int) -> list[Candidate]:
        limit = max(1, min(100, limit))
        people = sorted(
            [candidate for candidate in candidates if candidate.enrichment.account_type == "person"],
            key=lambda candidate: candidate.score.total,
            reverse=True,
        )
        organizations = sorted(
            [candidate for candidate in candidates if candidate.enrichment.account_type != "person"],
            key=lambda candidate: candidate.score.total,
            reverse=True,
        )
        person_target = min(len(people), max(1, round(limit * 0.75)))
        organization_target = min(len(organizations), limit - person_target)
        remaining = limit - person_target - organization_target
        if remaining:
            extra_people = min(remaining, len(people) - person_target)
            person_target += extra_people
            remaining -= extra_people
        if remaining:
            organization_target += min(remaining, len(organizations) - organization_target)
        return people[:person_target] + organizations[:organization_target]
