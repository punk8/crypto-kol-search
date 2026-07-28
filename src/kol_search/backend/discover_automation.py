from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Iterable

from kol_search.backend.connectors.base import ConnectorError, ConnectorResult
from kol_search.backend.connectors.x import XConnector
from kol_search.backend.kol_scoring import domain_terms, text_relevance
from kol_search.backend.models import (
    AccountContentQuery,
    AccountSearchQuery,
    ContentItem,
    ContentMetrics,
    ContentSearchQuery,
    DiscoveryDomain,
    HotContentItem,
    HotContentScoreComponents,
)
from kol_search.platform_modules.x_native import XRepository
from kol_search.platforms.x import XTweet, XTweetMetrics
from kol_search.settings import Settings


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_time(value: str | datetime | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse_discovery_domains(settings: Settings) -> tuple[DiscoveryDomain, ...]:
    try:
        raw = json.loads(settings.discovery_domains_json)
    except (TypeError, ValueError, json.JSONDecodeError):
        raw = []
    if not isinstance(raw, list):
        raw = []
    values: list[DiscoveryDomain] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            domain = DiscoveryDomain.model_validate(item)
        except ValueError:
            continue
        if domain.enabled and domain.key not in {value.key for value in values}:
            values.append(domain)
    return tuple(values)


class DiscoverAutomationService:
    """Autonomous multi-domain KOL enrollment and noise-filtered hot content."""

    def __init__(self, settings: Settings, connector: XConnector) -> None:
        self.settings = settings
        self.connector = connector
        self.repository = XRepository(settings.database_target())
        self.domains = parse_discovery_domains(settings)

    def close(self) -> None:
        self.repository.close()

    def refresh_domains(self) -> dict[str, Any]:
        output: dict[str, Any] = {"domains": {}, "warnings": []}
        for domain in self.domains:
            try:
                result = self.connector.search_accounts(
                    AccountSearchQuery(
                        query=domain.query,
                        language=domain.languages[0] if len(domain.languages) == 1 else None,
                        limit=self.settings.x_kol_discovery_limit,
                        sample_size=self.settings.x_kol_candidate_sample_size,
                        # High-confidence scoring requires at least three recent
                        # posts. Preserve legacy settings while making the
                        # autonomous enrollment path internally consistent.
                        recent_posts_per_account=max(
                            3, self.settings.x_kol_recent_posts_per_account
                        ),
                        min_engagement=self.settings.x_kol_min_engagement,
                    )
                )
            except ConnectorError as exc:
                output["warnings"].append(f"{domain.key}: {exc}")
                continue
            active = 0
            evaluated = 0
            for item in result.items:
                relevant_recent = sum(
                    1
                    for content in item.recent_content
                    if text_relevance(content.text, domain_terms(domain.query)) >= 0.35
                )
                evidence_count = len(item.evidence)
                qualified = bool(
                    self.settings.auto_watchlist_enabled
                    and item.score >= self.settings.auto_watchlist_min_score
                    and item.confidence == "High"
                    and evidence_count >= self.settings.auto_watchlist_min_evidence
                    and relevant_recent >= 2
                    and not item.protected
                )
                reasons = (
                    f"score={item.score}",
                    f"confidence={item.confidence}",
                    f"evidence={evidence_count}",
                    f"relevant_recent={relevant_recent}",
                )
                membership = self.repository.evaluate_domain_membership(
                    domain_key=domain.key,
                    domain_name=domain.name,
                    domain_query=domain.query,
                    account_id=item.native_id,
                    score=item.score,
                    confidence=item.confidence,
                    reasons=reasons,
                    qualified=qualified,
                    qualifying_runs=self.settings.auto_watchlist_qualifying_runs,
                    max_active=self.settings.auto_watchlist_max_per_domain,
                )
                evaluated += 1
                if membership["status"] == "active":
                    active += 1
            output["domains"][domain.key] = {
                "evaluated": evaluated,
                "active": active,
                "provider": result.provider,
                "warnings": list(result.warnings),
            }
        return output

    def _persist_content(self, items: Iterable[ContentItem]) -> int:
        stored: list[XTweet] = []
        all_terms = tuple(
            dict.fromkeys(
                term
                for domain in self.domains
                for term in domain_terms(domain.query)
            )
        )
        for item in items:
            handle = item.author.username or item.author.native_id
            account_id = self.repository.ensure_content_author(
                account_id=item.author.native_id,
                handle=handle,
                provider=item.provider,
            )
            stored.append(
                XTweet(
                    external_id=item.native_id,
                    author_external_id=account_id,
                    author_username=handle,
                    text=item.text,
                    created_at=item.published_at.isoformat() if item.published_at else None,
                    language=item.language,
                    url=item.canonical_url,
                    conversation_id=item.conversation_id,
                    referenced_tweet_id=item.referenced_content_id,
                    reference_type=item.reference_type,
                    metrics=XTweetMetrics(
                        likes=item.metrics.likes or 0,
                        reposts=item.metrics.reposts or 0,
                        replies=item.metrics.replies or 0,
                        quotes=item.metrics.quotes or 0,
                        bookmarks=item.metrics.bookmarks or 0,
                        views=item.metrics.views or 0,
                    ),
                    relevance_score=text_relevance(item.text, all_terms),
                    source_provider=item.provider,
                    captured_at=item.observed_at.isoformat(),
                )
            )
        self.repository.upsert_tweets(stored)
        return len(stored)

    def refresh_hot_content(self) -> dict[str, Any]:
        warnings: list[str] = []
        discover_items: list[ContentItem] = []
        following_items: list[ContentItem] = []
        if self.domains:
            combined = " OR ".join(f"({domain.query})" for domain in self.domains)
            query = f"({combined}) min_faves:{self.settings.hot_content_min_engagement} -filter:replies"
            try:
                response = self.connector.search_content(
                    ContentSearchQuery(
                        query=query[:500],
                        sort="top",
                        limit=max(20, self.settings.x_hot_content_limit),
                    )
                )
                discover_items.extend(response.items)
                warnings.extend(response.warnings)
            except ConnectorError as exc:
                warnings.append(str(exc))

        try:
            tracked = self.connector.list_tracked_accounts(status="active", limit=50)
        except ConnectorError as exc:
            tracked = ConnectorResult(items=(), provider="backend_repository", attempted_providers=())
            warnings.append(str(exc))
        for account in tracked.items:
            try:
                response = self.connector.get_account_content(
                    AccountContentQuery(
                        account_ref=account.username,
                        limit=self.settings.x_watch_posts_per_account,
                    )
                )
            except ConnectorError as exc:
                warnings.append(f"@{account.username}: {exc}")
                continue
            following_items.extend(response.items)
            warnings.extend(response.warnings)

        unique = {
            item.native_id: item for item in [*discover_items, *following_items]
        }
        stored = self._persist_content(unique.values())
        return {
            "discover": len(discover_items),
            "following": len(following_items),
            "stored": stored,
            "warnings": list(dict.fromkeys(warnings)),
        }

    def hot_content(
        self,
        *,
        source: str,
        domain_key: str | None = None,
        limit: int = 30,
    ) -> ConnectorResult[HotContentItem]:
        source_mode = "following" if source == "following" else "discover"
        rows = self.repository.list_hot_content_observations(
            window_hours=self.settings.hot_content_window_hours
        )
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            grouped[str(row["id"])].append(row)
        domains = tuple(
            domain
            for domain in self.domains
            if domain_key is None or domain.key == domain_key
        )
        now = _utc_now()
        output: list[HotContentItem] = []
        for content_id, observations in grouped.items():
            latest = observations[0]
            following = str(latest.get("kol_status") or "") in {"seed", "active"}
            if source_mode == "following" and not following:
                continue
            if source_mode == "discover" and latest.get("reference_type") in {
                "retweeted",
                "replied_to",
            }:
                continue
            matched: list[str] = []
            relevance = 0.0
            normalized_text = str(latest.get("text") or "")
            for domain in domains:
                excluded = any(
                    value.casefold() in normalized_text.casefold()
                    for value in domain.exclusions
                )
                score = 0.0 if excluded else text_relevance(
                    normalized_text, domain_terms(domain.query)
                )
                if score >= 0.25:
                    matched.append(domain.key)
                    relevance = max(relevance, score)
            if source_mode == "discover" and domains and not matched:
                continue
            published = _parse_time(latest.get("created_at") or latest.get("captured_at"))
            if published is None:
                continue
            age_hours = max(0.0, (now - published).total_seconds() / 3600)
            if age_hours > self.settings.hot_content_window_hours:
                continue
            engagement_total = int(latest.get("like_count") or 0) + 2 * int(
                latest.get("repost_count") or 0
            ) + int(latest.get("reply_count") or 0) + 2 * int(
                latest.get("quote_count") or 0
            )
            threshold = (
                self.settings.hot_following_min_engagement
                if source_mode == "following"
                else self.settings.hot_content_min_engagement
            )
            if engagement_total < threshold:
                continue
            velocity = 0.0
            if len(observations) > 1:
                previous = observations[1]
                previous_engagement = int(previous.get("like_count") or 0) + 2 * int(
                    previous.get("repost_count") or 0
                ) + int(previous.get("reply_count") or 0) + 2 * int(
                    previous.get("quote_count") or 0
                )
                latest_at = _parse_time(latest.get("metric_captured_at"))
                previous_at = _parse_time(previous.get("metric_captured_at"))
                if latest_at and previous_at and latest_at > previous_at:
                    elapsed = max(1 / 60, (latest_at - previous_at).total_seconds() / 3600)
                    velocity = max(0.0, engagement_total - previous_engagement) / elapsed
            components = HotContentScoreComponents(
                velocity=min(100.0, math.log1p(velocity) * 22),
                engagement=min(100.0, math.log1p(engagement_total) * 13),
                recency=max(
                    0.0,
                    100 * (1 - age_hours / self.settings.hot_content_window_hours),
                ),
                relevance=min(100.0, relevance * 100),
                author_quality=100.0 if following else 50.0,
            )
            hot_score = round(
                0.35 * components.velocity
                + 0.30 * components.engagement
                + 0.15 * components.recency
                + 0.15 * components.relevance
                + 0.05 * components.author_quality,
                1,
            )
            observed_at = _parse_time(latest.get("metric_captured_at")) or now
            output.append(
                HotContentItem(
                    content=ContentItem(
                        platform="x",
                        native_id=content_id,
                        provider=str(latest.get("source_provider") or "backend_repository"),
                        author={
                            "native_id": str(latest.get("author_id") or "unknown"),
                            "username": latest.get("author_handle"),
                            "display_name": latest.get("display_name"),
                            "profile_url": latest.get("profile_url"),
                        },
                        text=normalized_text,
                        canonical_url=latest.get("url"),
                        published_at=published,
                        observed_at=observed_at,
                        language=latest.get("language"),
                        conversation_id=latest.get("conversation_id"),
                        reference_type=latest.get("reference_type"),
                        referenced_content_id=latest.get("referenced_tweet_id"),
                        metrics=ContentMetrics(
                            likes=max(0, int(latest.get("like_count") or 0)),
                            reposts=max(0, int(latest.get("repost_count") or 0)),
                            replies=max(0, int(latest.get("reply_count") or 0)),
                            quotes=max(0, int(latest.get("quote_count") or 0)),
                            bookmarks=max(0, int(latest.get("bookmark_count") or 0)),
                            views=max(0, int(latest.get("view_count") or 0)),
                        ),
                    ),
                    source=source_mode,
                    hot_score=hot_score,
                    engagement_total=engagement_total,
                    velocity_per_hour=round(velocity, 1),
                    matched_domains=tuple(matched),
                    score_components=components,
                    reason=(
                        "Rapid engagement growth"
                        if velocity > 0
                        else "Strong recent engagement"
                    ),
                )
            )
        output.sort(
            key=lambda item: (item.hot_score, item.engagement_total), reverse=True
        )
        return ConnectorResult(
            items=tuple(output[: max(1, min(limit, 100))]),
            provider="backend_repository",
            attempted_providers=(),
            observed_at=now,
        )


__all__ = ["DiscoverAutomationService", "parse_discovery_domains"]
