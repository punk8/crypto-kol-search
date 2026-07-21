from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from kol_search.platforms.kernel import (
    ActionExecutionResult,
    PlatformCapability,
    PlatformHealthResult,
    PlatformManifest,
    PlatformOutcomeUpdate,
    PlatformPlugin,
    PlatformTaskContext,
    PlatformTaskName,
    PlatformTaskResult,
)
from kol_search.twitter.base import merge_client_diagnostics


class XAccount(BaseModel):
    """X-native account shape; it is not a shared cross-platform account model."""

    external_id: str
    username: str
    display_name: str | None = None
    description: str | None = None
    followers_count: int = 0
    following_count: int = 0
    tweet_count: int = 0
    listed_count: int = 0
    verified: bool = False
    protected: bool = False
    disabled: bool = False
    spam_risk: float = Field(default=0.0, ge=0.0, le=1.0)
    anomaly_signals: tuple[str, ...] = ()
    created_at: str | None = None
    profile_image_url: str | None = None
    url: str | None = None
    source_provider: str = "unknown"
    captured_at: str | None = None


class XTweetMetrics(BaseModel):
    likes: int = 0
    reposts: int = 0
    replies: int = 0
    quotes: int = 0
    bookmarks: int = 0
    views: int = 0


class XTweet(BaseModel):
    """X-native content model kept out of the shared automation schema."""

    external_id: str
    author_external_id: str
    author_username: str | None = None
    text: str = ""
    created_at: str | None = None
    language: str | None = None
    url: str | None = None
    conversation_id: str | None = None
    in_reply_to_user_id: str | None = None
    referenced_tweet_id: str | None = None
    reference_type: str | None = None
    mentioned_usernames: list[str] = Field(default_factory=list)
    metrics: XTweetMetrics = Field(default_factory=XTweetMetrics)
    relevance_score: float = Field(default=0.0, ge=0.0, le=1.0)
    source_provider: str = "unknown"
    captured_at: str | None = None


class XTrend(BaseModel):
    name: str
    rank: int = 0
    post_count: int = 0
    url: str | None = None


class XTrendTweet(BaseModel):
    """A concrete tweet returned while expanding one native trend."""

    trend_name: str
    trend_rank: int = 0
    tweet: XTweet


class XAccountRelation(BaseModel):
    """Explainable X relation from a trusted discovery source to one account."""

    source_external_id: str
    source_status: str
    target: XAccount
    relation_type: str = "following"
    evidence_external_id: str | None = None
    evidence_url: str | None = None


X_MANIFEST = PlatformManifest(
    platform_id="x",
    name="X",
    version="1.0",
    capabilities=frozenset(
        {
            PlatformCapability.ACCOUNT_SEARCH,
            PlatformCapability.CONTENT_SEARCH,
            PlatformCapability.TIMELINE_FEED,
            PlatformCapability.RELATIONS,
            PlatformCapability.NATIVE_TRENDS,
            PlatformCapability.COMMENT,
            PlatformCapability.DM,
            PlatformCapability.OWNED_PUBLISH,
            PlatformCapability.ANALYTICS,
        }
    ),
    default_scan_interval_seconds=30 * 60,
    safety_limits={
        "comment_hourly": 3,
        "comment_daily": 10,
        "dm_hourly": 2,
        "dm_daily": 5,
        "author_cooldown_days": 7,
        "owned_publish_daily": 2,
    },
    workbench_path="/platforms/x",
    metadata={
        "content_types": ("tweet", "reply", "quote", "repost"),
        "accent_color": "#111111",
        "short_name": "X",
        "publishing_bridge": "postiz",
        "feed_label": "内容动态",
        "workspace_template": "platform_x_workspace.html",
    },
)


def _attr(source: object, name: str, default: Any = None) -> Any:
    if isinstance(source, Mapping):
        return source.get(name, default)
    return getattr(source, name, default)


def _truthy_signal(source: object, raw: Mapping[str, Any], *names: str) -> bool:
    def enabled(value: object) -> bool:
        if isinstance(value, str):
            return value.strip().casefold() in {"1", "true", "yes", "y", "on"}
        return bool(value)

    return any(
        enabled(_attr(source, name, False)) or enabled(raw.get(name)) for name in names
    )


