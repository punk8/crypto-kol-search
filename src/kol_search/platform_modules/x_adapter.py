from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from datetime import timedelta
from typing import Any

from kol_search.discovery.promotion import (
    BalancedPromotionPolicy,
    KolStatus,
    PromotionInput,
)
from kol_search.platform_modules.adapter_utils import (
    keyword_relevance_score,
    log_ratio,
    parse_time,
    resolve_database_path,
    stable_native_id,
    utc_now,
)
from kol_search.platform_modules.content_intelligence import (
    ContentInsight,
    ContentIntelligence,
    NativeContentSample,
    content_intelligence_from_settings,
)
from kol_search.platform_modules.x_native import XRepository
from kol_search.platforms.kernel import (
    NativeObjectReference,
    PlatformActionProposal,
    PlatformOpportunityProposal,
    PlatformProjectionResult,
    PlatformSuggestedAction,
)
from kol_search.platforms.x import (
    XAccount,
    XAccountRelation,
    XTrend,
    XTrendTweet,
    XTweet,
)


_X_RELEVANCE_TERMS = (
    "crypto",
    "bitcoin",
    "ethereum",
    "defi",
    "web3",
    "blockchain",
    "stablecoin",
    "token",
    "rwa",
    "加密",
    "区块链",
    "链上",
)

_X_NEGATED_RELEVANCE_PHRASES = (
    "no crypto",
    "not crypto",
    "not about crypto",
    "unrelated to crypto",
    "without crypto",
    "非加密",
    "与加密无关",
    "不是加密",
    "不聊加密",
)


def _classify_relevance(text: str) -> float:
    return keyword_relevance_score(
        text,
        _X_RELEVANCE_TERMS,
        negated_phrases=_X_NEGATED_RELEVANCE_PHRASES,
    )


