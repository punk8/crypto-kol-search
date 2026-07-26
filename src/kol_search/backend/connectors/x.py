from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from threading import RLock
from typing import TypeVar
from urllib.parse import quote_plus

from pydantic import BaseModel

from kol_search.backend.connectors.base import ConnectorError, ConnectorResult
from kol_search.backend.models import (
    AccountContentQuery,
    AccountItem,
    AccountSearchQuery,
    AccountTrackingItem,
    AccountSummary,
    ConnectorCapability,
    ContentItem,
    ContentLookupQuery,
    ContentMetrics,
    ContentSearchQuery,
    PlatformConnectorDescriptor,
    ProviderDescriptor,
    TrendItem,
    TrendQuery,
    TrackedAccountItem,
)
from kol_search.backend.kol_scoring import SCORE_VERSION, domain_terms, score_x_account, text_relevance
from kol_search.backend.provider_state import ProviderStateStore
from kol_search.platform_modules.x_native import XRepository
from kol_search.platforms.x import (
    XAccount as StoredXAccount,
    XTweet as StoredXTweet,
    XTweetMetrics as StoredXTweetMetrics,
)
from kol_search.settings import Settings
from kol_search.twitter.base import TwitterBackendError, XReadProvider
from kol_search.twitter.factory import create_x_read_provider
from kol_search.twitter.models import XAccount, XTrend, XTweet


T = TypeVar("T")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _provider_warning(
    provider: str,
    state: str,
    exc: Exception,
) -> str:
    status_code = getattr(exc, "status_code", None)
    status = f" (HTTP {status_code})" if status_code is not None else ""
    return f"{provider} {state}{status}"


def _parse_datetime(value: str | None) -> datetime | None:
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


def _trend_item(
    trend: XTrend,
    *,
    provider: str,
    category: str | None,
    locale: str | None,
    observed_at: datetime,
) -> TrendItem:
    source_provider = str(trend.raw.get("source_provider") or provider)
    rank = trend.rank if trend.rank > 0 else None
    post_count = trend.post_count if trend.post_count > 0 else None
    return TrendItem(
        platform="x",
        native_key=str(trend.raw.get("id") or trend.name.casefold()),
        name=trend.name,
        category=category,
        locale=locale,
        rank=rank,
        post_count=post_count,
        url=trend.url
        or f"https://x.com/search?q={quote_plus(trend.name)}&src=trend_click",
        provider=source_provider,
        native_semantics=True,
        observed_at=observed_at,
        context=(str(trend.raw.get("context")) if trend.raw.get("context") else None),
    )


def _content_item(tweet: XTweet, *, provider: str, observed_at: datetime) -> ContentItem:
    source_provider = (
        provider
        if not tweet.source_provider or tweet.source_provider == "unknown"
        else tweet.source_provider
    )
    username = tweet.author_username.lstrip("@") if tweet.author_username else None
    return ContentItem(
        platform="x",
        native_id=tweet.id,
        provider=source_provider,
        author=AccountSummary(
            native_id=tweet.author_id or username or "unknown",
            username=username,
            display_name=None,
            profile_url=f"https://x.com/{username}" if username else None,
        ),
        text=tweet.text,
        canonical_url=tweet.url
        or (f"https://x.com/{username}/status/{tweet.id}" if username else None),
        published_at=_parse_datetime(tweet.created_at),
        observed_at=observed_at,
        language=tweet.lang,
        conversation_id=tweet.conversation_id,
        reference_type=tweet.reference_type,
        referenced_content_id=tweet.referenced_post_id,
        metrics=ContentMetrics(
            likes=max(0, tweet.like_count),
            reposts=max(0, tweet.retweet_count),
            replies=max(0, tweet.reply_count),
            quotes=max(0, tweet.quote_count),
            views=max(0, tweet.view_count) if tweet.view_count else None,
            bookmarks=max(0, tweet.bookmark_count) if tweet.bookmark_count else None,
        ),
    )