def _x_account_risk(source: object) -> tuple[bool, float, tuple[str, ...]]:
    """Normalize explicit provider flags and conservative X-local anomaly rules."""

    raw_value = _attr(source, "raw", {}) or {}
    raw = raw_value if isinstance(raw_value, Mapping) else {}
    status = str(_attr(source, "status", None) or raw.get("status") or "").casefold()
    disabled = _truthy_signal(
        source,
        raw,
        "disabled",
        "suspended",
        "deactivated",
        "deleted",
    ) or status in {"disabled", "suspended", "deactivated", "deleted", "unavailable"}

    signal_values = _attr(source, "anomaly_signals", None) or raw.get(
        "anomaly_signals", ()
    )
    signals = [str(value) for value in signal_values if str(value)]
    explicit = _attr(source, "spam_risk", None)
    if explicit is None:
        explicit = _attr(source, "spamRisk", None)
    if explicit is None:
        explicit = raw.get("spam_risk", raw.get("spamRisk", 0.0))
    try:
        risk = min(1.0, max(0.0, float(explicit or 0.0)))
    except (TypeError, ValueError, OverflowError):
        risk = 0.0

    followers = max(0, int(_attr(source, "followers_count", 0) or 0))
    following = max(0, int(_attr(source, "following_count", 0) or 0))
    tweet_count = max(0, int(_attr(source, "tweet_count", 0) or 0))
    bio = str(_attr(source, "description", "") or "").casefold()
    if following >= 1_000 and following > max(50, followers * 20):
        signals.append("extreme_follow_ratio")
        risk += 0.35
    if following >= 500 and tweet_count == 0:
        signals.append("mass_following_without_content")
        risk += 0.35
    if any(
        phrase in bio
        for phrase in (
            "guaranteed profit",
            "free airdrop",
            "dm for promo",
            "稳赚",
            "带单",
            "返利",
        )
    ):
        signals.append("spam_phrase_in_bio")
        risk += 0.70
    if disabled:
        signals.append("provider_disabled")
    return disabled, min(1.0, risk), tuple(dict.fromkeys(signals))


def _x_account(source: object) -> XAccount:
    external_id = str(_attr(source, "external_id") or _attr(source, "id") or "")
    username = str(_attr(source, "username") or "").lstrip("@")
    disabled, spam_risk, anomaly_signals = _x_account_risk(source)
    return XAccount(
        external_id=external_id,
        username=username,
        display_name=_attr(source, "name"),
        description=_attr(source, "description"),
        followers_count=int(_attr(source, "followers_count", 0) or 0),
        following_count=int(_attr(source, "following_count", 0) or 0),
        tweet_count=int(_attr(source, "tweet_count", 0) or 0),
        listed_count=int(_attr(source, "listed_count", 0) or 0),
        verified=bool(_attr(source, "verified", False)),
        protected=bool(_attr(source, "protected", False)),
        disabled=disabled,
        spam_risk=spam_risk,
        anomaly_signals=anomaly_signals,
        created_at=_attr(source, "created_at"),
        profile_image_url=_attr(source, "profile_image_url"),
        url=_attr(source, "url") or (f"https://x.com/{username}" if username else None),
        source_provider=str(_attr(source, "source_provider", "unknown") or "unknown"),
        captured_at=_attr(source, "captured_at"),
    )


def _x_tweet(source: object) -> XTweet:
    return XTweet(
        external_id=str(_attr(source, "external_id") or _attr(source, "id") or ""),
        author_external_id=str(_attr(source, "author_id") or ""),
        author_username=_attr(source, "author_username"),
        text=str(_attr(source, "text") or ""),
        created_at=_attr(source, "created_at"),
        language=_attr(source, "lang"),
        url=_attr(source, "url"),
        conversation_id=_attr(source, "conversation_id"),
        in_reply_to_user_id=_attr(source, "in_reply_to_user_id"),
        referenced_tweet_id=_attr(source, "referenced_post_id"),
        reference_type=_attr(source, "reference_type"),
        mentioned_usernames=list(_attr(source, "mentioned_usernames", []) or []),
        metrics=XTweetMetrics(
            likes=int(_attr(source, "like_count", 0) or 0),
            reposts=int(_attr(source, "retweet_count", 0) or 0),
            replies=int(_attr(source, "reply_count", 0) or 0),
            quotes=int(_attr(source, "quote_count", 0) or 0),
            bookmarks=int(_attr(source, "bookmark_count", 0) or 0),
            views=int(_attr(source, "view_count", 0) or 0),
        ),
        source_provider=str(_attr(source, "source_provider", "unknown") or "unknown"),
        captured_at=_attr(source, "captured_at"),
    )


def _x_trend(source: object) -> XTrend:
    return XTrend(
        name=str(_attr(source, "name") or ""),
        rank=int(_attr(source, "rank", 0) or 0),
        post_count=int(_attr(source, "post_count", 0) or 0),
        url=_attr(source, "url"),
    )


def _native_interaction_type(
    post: object,
    *,
    source_id: str,
    source_username: str,
) -> str | None:
    """Return only interactions that can be tied to the trusted source account."""

    normalized_username = source_username.casefold().lstrip("@")
    reply_id = str(_attr(post, "in_reply_to_user_id") or "")
    reply_username = str(_attr(post, "in_reply_to_username") or "").casefold().lstrip("@")
    if (source_id and reply_id == source_id) or (
        normalized_username and reply_username == normalized_username
    ):
        return "reply"
    reference_type = str(_attr(post, "reference_type") or "").casefold()
    if reference_type in {"quoted", "quote"}:
        # The provider query is scoped to this account's status URLs. A quoted
        # result therefore supplies a native quote edge even when the provider
        # does not expose the referenced author's ID.
        return "quote"
    mentions = {
        str(value).casefold().lstrip("@")
        for value in (_attr(post, "mentioned_usernames", ()) or ())
    }
    if normalized_username and normalized_username in mentions:
        return "mention"
    return None