class XAutomationAdapter:
    """Owns X persistence, X-local scoring, promotion, and opportunity proposals."""

    platform_id = "x"

    def __init__(
        self,
        settings: object,
        *,
        repository: XRepository | None = None,
        promotion: BalancedPromotionPolicy | None = None,
        content_intelligence: ContentIntelligence | None = None,
    ) -> None:
        self.settings = settings
        self._database_path = resolve_database_path(settings)
        self._repository = repository
        self.promotion = promotion or BalancedPromotionPolicy(
            review_enabled=bool(getattr(settings, "review_queue_enabled", False))
        )
        self.content_intelligence = (
            content_intelligence or content_intelligence_from_settings(settings)
        )

    @property
    def repository(self) -> XRepository:
        if self._repository is None:
            if self._database_path is None:
                raise RuntimeError("X automation adapter requires a database path")
            self._repository = XRepository(self._database_path)
        return self._repository

    def project_discovery(
        self,
        items: Iterable[object],
        *,
        payload: Mapping[str, Any],
    ) -> PlatformProjectionResult:
        accounts, tweets, trends, relations, trend_tweets = self._split(items)
        tweets, _insights, intelligence_warnings = self._enrich_tweets(tweets, payload)
        with self.repository.unit_of_work():
            self._persist(accounts, tweets, trends, trend_tweets)
            self._promote(accounts, tweets, relations)
            self.repository.pause_inactive(
                days=int(getattr(self.settings, "inactive_pause_days", 90))
            )
        return PlatformProjectionResult(
            native_counts={"accounts": len(accounts), "content": len(tweets)},
            warnings=intelligence_warnings,
        )

    def project_signals(
        self,
        items: Iterable[object],
        *,
        payload: Mapping[str, Any],
    ) -> PlatformProjectionResult:
        accounts, tweets, trends, relations, trend_tweets = self._split(items)
        tweets, insights, intelligence_warnings = self._enrich_tweets(tweets, payload)
        with self.repository.unit_of_work():
            self._persist(accounts, tweets, trends, trend_tweets)
            self._promote(accounts, tweets, relations)
            self.repository.pause_inactive(
                days=int(getattr(self.settings, "inactive_pause_days", 90))
            )
            opportunities = tuple(
                proposal
                for proposal in (
                    *(
                        self._content_opportunity(
                            tweet, insights.get(tweet.external_id)
                        )
                        for tweet in tweets
                    ),
                    *(self._trend_opportunity(trend) for trend in trends),
                )
                if proposal is not None
            )
        return PlatformProjectionResult(
            native_counts={
                "accounts": len(accounts),
                "content": len(tweets),
                "trends": len(trends),
            },
            opportunities=opportunities,
            warnings=intelligence_warnings,
            metadata={"content_intelligence_count": len(insights)},
        )

    def list_kols(
        self, *, status: str = "all", limit: int = 100
    ) -> list[dict[str, Any]]:
        return self.repository.list_kols(status=status, limit=limit)

    def list_trends(self, *, limit: int = 50) -> list[dict[str, Any]]:
        return self.repository.list_recent_trends(limit=limit)

    def workspace_feed(self, *, limit: int = 30) -> dict[str, object]:
        """Expose X-native workspace views without teaching the core X schemas."""

        trend_tweets = self.repository.list_recent_trend_tweets(limit=limit)
        tweets = self.repository.list_managed_kol_tweets(limit=limit)
        refreshed_at = max(
            (
                str(item.get("last_seen_at") or item.get("captured_at") or "")
                for item in [*trend_tweets, *tweets]
                if item.get("last_seen_at") or item.get("captured_at")
            ),
            default="",
        )
        return {
            "recent_trend_tweets": trend_tweets,
            "managed_kol_tweets": tweets,
            "refreshed_at": refreshed_at,
        }

    def workspace_feed_page(
        self, feed_name: str, *, limit: int = 20, offset: int = 0
    ) -> dict[str, object]:
        """Return one X-native feed page for progressive scrolling."""

        page_size = max(1, min(limit, 50))
        start = max(0, offset)
        if feed_name == "trends":
            rows = self.repository.list_recent_trend_tweets(
                limit=page_size + 1, offset=start
            )
        elif feed_name == "kols":
            rows = self.repository.list_managed_kol_tweets(
                limit=page_size + 1, offset=start
            )
        else:
            raise ValueError(f"Unknown X workspace feed: {feed_name}")
        return {
            "items": rows[:page_size],
            "next_offset": start + min(len(rows), page_size),
            "has_more": len(rows) > page_size,
        }

    def get_kol(self, native_id: str) -> dict[str, Any] | None:
        return self.repository.get_kol(native_id)

    def list_scan_targets(
        self,
        *,
        statuses: Iterable[str],
        after: str | None,
        limit: int,
    ) -> tuple[list[dict[str, Any]], str | None]:
        return self.repository.list_scan_targets(
            statuses=tuple(statuses), after=after, limit=limit
        )

    def summary(self) -> Mapping[str, int]:
        return self.repository.summary()

    def set_kol_status(self, native_id: str, status: str) -> None:
        current = self.repository.get_kol(native_id)
        if current is None:
            raise KeyError(native_id)
        self.repository.set_kol_status(
            native_id,
            KolStatus(status),
            score=float(current["score"]),
            reasons=tuple(current.get("reasons") or ()),
        )

    def add_seed(self, native_id: str, display_name: str | None = None) -> None:
        normalized = native_id.strip().lstrip("@")
        if not normalized:
            raise ValueError("X seed handle cannot be empty")
        current = self.repository.get_kol(normalized)
        if current is None:
            self.repository.upsert_account(
                XAccount(
                    external_id=normalized,
                    username=normalized,
                    display_name=(display_name or "").strip() or None,
                    url=f"https://x.com/{normalized}",
                    source_provider="manual_seed",
                )
            )
            current = self.repository.get_kol(normalized)
            assert current is not None
        self.repository.set_kol_status(
            normalized,
            KolStatus.SEED,
            score=float(current.get("score") or 0),
            reasons=("curated seed",),
        )

    @staticmethod
    def _split(
        items: Iterable[object],
    ) -> tuple[
        list[XAccount],
        list[XTweet],
        list[XTrend],
        list[XAccountRelation],
        list[XTrendTweet],
    ]:
        values = list(items)
        relations = [value for value in values if isinstance(value, XAccountRelation)]
        trend_tweets = [value for value in values if isinstance(value, XTrendTweet)]
        accounts = [value for value in values if isinstance(value, XAccount)]
        accounts.extend(relation.target for relation in relations)
        tweets_by_id = {
            value.external_id: value
            for value in [
                *[value for value in values if isinstance(value, XTweet)],
                *(value.tweet for value in trend_tweets),
            ]
        }
        return (
            accounts,
            list(tweets_by_id.values()),
            [value for value in values if isinstance(value, XTrend)],
            relations,
            trend_tweets,
        )

    def _persist(
        self,
        accounts: list[XAccount],
        tweets: list[XTweet],
        trends: list[XTrend],
        trend_tweets: list[XTrendTweet],
    ) -> None:
        repository = self.repository
        rich_ids = {account.external_id for account in accounts}
        trend_tweet_ids = {item.tweet.external_id for item in trend_tweets}
        account_aliases: dict[str, str] = {}
        for tweet in tweets:
            if (
                tweet.author_external_id not in rich_ids
                and repository.get_kol(tweet.author_external_id) is None
            ):
                original_id = tweet.author_external_id
                account_aliases[original_id] = repository.upsert_account(
                    XAccount(
                        external_id=original_id,
                        username=tweet.author_username or tweet.author_external_id,
                        source_provider=tweet.source_provider,
                        captured_at=tweet.captured_at,
                    ),
                    track_as_kol=tweet.external_id not in trend_tweet_ids,
                )
        for account in accounts:
            original_id = account.external_id
            canonical_id = repository.upsert_account(account)
            account_aliases[original_id] = canonical_id
            account.external_id = canonical_id
        for tweet in tweets:
            tweet.author_external_id = account_aliases.get(
                tweet.author_external_id, tweet.author_external_id
            )
        repository.upsert_tweets(tweets)
        repository.upsert_trends(trends)
        repository.upsert_trend_tweets(trend_tweets)

    def _enrich_tweets(
        self,
        tweets: list[XTweet],
        payload: Mapping[str, Any],
    ) -> tuple[list[XTweet], Mapping[str, ContentInsight], tuple[str, ...]]:
        insights: Mapping[str, ContentInsight] = {}
        warnings: tuple[str, ...] = ()
        brand = payload.get("brand")
        try:
            insights = self.content_intelligence.analyze(
                platform_id=self.platform_id,
                contents=[
                    NativeContentSample(
                        object_id=tweet.external_id,
                        author_id=tweet.author_external_id,
                        text=tweet.text,
                        url=tweet.url,
                    )
                    for tweet in tweets
                ],
                brand=brand if isinstance(brand, Mapping) else {},
            )
        except Exception as exc:
            warnings = (f"optional X content intelligence failed: {exc}",)
        classified = [
            tweet.model_copy(
                update={
                    "relevance_score": max(
                        tweet.relevance_score,
                        _classify_relevance(tweet.text),
                        (
                            insights[tweet.external_id].relevance_score
                            if tweet.external_id in insights
                            else 0.0
                        ),
                    )
                }
            )
            for tweet in tweets
        ]
        return classified, insights, warnings

    def _promote(
        self,
        accounts: list[XAccount],
        tweets: list[XTweet],
        relations: list[XAccountRelation],
    ) -> None:
        repository = self.repository
        posts_by_author: dict[str, int] = {}
        for tweet in tweets:
            posts_by_author[tweet.author_external_id] = (
                posts_by_author.get(tweet.author_external_id, 0) + 1
            )
        trusted_relations = [
            relation
            for relation in relations
            if relation.source_status in {KolStatus.SEED.value, KolStatus.ACTIVE.value}
        ]
        repository.record_evidence(
            [
                {
                    "source_account_id": relation.source_external_id,
                    "target_account_id": relation.target.external_id,
                    "relation_type": "seed_relation",
                    "evidence": (
                        f"{relation.relation_type}:{relation.evidence_external_id}"
                        if relation.evidence_external_id
                        else relation.relation_type
                    ),
                    "evidence_url": relation.evidence_url or relation.target.url,
                    "weight": 0.20,
                }
                for relation in trusted_relations
            ]
        )
        stored_by_id = {
            str(row["id"]): row for row in repository.list_kols(limit=500)
        }
        all_ids = (
            {account.external_id for account in accounts}
            | set(stored_by_id)
            | {
                account_id
                for account_id in posts_by_author
                if repository.get_kol(account_id) is not None
            }
        )
        by_id = {account.external_id: account for account in accounts}
        for account_id in all_ids:
            account = by_id.get(account_id)
            stored = stored_by_id.get(account_id) or repository.get_kol(account_id) or {}
            followers = account.followers_count if account else int(stored.get("followers_count") or 0)
            content_count = repository.recent_relevant_content_count(account_id)
            relationship_count = self.repository.relationship_evidence_count(account_id)
            has_relationship = relationship_count > 0
            score = min(
                1.0,
                0.30
                + 0.30 * log_ratio(followers, 1_000_000)
                + 0.25 * min(1.0, content_count / 3)
                + (0.20 if has_relationship else 0),
            )
            current = KolStatus(stored.get("status", KolStatus.CANDIDATE.value))
            decision = self.promotion.decide(
                PromotionInput(
                    score=score,
                    relevant_content_count_30d=content_count,
                    relationship_evidence_count=relationship_count,
                    protected=(
                        account.protected
                        if account is not None
                        else bool(stored.get("protected"))
                    ),
                    disabled=(
                        account.disabled
                        if account is not None
                        else bool(stored.get("disabled"))
                    ),
                    spam_risk=(
                        account.spam_risk
                        if account is not None
                        else float(stored.get("spam_risk") or 0.0)
                    ),
                    current_status=current,
                )
            )
            repository.set_kol_status(
                account_id,
                decision.status,
                score=score,
                reasons=decision.reasons,
                qualified=decision.qualified,
            )

    def _content_opportunity(
        self,
        content: XTweet,
        insight: ContentInsight | None = None,
    ) -> PlatformOpportunityProposal | None:
        if content.relevance_score < 0.5:
            return None
        ttl_hours = max(1, int(getattr(self.settings, "signal_opportunity_ttl_hours", 24)))
        created = parse_time(content.created_at)
        age_hours = max(0.0, (utc_now() - created).total_seconds() / 3600) if created else 24.0
        if age_hours > ttl_hours:
            return None
        engagement = (
            content.metrics.likes
            + content.metrics.reposts
            + content.metrics.replies
            + content.metrics.quotes
        )
        engagement_points = min(30.0, math.log1p(max(0, engagement)) * 6)
        recency_points = max(0.0, 20 * (1 - age_hours / ttl_hours))
        relevance_points = content.relevance_score * 30
        score = min(100.0, 20 + relevance_points + engagement_points + recency_points)
        expires_at = ((created or utc_now()) + timedelta(hours=ttl_hours)).isoformat()
        fallback_draft = (
            "这个观察很有启发。接下来最值得验证的指标是什么？"
            if any("\u4e00" <= char <= "\u9fff" for char in content.text)
            else "Useful framing. Which measurable signal would confirm this next?"
        )
        draft = str((insight.reply_draft if insight else None) or fallback_draft).strip()[
            :260
        ]
        evidence: list[dict[str, Any]] = [
            {"kind": "recent_content", "engagement": engagement}
        ]
        if insight is not None:
            evidence.append(
                {
                    "kind": "content_intelligence",
                    "relevance_score": insight.relevance_score,
                    "cluster_key": insight.cluster_key,
                    "cluster_title": insight.cluster_title,
                    "rationale": insight.rationale,
                }
            )
        return PlatformOpportunityProposal(
            opportunity_type="reply",
            native_object=NativeObjectReference("x", "tweet", content.external_id),
            priority=round(score),
            score=score,
            title=content.text.splitlines()[0][:120] if content.text else content.external_id,
            evidence=tuple(evidence),
            payload={
                "target_url": content.url,
                "target_author_id": content.author_external_id,
                "text": content.text,
                "draft": draft,
                "is_initial_dm": False,
                "cluster_key": insight.cluster_key if insight else None,
                "cluster_title": insight.cluster_title if insight else None,
            },
            expires_at=expires_at,
            score_reasons=(
                "20 platform opportunity base",
                f"{relevance_points:.1f} relevance points",
                f"{engagement_points:.1f} engagement points",
                f"{recency_points:.1f} recency points",
            ),
            suggested_actions=(
                PlatformActionProposal(
                    PlatformSuggestedAction.COMMENT,
                    draft=draft,
                    score=score,
                    expires_at=expires_at,
                ),
            ),
        )

    def _trend_opportunity(self, trend: XTrend) -> PlatformOpportunityProposal | None:
        relevance = _classify_relevance(trend.name)
        if relevance < 0.5:
            return None
        rank = max(1, trend.rank or 20)
        rank_points = max(0.0, (20 - rank) * 1.25)
        volume_points = min(25.0, math.log1p(max(0, trend.post_count)) * 3)
        relevance_points = relevance * 25
        score = min(100.0, 30 + relevance_points + rank_points + volume_points)
        observed_at = utc_now()
        ttl_hours = max(
            1, int(getattr(self.settings, "signal_opportunity_ttl_hours", 24))
        )
        draft = (
            f"{trend.name}: the useful question is which measurable signal "
            "confirms the trend next."
        )
        actions: tuple[PlatformActionProposal, ...] = ()
        if score >= float(getattr(self.settings, "auto_publish_score", 80.0)):
            actions = (
                PlatformActionProposal(
                    PlatformSuggestedAction.OWNED_PUBLISH,
                    draft=draft,
                    score=score,
                ),
            )
        return PlatformOpportunityProposal(
            opportunity_type="topic",
            native_object=NativeObjectReference(
                "x", "trend", stable_native_id("x", trend.name.casefold())
            ),
            priority=round(score),
            score=score,
            title=trend.name,
            evidence=(
                {
                    "kind": "native_trend",
                    "rank": rank,
                    "content_count": trend.post_count,
                    "relevance_score": relevance,
                },
            ),
            payload={
                "draft": draft,
                "target_url": trend.url,
                "observed_at": observed_at.isoformat(),
            },
            expires_at=(observed_at + timedelta(hours=ttl_hours)).isoformat(),
            score_reasons=(
                "30 native trend base",
                f"{relevance_points:.1f} deterministic relevance points",
                f"{rank_points:.1f} rank points",
                f"{volume_points:.1f} volume points",
            ),
            suggested_actions=actions,
        )


__all__ = ["XAutomationAdapter"]