class XConnector:
    """Platform connector that hides X provider selection from frontend clients."""

    platform_id = "x"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.provider_names = settings.x_provider_names()
        self._providers: dict[str, XReadProvider] = {}
        self._provider_lock = RLock()
        self._provider_state: ProviderStateStore | None = None
        self._provider_state_lock = RLock()
        self._repository: XRepository | None = None
        self._repository_lock = RLock()

    def _state_store(self) -> ProviderStateStore:
        with self._provider_state_lock:
            if self._provider_state is None:
                self._provider_state = ProviderStateStore(
                    self.settings.provider_state_database_target()
                )
            return self._provider_state

    def _state_repository(self) -> XRepository:
        with self._repository_lock:
            if self._repository is None:
                self._repository = XRepository(self.settings.database_target())
            return self._repository

    def descriptor(self) -> PlatformConnectorDescriptor:
        providers: list[ProviderDescriptor] = []
        for name in self.provider_names:
            ready, reason = self.settings.backend_ready(name)
            providers.append(
                ProviderDescriptor(
                    id=name,
                    ready=ready,
                    configured=ready,
                    role="primary" if not providers else "fallback",
                    detail=reason,
                )
            )
        return PlatformConnectorDescriptor(
            platform="x",
            name="X",
            capabilities=(
                "native_trends",
                "content_search",
                "account_search",
                "account_timeline",
                "content_lookup",
                "public_metrics",
            ),
            providers=providers,
        )

    def _provider(self, name: str) -> XReadProvider:
        with self._provider_lock:
            provider = self._providers.get(name)
            if provider is None:
                provider = create_x_read_provider(
                    name, self.settings, state_store=self._state_store()
                )
                self._providers[name] = provider
            return provider

    @staticmethod
    def _snapshot_key(operation: str, payload: str) -> str:
        digest = hashlib.sha256(f"x:{operation}:{payload}".encode()).hexdigest()
        return f"x:{operation}:{digest}"

    @staticmethod
    def _snapshot_payload(result: ConnectorResult[T]) -> dict[str, object]:
        return {
            "items": [
                item.model_dump(mode="json")
                if isinstance(item, BaseModel)
                else item
                for item in result.items
            ],
            "provider": result.provider,
            "attempted_providers": list(result.attempted_providers),
            "warnings": list(result.warnings),
            "observed_at": result.observed_at.isoformat(),
        }

    @staticmethod
    def _snapshot_result(
        payload: dict[str, object],
        *,
        item_model: type[T],
        stale: bool,
    ) -> ConnectorResult[T]:
        raw_items = payload.get("items") or []
        items = tuple(item_model.model_validate(item) for item in raw_items)
        observed_at = _parse_datetime(str(payload.get("observed_at") or ""))
        cache_warning = (
            "served from stale backend snapshot"
            if stale
            else "served from backend snapshot cache"
        )
        return ConnectorResult(
            items=items,
            provider=str(payload.get("provider") or "backend_snapshot"),
            attempted_providers=tuple(payload.get("attempted_providers") or ()),
            warnings=tuple(
                dict.fromkeys([*(payload.get("warnings") or ()), cache_warning])
            ),
            observed_at=observed_at or _utc_now(),
        )

    def _with_snapshot(
        self,
        operation: str,
        payload: str,
        *,
        item_model: type[T],
        ttl_seconds: int,
        loader: Callable[[], ConnectorResult[T]],
    ) -> ConnectorResult[T]:
        if ttl_seconds <= 0:
            return loader()
        key = self._snapshot_key(operation, payload)
        store = self._state_store()
        cached = store.get_snapshot(key)
        if cached is not None:
            value, fresh = cached
            if fresh:
                try:
                    return self._snapshot_result(
                        value, item_model=item_model, stale=False
                    )
                except (TypeError, ValueError):
                    # Ignore an incompatible snapshot after a schema/model
                    # change and replace it with a live response below.
                    pass
        try:
            result = loader()
        except ConnectorError:
            stale = store.get_snapshot(
                key, stale_seconds=self.settings.x_snapshot_stale_seconds
            )
            if stale is None:
                raise
            value, _ = stale
            try:
                return self._snapshot_result(value, item_model=item_model, stale=True)
            except (TypeError, ValueError):
                raise
        store.put_snapshot(
            key,
            platform="x",
            operation=operation,
            provider=result.provider,
            payload=self._snapshot_payload(result),
            observed_at=result.observed_at,
            ttl_seconds=ttl_seconds,
        )
        return result

    def _run_with_fallback(
        self,
        capability: ConnectorCapability,
        operation: Callable[[XReadProvider], list[T]],
        *,
        accept_empty: bool = True,
    ) -> ConnectorResult[T]:
        attempted: list[str] = []
        warnings: list[str] = []
        for name in self.provider_names:
            attempted.append(name)
            try:
                provider = self._provider(name)
            except (TwitterBackendError, RuntimeError, ValueError, TypeError, KeyError) as exc:
                warnings.append(_provider_warning(name, "unavailable", exc))
                continue

            capabilities = provider.capabilities
            supported = {
                "native_trends": capabilities.trends,
                "content_search": capabilities.post_search,
                "account_search": capabilities.user_search or capabilities.post_search,
                "account_timeline": capabilities.user_timeline,
                "content_lookup": capabilities.post_lookup,
                "public_metrics": True,
            }[capability]
            if not supported:
                warnings.append(f"{name} does not support {capability}")
                continue
            try:
                items = operation(provider)
            except (TwitterBackendError, ValueError, TypeError, KeyError) as exc:
                warnings.append(_provider_warning(name, "failed", exc))
                continue
            if not items and not accept_empty:
                warnings.append(f"{name} returned no {capability} items")
                continue
            return ConnectorResult(
                items=tuple(items),
                provider=provider.name,
                attempted_providers=tuple(attempted),
                warnings=tuple(warnings),
            )
        providers = ", ".join(attempted) or "none"
        raise ConnectorError(
            f"X connector could not serve {capability}; attempted providers: {providers}"
        )

    def get_trends(self, query: TrendQuery) -> ConnectorResult[TrendItem]:
        def load() -> ConnectorResult[TrendItem]:
            observed_at = _utc_now()

            def operation(provider: XReadProvider) -> list[TrendItem]:
                trends = provider.get_trends(
                    query.limit,
                    category=query.category,
                    locale=query.locale,
                )
                return [
                    _trend_item(
                        trend,
                        provider=provider.name,
                        category=None,
                        locale=None,
                        observed_at=observed_at,
                    )
                    for trend in trends
                ]

            return self._run_with_fallback(
                "native_trends", operation, accept_empty=False
            )

        # Neither current X provider sends category or locale upstream. Share a
        # single native-trend snapshot across the UI filters, then attach the
        # requested display labels. This avoids paying again when a user moves
        # between category tabs.
        raw_result = self._with_snapshot(
            "trends",
            TrendQuery(limit=query.limit).model_dump_json(),
            item_model=TrendItem,
            ttl_seconds=self.settings.x_trend_cache_seconds,
            loader=load,
        )
        return ConnectorResult(
            items=tuple(
                item.model_copy(
                    update={"category": query.category, "locale": query.locale}
                )
                for item in raw_result.items
            ),
            provider=raw_result.provider,
            attempted_providers=raw_result.attempted_providers,
            warnings=raw_result.warnings,
            observed_at=raw_result.observed_at,
        )

    def search_content(
        self, query: ContentSearchQuery
    ) -> ConnectorResult[ContentItem]:
        def load() -> ConnectorResult[ContentItem]:
            observed_at = _utc_now()

            def operation(provider: XReadProvider) -> list[ContentItem]:
                posts = provider.search_tweets(
                    query.query,
                    max_results=query.limit,
                    since_id=query.since_id,
                    start_time=(
                        query.start_time.isoformat() if query.start_time else None
                    ),
                )
                if query.sort == "top":
                    posts.sort(key=lambda post: post.engagement, reverse=True)
                return [
                    _content_item(
                        post, provider=provider.name, observed_at=observed_at
                    )
                    for post in posts
                ]

            return self._run_with_fallback("content_search", operation)

        return self._with_snapshot(
            "content_search",
            query.model_dump_json(),
            item_model=ContentItem,
            ttl_seconds=self.settings.x_content_cache_seconds,
            loader=load,
        )

    @staticmethod
    def _account_search_expression(query: AccountSearchQuery) -> str:
        values = [f"({query.query})"]
        if query.min_engagement:
            values.append(f"min_faves:{query.min_engagement}")
        values.append("-filter:replies")
        if query.language:
            values.append(f"lang:{query.language}")
        return " ".join(values)

    def _persist_account_candidate(
        self,
        *,
        account: XAccount,
        posts: list[XTweet],
        item: AccountItem,
        query: AccountSearchQuery,
    ) -> None:
        repository = self._state_repository()
        captured_at = item.observed_at.isoformat()
        account_id = repository.upsert_account(
            StoredXAccount(
                external_id=account.id,
                username=account.handle,
                display_name=account.name,
                description=account.description,
                followers_count=max(0, account.followers_count),
                following_count=max(0, account.following_count),
                tweet_count=max(0, account.tweet_count),
                listed_count=max(0, account.listed_count),
                verified=account.verified,
                protected=account.protected,
                created_at=account.created_at,
                profile_image_url=account.profile_image_url,
                url=f"https://x.com/{account.handle}",
                source_provider=item.provider,
                captured_at=captured_at,
            )
        )
        terms = domain_terms(query.query)
        stored_posts = [
            StoredXTweet(
                external_id=post.id,
                author_external_id=account_id,
                author_username=account.handle,
                text=post.text,
                created_at=post.created_at,
                language=post.lang,
                url=post.url,
                conversation_id=post.conversation_id,
                in_reply_to_user_id=post.in_reply_to_user_id,
                referenced_tweet_id=post.referenced_post_id,
                reference_type=post.reference_type,
                mentioned_usernames=post.mentioned_usernames,
                metrics=StoredXTweetMetrics(
                    likes=max(0, post.like_count),
                    reposts=max(0, post.retweet_count),
                    replies=max(0, post.reply_count),
                    quotes=max(0, post.quote_count),
                    bookmarks=max(0, post.bookmark_count),
                    views=max(0, post.view_count),
                ),
                relevance_score=text_relevance(post.text, terms),
                source_provider=item.provider,
                captured_at=captured_at,
            )
            for post in {value.id: value for value in posts}.values()
        ]
        repository.upsert_tweets(stored_posts)
        domain_key = hashlib.sha256(query.query.casefold().encode()).hexdigest()[:16]
        repository.record_evidence(
            [
                {
                    "target_account_id": account_id,
                    "relation_type": f"domain_content:{domain_key}",
                    "evidence": evidence.native_content_id or evidence.summary,
                    "evidence_url": evidence.url,
                    "weight": 0.35 if evidence.kind == "domain_content" else 0.15,
                    "observed_at": captured_at,
                }
                for evidence in item.evidence
            ]
        )
        reasons = tuple(
            f"{component.label}: {component.score}/100 — {component.reason}"
            for component in item.score_components
        )
        current = repository.get_kol(account_id) or {}
        current_status = str(current.get("status") or "candidate")
        repository.set_kol_status(
            account_id,
            current_status if current_status in {"seed", "active"} else "candidate",
            score=item.score / 100,
            reasons=(
                tuple(current.get("reasons") or ())
                if current_status in {"seed", "active"}
                else (
                    f"score_version={item.score_version}",
                    f"domain={query.query}",
                    *reasons,
                )
            ),
            qualified=current_status in {"seed", "active"},
        )
        repository.record_domain_score_snapshot(
            account_id=account_id,
            domain_key=domain_key,
            domain_query=query.query,
            score=item.score,
            confidence=item.confidence,
            score_version=item.score_version,
            components=[value.model_dump(mode="json") for value in item.score_components],
            evidence=[value.model_dump(mode="json") for value in item.evidence],
            observed_at=captured_at,
        )

    def _search_accounts_uncached(
        self, query: AccountSearchQuery
    ) -> ConnectorResult[AccountItem]:
        observed_at = _utc_now()
        partial_warnings: list[str] = []

        def operation(provider: XReadProvider) -> list[AccountItem]:
            def hydration_providers() -> tuple[XReadProvider, ...]:
                """Prefer free profile/timeline reads after paid candidate recall."""

                values: list[XReadProvider] = []
                for name in self.provider_names:
                    try:
                        candidate = self._provider(name)
                    except (TwitterBackendError, RuntimeError, ValueError, TypeError, KeyError):
                        continue
                    if candidate not in values:
                        values.append(candidate)
                if provider not in values:
                    values.append(provider)
                return tuple(values)

            hydration_chain = hydration_providers()
            failures: list[TwitterBackendError] = []
            direct_accounts: list[XAccount] = []
            domain_posts: list[XTweet] = []
            if provider.capabilities.user_search:
                try:
                    direct_accounts = provider.search_users(
                        query.query, max_results=min(30, query.limit * 2)
                    )
                except TwitterBackendError as exc:
                    failures.append(exc)
                    partial_warnings.append("Direct X account search was unavailable")
            if provider.capabilities.post_search:
                try:
                    domain_posts = provider.search_tweets(
                        self._account_search_expression(query),
                        max_results=query.sample_size,
                    )
                except TwitterBackendError as exc:
                    failures.append(exc)
                    partial_warnings.append("Domain content search was unavailable")

            accounts_by_handle = {account.handle.casefold(): account for account in direct_accounts}
            posts_by_handle: dict[str, list[XTweet]] = {}
            for post in domain_posts:
                if not post.author_username:
                    continue
                key = post.author_username.lstrip("@").casefold()
                posts_by_handle.setdefault(key, []).append(post)

            ranked_handles = sorted(
                set(accounts_by_handle) | set(posts_by_handle),
                key=lambda handle: (
                    max((post.engagement for post in posts_by_handle.get(handle, [])), default=0),
                    accounts_by_handle.get(handle).followers_count
                    if handle in accounts_by_handle
                    else 0,
                ),
                reverse=True,
            )[: min(30, max(query.limit * 2, query.limit))]

            candidates: list[AccountItem] = []
            for handle in ranked_handles:
                account = accounts_by_handle.get(handle)
                if account is None:
                    for hydration_provider in hydration_chain:
                        try:
                            account = hydration_provider.get_user_by_username(handle)
                        except TwitterBackendError:
                            continue
                        if account is not None:
                            break
                    if account is None:
                        partial_warnings.append(f"@{handle} profile was unavailable")
                author_domain_posts = posts_by_handle.get(handle, [])
                if account is None and author_domain_posts:
                    sample = author_domain_posts[0]
                    account = XAccount(
                        id=sample.author_id,
                        username=sample.author_username or handle,
                        source_provider=provider.name,
                    )
                if account is None or not account.id:
                    continue

                recent_posts: list[XTweet] = []
                for timeline_provider in hydration_chain:
                    if not timeline_provider.capabilities.user_timeline:
                        continue
                    try:
                        recent_posts = timeline_provider.get_user_tweets(
                            account.id,
                            max_results=query.recent_posts_per_account,
                            username=account.handle,
                        )
                    except TwitterBackendError:
                        continue
                    if recent_posts:
                        break
                if not recent_posts and provider.capabilities.user_timeline:
                    partial_warnings.append(f"@{account.handle} timeline was unavailable")
                if not recent_posts:
                    recent_posts = author_domain_posts[: query.recent_posts_per_account]

                scored = score_x_account(
                    account,
                    query=query.query,
                    domain_posts=author_domain_posts,
                    recent_posts=recent_posts,
                    observed_at=observed_at,
                )
                source_provider = (
                    account.source_provider
                    if account.source_provider != "unknown"
                    else provider.name
                )
                item = AccountItem(
                    platform="x",
                    native_id=account.id,
                    provider=source_provider,
                    username=account.handle,
                    display_name=account.name,
                    description=account.description,
                    profile_url=f"https://x.com/{account.handle}",
                    profile_image_url=account.profile_image_url,
                    followers_count=max(0, account.followers_count),
                    following_count=max(0, account.following_count),
                    post_count=max(0, account.tweet_count),
                    listed_count=max(0, account.listed_count),
                    verified=account.verified,
                    protected=account.protected,
                    score=scored.score,
                    confidence=scored.confidence,
                    score_version=SCORE_VERSION,
                    score_components=list(scored.components),
                    evidence=list(scored.evidence),
                    recent_content=[
                        _content_item(post, provider=provider.name, observed_at=observed_at)
                        for post in recent_posts
                    ],
                    observed_at=observed_at,
                )
                try:
                    self._persist_account_candidate(
                        account=account,
                        posts=[*author_domain_posts, *recent_posts],
                        item=item,
                        query=query,
                    )
                except Exception:
                    partial_warnings.append(
                        f"@{account.handle} score persistence was unavailable"
                    )
                candidates.append(item)

            if not candidates and failures:
                raise failures[-1]
            candidates.sort(
                key=lambda item: (item.score, item.followers_count), reverse=True
            )
            return candidates[: query.limit]

        result = self._run_with_fallback("account_search", operation)
        response = ConnectorResult(
            items=result.items,
            provider=result.provider,
            attempted_providers=result.attempted_providers,
            warnings=tuple(dict.fromkeys([*result.warnings, *partial_warnings])),
            observed_at=result.observed_at,
        )
        return response

    def search_accounts(
        self, query: AccountSearchQuery
    ) -> ConnectorResult[AccountItem]:
        return self._with_snapshot(
            "account_search",
            query.model_dump_json(),
            item_model=AccountItem,
            ttl_seconds=self.settings.x_kol_discovery_cache_seconds,
            loader=lambda: self._search_accounts_uncached(query),
        )

    def get_account_content(
        self, query: AccountContentQuery
    ) -> ConnectorResult[ContentItem]:
        def load() -> ConnectorResult[ContentItem]:
            observed_at = _utc_now()

            def operation(provider: XReadProvider) -> list[ContentItem]:
                reference = query.account_ref.lstrip("@")
                username = None if reference.isdigit() else reference
                user_id = reference
                if username:
                    account = provider.get_user_by_username(username)
                    if account is None:
                        return []
                    user_id = account.id
                posts = provider.get_user_tweets(
                    user_id,
                    max_results=query.limit,
                    username=username,
                    include_replies=query.include_replies,
                    start_time=(
                        query.start_time.isoformat() if query.start_time else None
                    ),
                )
                return [
                    _content_item(
                        post, provider=provider.name, observed_at=observed_at
                    )
                    for post in posts
                ]

            return self._run_with_fallback("account_timeline", operation)

        return self._with_snapshot(
            "account_timeline",
            query.model_dump_json(),
            item_model=ContentItem,
            ttl_seconds=self.settings.x_timeline_cache_seconds,
            loader=load,
        )

    def set_account_tracking(
        self, native_id: str, *, enabled: bool
    ) -> ConnectorResult[AccountTrackingItem]:
        repository = self._state_repository()
        account = repository.get_kol(native_id)
        if account is None:
            raise ConnectorError(
                "The X account must be discovered before it can be tracked."
            )
        status = "active" if enabled else "paused"
        repository.set_kol_status(
            native_id,
            status,
            score=float(account.get("score") or 0),
            reasons=tuple(account.get("reasons") or ()),
            qualified=enabled,
        )
        observed_at = _utc_now()
        return ConnectorResult(
            items=(
                AccountTrackingItem(
                    platform="x",
                    native_id=native_id,
                    status=status,
                    updated_at=observed_at,
                ),
            ),
            provider="backend_repository",
            attempted_providers=(),
            observed_at=observed_at,
        )

    def list_tracked_accounts(
        self, *, status: str = "active", limit: int = 100
    ) -> ConnectorResult[TrackedAccountItem]:
        allowed = {"seed", "active", "paused"}
        if status not in allowed:
            raise ConnectorError("Tracked account status is invalid.")
        rows = self._state_repository().list_kols(status=status, limit=limit)
        observed_at = _utc_now()
        items = tuple(
            TrackedAccountItem(
                platform="x",
                native_id=str(row["id"]),
                username=str(row["handle"]),
                display_name=row.get("display_name"),
                profile_url=row.get("profile_url") or f"https://x.com/{row['handle']}",
                status=status,
                score=max(0.0, min(1.0, float(row.get("score") or 0))),
            )
            for row in rows
        )
        return ConnectorResult(
            items=items,
            provider="backend_repository",
            attempted_providers=(),
            observed_at=observed_at,
        )

    def get_content(
        self, query: ContentLookupQuery
    ) -> ConnectorResult[ContentItem]:
        def load() -> ConnectorResult[ContentItem]:
            observed_at = _utc_now()

            def operation(provider: XReadProvider) -> list[ContentItem]:
                post = provider.get_tweet(query.native_id)
                if post is None:
                    return []
                return [
                    _content_item(
                        post, provider=provider.name, observed_at=observed_at
                    )
                ]

            return self._run_with_fallback("content_lookup", operation)

        return self._with_snapshot(
            "content_lookup",
            query.model_dump_json(),
            item_model=ContentItem,
            ttl_seconds=self.settings.x_content_lookup_cache_seconds,
            loader=load,
        )

    def provider_usage(self, *, refresh: bool = False) -> list[dict[str, object]]:
        output: list[dict[str, object]] = []
        for name in self.provider_names:
            if name != "getxapi":
                continue
            try:
                provider = self._provider(name)
                usage_status = getattr(provider, "usage_status", None)
                if callable(usage_status):
                    output.append(usage_status(refresh=refresh))
            except (TwitterBackendError, RuntimeError, ValueError, TypeError):
                output.append(
                    {
                        "provider": name,
                        "state": "unavailable",
                        "requests_today": self._state_store().usage_today(name),
                        "daily_limit": self.settings.get_x_api_daily_call_limit,
                    }
                )
        return output

    def close(self) -> None:
        with self._provider_lock:
            providers = tuple(self._providers.values())
            self._providers.clear()
        for provider in providers:
            close = getattr(provider, "close", None)
            if callable(close):
                close()
        with self._repository_lock:
            repository, self._repository = self._repository, None
        if repository is not None:
            repository.close()
        with self._provider_state_lock:
            provider_state, self._provider_state = self._provider_state, None
        if provider_state is not None:
            provider_state.close()