class _XRuntime:
    def __init__(
        self,
        settings: object,
        client_factory: Callable[[], object] | None,
    ) -> None:
        self.settings = settings
        self._client_factory = client_factory
        self._client: object | None = None

    def client(self) -> object:
        if self._client is None:
            if self._client_factory is not None:
                self._client = self._client_factory()
            else:
                from kol_search.twitter.factory import create_x_read_provider

                self._client = create_x_read_provider(
                    getattr(self.settings, "twitter_backend", None),
                    self.settings,  # type: ignore[arg-type]
                )
        return self._client

    def close(self) -> None:
        if self._client is not None:
            close = getattr(self._client, "close", None)
            if callable(close):
                close()
            self._client = None

    def health(self) -> PlatformHealthResult:
        client = self.client()
        health_check = getattr(client, "health_check", None)
        if callable(health_check):
            result = health_check()
            return PlatformHealthResult(
                platform_id="x",
                ready=bool(_attr(result, "ready", False)),
                account=_attr(result, "account"),
                detail=_attr(result, "detail"),
            )
        run = getattr(client, "_run", None)
        if callable(run):
            rows = run("whoami")
            row = rows[0] if rows else {}
            account = str(
                _attr(row, "screen_name") or _attr(row, "username") or ""
            ).lstrip("@")
            return PlatformHealthResult(
                platform_id="x",
                ready=bool(account),
                account=account or None,
                detail=None if account else "X account is not connected",
            )
        provider = str(getattr(client, "name", "x") or "x")
        return PlatformHealthResult(
            platform_id="x",
            ready=True,
            detail=f"{provider} backend configured",
        )

    def discover(self, context: PlatformTaskContext) -> PlatformTaskResult:
        client = self.client()
        _begin_read_run(client)
        limit = max(1, min(int(context.payload.get("limit", 20)), 100))
        queries = _queries(context.payload)
        seed_accounts = tuple(
            item
            for item in (context.payload.get("seed_accounts") or ())
            if isinstance(item, Mapping)
        )
        relation_sources = tuple(
            (
                str(item.get("id") or item.get("external_id") or ""),
                str(item.get("handle") or item.get("username") or ""),
                str(item.get("status") or "unknown"),
            )
            for item in seed_accounts
            if str(item.get("handle") or item.get("username") or "").strip()
        )
        if not relation_sources:
            relation_sources = tuple(
                (value, value, "unknown")
                for value in _strings(
                    context.payload.get("seed_usernames")
                    or context.payload.get("seeds")
                    or ()
                )
            )
        items: list[object] = []
        warnings: list[str] = []
        attempted = 0
        succeeded = 0

        search_accounts = bool(context.payload.get("search_accounts", True))
        search_content = bool(context.payload.get("search_content", True))
        for query in queries:
            if search_accounts and callable(getattr(client, "search_users", None)):
                attempted += 1
                try:
                    items.extend(_x_account(item) for item in client.search_users(query, limit))
                    succeeded += 1
                except Exception as exc:
                    warnings.append(f"account search {query!r} failed: {exc}")
            if search_content:
                attempted += 1
                try:
                    items.extend(
                        _x_tweet(item)
                        for item in client.search_tweets(query, limit)  # type: ignore[attr-defined]
                    )
                    succeeded += 1
                except Exception as exc:
                    warnings.append(f"content search {query!r} failed: {exc}")

        for source_id, username, source_status in relation_sources:
            followings = getattr(client, "get_followings", None)
            capabilities = getattr(client, "capabilities", None)
            supports_followings = capabilities is None or bool(
                getattr(capabilities, "followings", False)
            )
            if callable(followings) and supports_followings:
                attempted += 1
                try:
                    items.extend(
                        XAccountRelation(
                            source_external_id=source_id or username,
                            source_status=source_status,
                            target=_x_account(item),
                            relation_type="following",
                        )
                        for item in followings(username, limit)
                    )
                    succeeded += 1
                except Exception as exc:
                    warnings.append(f"followings for @{username} failed: {exc}")
            else:
                warnings.append("configured X backend does not expose followings")

            verified_followers = getattr(client, "get_verified_followers", None)
            supports_verified_followers = capabilities is None or bool(
                getattr(capabilities, "verified_followers", False)
            )
            if callable(verified_followers) and supports_verified_followers:
                attempted += 1
                try:
                    items.extend(
                        XAccountRelation(
                            source_external_id=source_id or username,
                            source_status=source_status,
                            target=_x_account(item),
                            relation_type="verified_follower",
                        )
                        for item in verified_followers(
                            source_id or username,
                            limit,
                            username=username,
                        )
                    )
                    succeeded += 1
                except Exception as exc:
                    warnings.append(f"verified followers for @{username} failed: {exc}")

            # Search is an existing provider capability shared by all production
            # X readers. It exposes inbound mentions/replies/quotes without adding
            # a second relation-specific provider abstraction.
            search_tweets = getattr(client, "search_tweets", None)
            if callable(search_tweets) and source_status in {
                "seed",
                "active",
            }:
                attempted += 1
                try:
                    native_posts = list(
                        search_tweets(
                            (
                                f'(to:{username} OR @{username} OR '
                                f'url:"x.com/{username}/status") -from:{username}'
                            ),
                            limit,
                        )
                    )
                    interaction_rows = [
                        (post, kind)
                        for post in native_posts
                        if (
                            kind := _native_interaction_type(
                                post,
                                source_id=source_id,
                                source_username=username,
                            )
                        )
                    ]
                    handles = tuple(
                        dict.fromkeys(
                            str(_attr(post, "author_username") or "").lstrip("@")
                            for post, _kind in interaction_rows
                            if str(_attr(post, "author_username") or "").strip()
                        )
                    )
                    profiles: dict[str, XAccount] = {}
                    lookup = getattr(client, "get_users_by_usernames", None)
                    if handles and callable(lookup):
                        try:
                            profiles = {
                                account.username.casefold(): account
                                for account in (_x_account(value) for value in lookup(list(handles)))
                            }
                        except Exception as exc:
                            warnings.append(
                                f"interaction profile hydration for @{username} failed: {exc}"
                            )
                    for post, kind in interaction_rows:
                        author_id = str(_attr(post, "author_id") or "")
                        author_username = str(
                            _attr(post, "author_username") or author_id
                        ).lstrip("@")
                        if not author_id or author_id == source_id or (
                            author_username.casefold() == username.casefold().lstrip("@")
                        ):
                            continue
                        target = profiles.get(author_username.casefold()) or XAccount(
                            external_id=author_id,
                            username=author_username,
                            source_provider=str(
                                _attr(post, "source_provider", "unknown") or "unknown"
                            ),
                            captured_at=_attr(post, "captured_at"),
                            url=f"https://x.com/{author_username}" if author_username else None,
                        )
                        converted = _x_tweet(post)
                        items.extend(
                            (
                                converted,
                                XAccountRelation(
                                    source_external_id=source_id or username,
                                    source_status=source_status,
                                    target=target,
                                    relation_type=kind,
                                    evidence_external_id=converted.external_id,
                                    evidence_url=converted.url,
                                ),
                            )
                        )
                    succeeded += 1
                except Exception as exc:
                    warnings.append(f"native interactions for @{username} failed: {exc}")

        if not queries and not relation_sources:
            raise ValueError("X discovery requires query/queries or seed accounts")
        metadata = {
            "provider": str(getattr(client, "name", "unknown")),
            "attempted_calls": attempted,
            "succeeded_calls": succeeded,
        }
        merge_client_diagnostics(client, metadata, warnings)
        if attempted and not succeeded:
            return PlatformTaskResult.failed(
                "all X discovery provider calls failed",
                warnings=tuple(dict.fromkeys(warnings)),
                metadata=metadata,
            )
        return PlatformTaskResult.completed(
            items,
            warnings=tuple(dict.fromkeys(warnings)),
            metadata=metadata,
        )

    def scan_signals(self, context: PlatformTaskContext) -> PlatformTaskResult:
        client = self.client()
        _begin_read_run(client)
        limit = max(1, min(int(context.payload.get("limit", 20)), 100))
        account_cursors = _decode_scan_cursor(
            _optional_string(context.payload.get("cursor"))
        )
        account_ids = _strings(
            context.payload.get("account_external_ids")
            or context.payload.get("accounts")
            or ()
        )
        target_by_id = {
            str(item.get("id") or item.get("external_id") or ""): item
            for item in (context.payload.get("account_targets") or ())
            if isinstance(item, Mapping)
        }
        items: list[object] = []
        warnings: list[str] = []
        attempted = 0
        succeeded = 0
        for external_id in account_ids:
            attempted += 1
            try:
                target = target_by_id.get(external_id, {})
                username = str(
                    target.get("handle") or target.get("username") or external_id
                ).lstrip("@")
                tweets = [
                    _x_tweet(item)
                    for item in client.get_user_tweets(  # type: ignore[attr-defined]
                        external_id,
                        limit,
                        username=username,
                    )
                ]
                previous = account_cursors.get(external_id) or account_cursors.get("*")
                items.extend(
                    tweet
                    for tweet in tweets
                    if not previous or _is_after_cursor(tweet.created_at, previous)
                )
                latest = _latest_scan_cursor(
                    previous,
                    (tweet.created_at for tweet in tweets),
                )
                if latest:
                    account_cursors[external_id] = latest
                succeeded += 1
            except Exception as exc:
                warnings.append(f"timeline {external_id!r} failed: {exc}")
        if bool(context.payload.get("include_trends", True)):
            attempted += 1
            try:
                trends = [
                    _x_trend(item)
                    for item in client.get_trends(limit)  # type: ignore[attr-defined]
                ]
                items.extend(trends)
                succeeded += 1
                search_tweets = getattr(client, "search_tweets", None)
                if callable(search_tweets):
                    settings = context.settings or self.settings
                    trend_limit = max(
                        1,
                        min(
                            int(
                                context.payload.get("trends_per_scan")
                                or getattr(settings, "x_trends_per_scan", 5)
                            ),
                            20,
                        ),
                    )
                    tweets_per_trend = max(
                        1,
                        min(
                            int(
                                context.payload.get("tweets_per_trend")
                                or getattr(settings, "x_tweets_per_trend", 10)
                            ),
                            50,
                        ),
                    )
                    ordered_trends = sorted(
                        (trend for trend in trends if trend.name.strip()),
                        key=lambda trend: (trend.rank or 10_000, trend.name),
                    )[:trend_limit]
                    if ordered_trends:
                        attempted += 1
                        try:
                            query = " OR ".join(
                                trend.name for trend in ordered_trends
                            )
                            trend_tweets = [
                                _x_tweet(item)
                                for item in search_tweets(
                                    query,
                                    min(50, tweets_per_trend * len(ordered_trends)),
                                )
                            ]
                            for tweet in trend_tweets:
                                searchable_text = tweet.text.casefold().replace("#", "")
                                matches = [
                                    trend
                                    for trend in ordered_trends
                                    if trend.name.casefold().lstrip("#")
                                    in searchable_text
                                ]
                                if not matches and len(ordered_trends) == 1:
                                    matches = [ordered_trends[0]]
                                items.extend(
                                    XTrendTweet(
                                        trend_name=trend.name,
                                        trend_rank=trend.rank,
                                        tweet=tweet,
                                    )
                                    for trend in matches
                                )
                            succeeded += 1
                        except Exception as exc:
                            warnings.append(
                                f"trend tweet search failed: {exc}"
                            )
            except Exception as exc:
                warnings.append(f"native trends failed: {exc}")
        metadata = {
            "provider": str(getattr(client, "name", "unknown")),
            "attempted_calls": attempted,
            "succeeded_calls": succeeded,
        }
        merge_client_diagnostics(client, metadata, warnings)
        if attempted and not succeeded:
            return PlatformTaskResult.failed(
                "all X signal provider calls failed",
                warnings=tuple(dict.fromkeys(warnings)),
                metadata=metadata,
            )
        return PlatformTaskResult.completed(
            items,
            cursor=_encode_scan_cursor(account_cursors),
            warnings=tuple(dict.fromkeys(warnings)),
            metadata=metadata,
        )

    def refresh_outcomes(self, context: PlatformTaskContext) -> PlatformTaskResult:
        from kol_search.platform_modules.browser_actions import refresh_x_browser_outcomes

        settings = context.settings or self.settings
        if context.settings is None:
            context = replace(context, settings=settings)
        browser_result = refresh_x_browser_outcomes(context)
        updates: list[PlatformOutcomeUpdate] = [
            item
            for item in browser_result.items
            if isinstance(item, PlatformOutcomeUpdate)
        ]
        warnings = list(browser_result.warnings)
        checked = int(browser_result.metadata.get("actions_checked", 0))
        actions = tuple(
            item
            for item in (context.payload.get("actions") or ())
            if isinstance(item, Mapping)
            and str(item.get("status") or "") == "confirmation_required"
            and str(item.get("action_type") or item.get("kind") or "")
            in {"owned_post", "owned_publish"}
        )
        if not actions:
            return browser_result

        publisher, owns_publisher, error = _postiz_publisher(context, settings)
        if publisher is None:
            return PlatformTaskResult.completed(
                updates,
                warnings=(*warnings, error or "Postiz publisher is not configured"),
                metadata={
                    "actions_checked": checked,
                    "outcomes_updated": len(updates),
                },
            )

        try:
            for action in actions:
                action_id = _positive_int(action.get("id"))
                if action_id is None:
                    warnings.append("ignored an X outcome without a valid action id")
                    continue
                platform_payload = _mapping(action.get("payload"))
                connection = _mapping(action.get("connection"))
                connection_metadata = _mapping(connection.get("metadata"))
                integration_id = _optional_string(
                    platform_payload.get("integration_id")
                    or connection_metadata.get("integration_id")
                )
                external_id = _optional_string(action.get("external_id"))
                wanted_content = _optional_string(
                    action.get("final_text") or action.get("draft")
                )
                around = _outcome_around(action, platform_payload, context.started_at)
                checked += 1
                try:
                    rows = publisher.recent_posts(around=around, hours=24)  # type: ignore[attr-defined]
                except Exception as exc:
                    from kol_search.platform_modules.x_postiz import XPostizError

                    if not isinstance(exc, (XPostizError, ValueError)):
                        raise
                    warnings.append(f"X action {action_id} receipt lookup failed: {exc}")
                    continue

                matches = [
                    row
                    for row in rows
                    if isinstance(row, Mapping)
                    and _postiz_row_matches(
                        row,
                        external_id=external_id,
                        integration_id=integration_id,
                        content=wanted_content,
                        around=around,
                    )
                ]
                if not matches:
                    warnings.append(f"X action {action_id} has no matching Postiz receipt")
                    continue
                if len(matches) != 1:
                    warnings.append(
                        f"X action {action_id} has ambiguous Postiz receipts; "
                        "manual confirmation is required"
                    )
                    continue
                matched = matches[0]
                receipt_url = _postiz_release_url(matched)
                if not receipt_url:
                    warnings.append(
                        f"X action {action_id} is present in Postiz but has no release URL"
                    )
                    continue
                updates.append(
                    PlatformOutcomeUpdate(
                        action_id=action_id,
                        result=ActionExecutionResult(
                            success=True,
                            external_id=_postiz_external_id(matched) or external_id,
                            receipt_url=receipt_url,
                            raw_receipt=dict(matched),
                            confirmed=True,
                        ),
                    )
                )
        finally:
            if owns_publisher:
                publisher.close()  # type: ignore[attr-defined]

        return PlatformTaskResult.completed(
            updates,
            warnings=warnings,
            metadata={
                "actions_checked": checked,
                "outcomes_updated": len(updates),
            },
        )

    def execute_action(self, context: PlatformTaskContext) -> ActionExecutionResult:
        settings = context.settings or self.settings
        if context.settings is None:
            context = replace(context, settings=settings)
        action_type = str(
            context.payload.get("action_type") or context.payload.get("kind") or ""
        )
        try:
            if action_type in {"owned_post", "owned_publish"}:
                return _execute_owned_publish(context, settings)
            from kol_search.platform_modules.browser_actions import execute_x_browser_action

            return execute_x_browser_action(context)
        except Exception as exc:
            return ActionExecutionResult.failed(str(exc))


def _execute_owned_publish(
    context: PlatformTaskContext, settings: object
) -> ActionExecutionResult:
    if not bool(getattr(settings, "live_write_enabled", False)):
        return ActionExecutionResult.failed("live writes are disabled")

    payload = context.payload
    platform_payload = _mapping(payload.get("platform_payload"))
    connection = _mapping(payload.get("connection"))
    connection_metadata = _mapping(connection.get("metadata"))
    integration_id = _optional_string(
        platform_payload.get("integration_id")
        or connection_metadata.get("integration_id")
    )
    if not integration_id:
        return ActionExecutionResult.failed("X owned publish requires a Postiz integration id")

    explicit_mode = _optional_string(platform_payload.get("mode"))
    publish_at: datetime | None
    if explicit_mode is None:
        try:
            mode, publish_at = _publishing_delivery(
                settings,
                now=context.started_at,
            )
        except (TypeError, ValueError) as exc:
            return ActionExecutionResult.failed(str(exc))
    else:
        mode = explicit_mode.lower()
        if mode not in {"now", "schedule"}:
            return ActionExecutionResult.failed(
                "X owned publish mode must be 'now' or 'schedule'"
            )
        publish_at = None
        if mode == "schedule":
            scheduled_at = platform_payload.get("scheduled_at_utc")
            try:
                publish_at = _datetime_value(scheduled_at)
            except (TypeError, ValueError) as exc:
                return ActionExecutionResult.failed(f"invalid scheduled_at_utc: {exc}")
            if publish_at is not None and publish_at <= context.started_at:
                return ActionExecutionResult.failed(
                    "scheduled_at_utc is in the past; review and schedule the action again"
                )
        if mode == "schedule" and publish_at is None:
            return ActionExecutionResult.failed(
                "scheduled X owned publish requires scheduled_at_utc"
            )

    content = str(payload.get("draft") or "").strip()
    if not content:
        return ActionExecutionResult.failed("X owned publish content is empty")

    publisher, owns_publisher, error = _postiz_publisher(context, settings)
    if publisher is None:
        return ActionExecutionResult.failed(error or "Postiz publisher is not configured")
    try:
        try:
            receipt = publisher.create_x_post(  # type: ignore[attr-defined]
                integration_id=integration_id,
                content=content,
                mode=mode,
                publish_at=publish_at,
                settings={
                    "who_can_reply_post": str(
                        platform_payload.get("who_can_reply") or "everyone"
                    ),
                    "made_with_ai": bool(platform_payload.get("made_with_ai", False)),
                },
            )
        except Exception as exc:
            from kol_search.platform_modules.x_postiz import (
                XPostizError,
                XPostizRequestUncertain,
            )

            if isinstance(exc, XPostizRequestUncertain):
                return ActionExecutionResult(
                    success=True,
                    confirmed=False,
                    error=str(exc),
                )
            if isinstance(exc, (XPostizError, ValueError)):
                return ActionExecutionResult.failed(str(exc))
            raise

        row = _postiz_receipt_row(receipt)
        external_id = _postiz_external_id(row)
        receipt_url = _postiz_release_url(row)
        if not external_id:
            return ActionExecutionResult(
                success=True,
                receipt_url=receipt_url,
                raw_receipt=receipt,
                confirmed=False,
                error="Postiz create response is missing a post id; do not retry automatically",
            )
        confirmed = mode == "schedule" or bool(receipt_url)
        return ActionExecutionResult(
            success=True,
            external_id=external_id,
            receipt_url=receipt_url,
            raw_receipt=receipt,
            confirmed=confirmed,
            error=(
                None
                if confirmed
                else "Postiz accepted the immediate publish but no release URL was returned"
            ),
        )
    finally:
        if owns_publisher:
            publisher.close()  # type: ignore[attr-defined]


def _strings(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value.strip(),) if value.strip() else ()
    if isinstance(value, Iterable):
        return tuple(str(item).strip() for item in value if str(item).strip())
    return ()


def _begin_read_run(client: object) -> None:
    begin_run = getattr(client, "begin_run", None)
    if callable(begin_run):
        begin_run()


def _mapping(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _positive_int(value: object) -> int | None:
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _datetime_value(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        if not text:
            return None
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _publishing_delivery(
    settings: object,
    *,
    now: datetime,
) -> tuple[str, datetime | None]:
    """Choose the X-owned delivery time from X publishing configuration."""

    timezone_name = str(getattr(settings, "timezone", "UTC") or "UTC")
    try:
        zone = ZoneInfo(timezone_name)
    except Exception as exc:
        raise ValueError(f"invalid X publishing timezone: {timezone_name}") from exc
    local_now = now.astimezone(zone)
    configured = getattr(settings, "publishing_windows", None)
    if callable(configured):
        raw_windows = configured()
    else:
        raw_value = str(getattr(settings, "publish_windows", "09:00-11:00,17:00-20:00"))
        raw_windows = tuple(value.strip() for value in raw_value.split(",") if value.strip())

    windows: list[tuple[int, int]] = []
    for value in raw_windows:
        try:
            start_text, end_text = str(value).split("-", 1)
            start_hour, start_minute = (int(part) for part in start_text.split(":"))
            end_hour, end_minute = (int(part) for part in end_text.split(":"))
            start_total = start_hour * 60 + start_minute
            end_total = end_hour * 60 + end_minute
        except (TypeError, ValueError):
            continue
        if 0 <= start_total < end_total <= 24 * 60:
            windows.append((start_total, end_total))
    if not windows:
        raise ValueError("X publishing windows do not contain a valid time window")

    current_total = local_now.hour * 60 + local_now.minute
    if any(start <= current_total < end for start, end in windows):
        return "now", None
    candidates: list[datetime] = []
    for day_offset in (0, 1):
        target_day = local_now.date() + timedelta(days=day_offset)
        for start, _end in windows:
            candidate = datetime(
                target_day.year,
                target_day.month,
                target_day.day,
                start // 60,
                start % 60,
                tzinfo=zone,
            )
            if candidate > local_now:
                candidates.append(candidate)
    return "schedule", min(candidates).astimezone(timezone.utc)


def _postiz_publisher(
    context: PlatformTaskContext, settings: object
) -> tuple[object | None, bool, str | None]:
    injected = context.services.get("postiz_publisher")
    if injected is not None:
        return injected, False, None
    api_key = _optional_string(getattr(settings, "postiz_api_key", None))
    if not api_key:
        return None, False, "POSTIZ_API_KEY is not configured"
    from kol_search.platform_modules.x_postiz import XPostizPublisher

    return (
        XPostizPublisher(
            api_key=api_key,
            base_url=str(
                getattr(
                    settings,
                    "postiz_api_url",
                    "https://api.postiz.com/public/v1",
                )
            ),
        ),
        True,
        None,
    )


def _postiz_receipt_row(receipt: object) -> dict[str, Any]:
    if isinstance(receipt, Mapping):
        for key in ("posts", "data"):
            nested = receipt.get(key)
            if isinstance(nested, Mapping):
                return dict(nested)
            if isinstance(nested, (list, tuple)) and nested and isinstance(
                nested[0], Mapping
            ):
                return dict(nested[0])
        return dict(receipt)
    if isinstance(receipt, (list, tuple)) and receipt and isinstance(
        receipt[0], Mapping
    ):
        return dict(receipt[0])
    return {}


def _postiz_external_id(row: Mapping[str, Any]) -> str | None:
    return _optional_string(
        row.get("postId")
        or row.get("post_id")
        or row.get("id")
        or row.get("externalId")
    )


def _postiz_release_url(row: Mapping[str, Any]) -> str | None:
    return _optional_string(
        row.get("releaseURL") or row.get("releaseUrl") or row.get("release_url")
    )


def _postiz_row_integration_id(row: Mapping[str, Any]) -> str | None:
    integration = row.get("integration")
    if isinstance(integration, Mapping):
        return _optional_string(integration.get("id"))
    return _optional_string(
        row.get("integrationId") or row.get("integration_id") or integration
    )


def _postiz_row_content(row: Mapping[str, Any]) -> str | None:
    direct = _optional_string(row.get("content"))
    if direct:
        return direct
    values = row.get("value")
    if isinstance(values, (list, tuple)) and values and isinstance(values[0], Mapping):
        return _optional_string(values[0].get("content"))
    posts = row.get("posts")
    if isinstance(posts, (list, tuple)) and posts and isinstance(posts[0], Mapping):
        post_values = posts[0].get("value")
        if (
            isinstance(post_values, (list, tuple))
            and post_values
            and isinstance(post_values[0], Mapping)
        ):
            return _optional_string(post_values[0].get("content"))
    return None


def _postiz_row_matches(
    row: Mapping[str, Any],
    *,
    external_id: str | None,
    integration_id: str | None,
    content: str | None,
    around: datetime,
) -> bool:
    if external_id and _postiz_external_id(row) == external_id:
        return True
    if not (
        integration_id
        and content
        and _postiz_row_integration_id(row) == integration_id
        and _postiz_row_content(row) == content
    ):
        return False
    observed = _postiz_row_timestamp(row)
    if observed is None:
        # Content is not a receipt identifier. Without either an external ID or
        # a provider timestamp close to this action, identical historical posts
        # cannot be distinguished safely.
        return False
    return abs((observed - around).total_seconds()) <= 15 * 60


def _postiz_row_timestamp(row: Mapping[str, Any]) -> datetime | None:
    candidates: list[object] = [
        row.get("createdAt"),
        row.get("created_at"),
        row.get("date"),
        row.get("publishDate"),
        row.get("scheduledAt"),
    ]
    posts = row.get("posts")
    if isinstance(posts, (list, tuple)) and posts and isinstance(posts[0], Mapping):
        candidates.extend(
            (
                posts[0].get("createdAt"),
                posts[0].get("date"),
                posts[0].get("scheduledAt"),
            )
        )
    for value in candidates:
        try:
            parsed = _datetime_value(value)
        except (TypeError, ValueError):
            continue
        if parsed is not None:
            return parsed
    return None


def _outcome_around(
    action: Mapping[str, Any],
    platform_payload: Mapping[str, Any],
    fallback: datetime,
) -> datetime:
    for value in (
        platform_payload.get("scheduled_at_utc"),
        action.get("started_at"),
        action.get("created_at"),
    ):
        try:
            parsed = _datetime_value(value)
        except (TypeError, ValueError):
            continue
        if parsed is not None:
            return parsed
    return fallback.astimezone(timezone.utc)


def _queries(payload: Mapping[str, Any]) -> tuple[str, ...]:
    return _strings(payload.get("queries") or payload.get("query") or ())


def _optional_string(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _decode_scan_cursor(value: str | None) -> dict[str, str]:
    if not value:
        return {}
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return {"*": value}
    if not isinstance(payload, Mapping):
        return {"*": value}
    accounts = payload.get("accounts")
    if not isinstance(accounts, Mapping):
        return {}
    return {
        str(account_id): str(timestamp)
        for account_id, timestamp in accounts.items()
        if str(account_id).strip() and str(timestamp).strip()
    }


def _encode_scan_cursor(values: Mapping[str, str]) -> str | None:
    accounts = {key: value for key, value in values.items() if key != "*"}
    if not accounts:
        return values.get("*")
    return json.dumps(
        {"version": 1, "accounts": accounts},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _is_after_cursor(value: str | None, cursor: str) -> bool:
    if not value:
        return True
    try:
        observed = _datetime_value(value)
        previous = _datetime_value(cursor)
    except (TypeError, ValueError):
        return value > cursor
    if observed is None or previous is None:
        return value > cursor
    return observed > previous


def _latest_scan_cursor(
    previous: str | None,
    observed_values: Iterable[str | None],
) -> str | None:
    """Advance a content cursor monotonically despite unordered provider rows."""

    latest = previous
    for value in observed_values:
        if not value:
            continue
        if latest is None or _is_after_cursor(value, latest):
            latest = value
    return latest


def x_manifest(settings: object | None = None) -> PlatformManifest:
    if settings is None:
        return X_MANIFEST
    interval_minutes = int(
        getattr(settings, "x_signal_interval_minutes", None)
        or getattr(settings, "signal_interval_minutes", 30)
    )
    limits = {
        "comment_hourly": int(getattr(settings, "comment_hourly_limit", 3)),
        "comment_daily": int(getattr(settings, "comment_daily_limit", 10)),
        "dm_hourly": int(getattr(settings, "dm_hourly_limit", 2)),
        "dm_daily": int(getattr(settings, "dm_daily_limit", 5)),
        "author_cooldown_days": int(getattr(settings, "author_cooldown_days", 7)),
        "owned_publish_daily": int(getattr(settings, "publish_daily_limit", 2)),
    }
    return replace(
        X_MANIFEST,
        default_scan_interval_seconds=max(60, interval_minutes * 60),
        safety_limits=limits,
    )


def _x_router_factory(manifest: PlatformManifest) -> object:
    from kol_search.platform_modules.web_router import (
        create_platform_workspace_router,
    )

    return create_platform_workspace_router(manifest)


def create_x_plugin(
    settings: object,
    *,
    client_factory: Callable[[], object] | None = None,
) -> PlatformPlugin:
    """Create an X plugin with a lazy, X-owned Twitter provider client."""

    from kol_search.platform_modules.adapter_utils import build_opportunity_task
    from kol_search.platform_modules.x_adapter import XAutomationAdapter
    from kol_search.platform_modules.x_native import install_x_schema

    manifest = x_manifest(settings)
    runtime = _XRuntime(settings, client_factory)
    adapter = XAutomationAdapter(settings)
    return PlatformPlugin(
        manifest=manifest,
        handlers={
            PlatformTaskName.DISCOVER: runtime.discover,
            PlatformTaskName.SCAN_SIGNALS: runtime.scan_signals,
            PlatformTaskName.BUILD_OPPORTUNITIES: (
                lambda context: build_opportunity_task(adapter, context)
            ),
            PlatformTaskName.EXECUTE_ACTION: runtime.execute_action,
            PlatformTaskName.REFRESH_OUTCOMES: runtime.refresh_outcomes,
        },
        native_models={
            "account": XAccount,
            "tweet": XTweet,
            "trend": XTrend,
            "trend_tweet": XTrendTweet,
            "relation": XAccountRelation,
        },
        automation_adapter=adapter,
        schema_installer=install_x_schema,
        router_factory=lambda: _x_router_factory(manifest),
        health_check=runtime.health,
        close=runtime.close,
    )


__all__ = [
    "XAccount",
    "XAccountRelation",
    "XTrend",
    "XTrendTweet",
    "XTweet",
    "XTweetMetrics",
    "X_MANIFEST",
    "create_x_plugin",
    "x_manifest",
]
