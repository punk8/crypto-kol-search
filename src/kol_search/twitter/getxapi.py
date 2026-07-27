from __future__ import annotations

from math import ceil
import time
from typing import Any, Callable, Protocol, TypeVar
from urllib.parse import quote_plus, urljoin

import httpx

from kol_search.twitter.base import TwitterBackendError
from kol_search.twitter.models import XAccount, XReadCapabilities, XTrend, XTweet
from kol_search.twitter.third_party import _parse_tweet, _parse_user


ItemT = TypeVar("ItemT", XAccount, XTweet)


class ProviderState(Protocol):
    def reserve_calls(
        self, provider: str, *, units: int, daily_limit: int
    ) -> bool: ...

    def usage_today(self, provider: str) -> int: ...

    def debit_estimated_credits(self, provider: str, *, amount: float) -> None: ...

    def record_account_status(
        self,
        provider: str,
        *,
        credits_remaining: float | None,
        credits_used: float | None,
        upstream_total_requests: int | None,
    ) -> None: ...

    def account_status(self, provider: str) -> dict[str, Any] | None: ...

    def account_status_is_fresh(
        self, provider: str, *, max_age_seconds: int
    ) -> bool: ...


class GetXApiClient:
    """Read-only adapter for GetXAPI's documented REST endpoints."""

    name = "getxapi"
    capabilities = XReadCapabilities(
        user_search=True,
        post_search=True,
        batch_user_lookup=False,
        user_timeline=True,
        post_lookup=True,
        followings=True,
        verified_followers=True,
        trends=True,
        user_search_page_size=20,
        post_search_page_size=20,
        batch_user_lookup_size=1,
        followings_page_size=200,
        verified_followers_page_size=20,
    )

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.getxapi.com",
        timeout: float = 30.0,
        trend_woeid: int = 1,
        state_store: ProviderState | None = None,
        daily_call_limit: int = 500,
        min_credits: float = 0.01,
        balance_cache_seconds: int = 300,
        estimated_credits_per_call: float = 0.001,
    ) -> None:
        if not api_key:
            raise TwitterBackendError(
                "GetXAPI backend requires GET_X_API_KEY.",
                hint="Create a read-only API key and configure it on the backend service.",
            )
        self._base = base_url.rstrip("/") + "/"
        self._trend_woeid = max(1, int(trend_woeid))
        self._state_store = state_store
        self._daily_call_limit = max(1, int(daily_call_limit))
        self._min_credits = max(0.0, float(min_credits))
        self._balance_cache_seconds = max(0, int(balance_cache_seconds))
        self._estimated_credits_per_call = max(
            0.0, float(estimated_credits_per_call)
        )
        # GetXAPI's account endpoint currently reports wallet credits but omits
        # active Starter / subscription plan credits. Only a billable HTTP 402
        # is therefore authoritative evidence that all usable credit is gone.
        self._upstream_balance_exhausted = False
        self.warnings: list[str] = []
        self._client = httpx.Client(
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
                "User-Agent": "signalscope-discover/0.1 (internal research)",
            },
            timeout=timeout,
        )

    def close(self) -> None:
        self._client.close()

    @staticmethod
    def _optional_float(value: Any) -> float | None:
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _optional_int(value: Any) -> int | None:
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def _request_json(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        billable: bool,
    ) -> dict[str, Any]:
        if billable:
            self._prepare_billable_call()
        url = urljoin(self._base, path.lstrip("/"))
        response: httpx.Response | None = None
        for attempt in range(3):
            try:
                response = self._client.get(url, params=params)
            except httpx.HTTPError as exc:
                raise TwitterBackendError("GetXAPI network request failed.") from exc
            if response.status_code != 429 or attempt == 2:
                break
            retry_after = response.headers.get("Retry-After")
            try:
                wait_seconds = max(0.25, float(retry_after or "1"))
            except ValueError:
                wait_seconds = 1.0
            time.sleep(wait_seconds)

        if response is None:
            raise TwitterBackendError("GetXAPI request did not produce a response.")
        if billable and response.status_code == 402:
            self._upstream_balance_exhausted = True
            raise TwitterBackendError(
                "GetXAPI credit balance exhausted.",
                status_code=402,
                hint="Top up the provider account or wait for plan credits to renew.",
            )
        if response.status_code >= 400:
            raise TwitterBackendError(
                "GetXAPI request failed.",
                status_code=response.status_code,
                hint="Check GET_X_API_KEY, credit balance, and endpoint availability.",
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise TwitterBackendError("GetXAPI returned invalid JSON.") from exc
        if not isinstance(payload, dict):
            raise TwitterBackendError("GetXAPI returned an unexpected response shape.")
        if payload.get("status") == "error" or payload.get("error"):
            raise TwitterBackendError("GetXAPI returned an application error.")
        if billable:
            self._upstream_balance_exhausted = False
        return payload

    def _refresh_account_status(self, *, force: bool = False) -> dict[str, Any] | None:
        if self._state_store is None:
            return None
        if not force and self._state_store.account_status_is_fresh(
            self.name, max_age_seconds=self._balance_cache_seconds
        ):
            return self._state_store.account_status(self.name)
        payload = self._request_json("account/me", billable=False)
        self._state_store.record_account_status(
            self.name,
            credits_remaining=self._optional_float(payload.get("credits_remaining")),
            credits_used=self._optional_float(payload.get("credits_used")),
            upstream_total_requests=self._optional_int(payload.get("total_requests")),
        )
        return self._state_store.account_status(self.name)

    def _prepare_billable_call(self) -> None:
        if self._state_store is None:
            return
        try:
            self._refresh_account_status()
        except TwitterBackendError:
            warning = "GetXAPI balance check was unavailable"
            if warning not in self.warnings:
                self.warnings.append(warning)
        if not self._state_store.reserve_calls(
            self.name, units=1, daily_limit=self._daily_call_limit
        ):
            raise TwitterBackendError(
                "GetXAPI daily call budget exhausted.",
                status_code=429,
                hint="Wait for the UTC budget reset or raise GET_X_API_DAILY_CALL_LIMIT.",
            )
        self._state_store.debit_estimated_credits(
            self.name, amount=self._estimated_credits_per_call
        )

    def usage_status(self, *, refresh: bool = False) -> dict[str, Any]:
        if self._state_store is None:
            return {
                "provider": self.name,
                "state": "unavailable",
                "requests_today": 0,
                "daily_limit": self._daily_call_limit,
            }
        error = False
        try:
            status = self._refresh_account_status(force=refresh)
        except TwitterBackendError:
            status = self._state_store.account_status(self.name)
            error = True
        used = self._state_store.usage_today(self.name)
        credits = self._optional_float(
            status.get("credits_remaining") if status else None
        )
        credits = max(0.0, credits) if credits is not None else None
        credits_used = self._optional_float(
            status.get("credits_used") if status else None
        )
        credits_used = max(0.0, credits_used) if credits_used is not None else None
        upstream_requests = self._optional_int(
            status.get("upstream_total_requests") if status else None
        )
        upstream_requests = (
            max(0, upstream_requests) if upstream_requests is not None else None
        )
        if used >= self._daily_call_limit:
            state = "budget_exhausted"
        elif self._upstream_balance_exhausted:
            state = "low_balance"
        elif error and status is None:
            state = "unavailable"
        else:
            state = "healthy"
        # A zero from /account/me is only the wallet balance. Returning it as
        # total remaining credit causes active plan credits to be misreported.
        reported_credits = (
            None
            if credits == 0 and not self._upstream_balance_exhausted
            else credits
        )
        return {
            "provider": self.name,
            "state": state,
            "requests_today": used,
            "daily_limit": self._daily_call_limit,
            "credits_remaining": reported_credits,
            "credits_used": credits_used,
            "upstream_total_requests": upstream_requests,
            "observed_at": status.get("observed_at") if status else None,
        }

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._request_json(path, params=params, billable=True)

    def _account(self, value: dict[str, Any]) -> XAccount:
        return _parse_user(value).model_copy(update={"source_provider": self.name})

    def _tweet(self, value: dict[str, Any]) -> XTweet:
        return _parse_tweet(value).model_copy(update={"source_provider": self.name})

    @staticmethod
    def _dedupe(items: list[ItemT]) -> list[ItemT]:
        output: list[ItemT] = []
        seen: set[str] = set()
        for item in items:
            key = item.id
            if not key or key in seen:
                continue
            seen.add(key)
            output.append(item)
        return output

    def _paged(
        self,
        path: str,
        *,
        params: dict[str, Any],
        result_key: str,
        target: int,
        page_size: int,
        parser: Callable[[dict[str, Any]], ItemT],
    ) -> list[ItemT]:
        cursor = ""
        output: list[ItemT] = []
        seen_cursors: set[str] = set()
        for _ in range(max(1, ceil(target / page_size))):
            request_params = dict(params)
            if cursor:
                request_params["cursor"] = cursor
            payload = self._get(path, params=request_params)
            values = payload.get(result_key) or []
            if not isinstance(values, list):
                break
            output.extend(parser(item) for item in values if isinstance(item, dict))
            output = self._dedupe(output)
            if len(output) >= target or not payload.get("has_more"):
                break
            next_cursor = str(payload.get("next_cursor") or "")
            if not next_cursor or next_cursor in seen_cursors:
                break
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        return output[:target]

    def search_users(self, query: str, max_results: int = 100) -> list[XAccount]:
        target = max(1, min(max_results, 500))
        return self._paged(
            "twitter/user/search",
            params={"q": query},
            result_key="users",
            target=target,
            page_size=20,
            parser=self._account,
        )

    def search_tweets(
        self,
        query: str,
        max_results: int = 40,
        since_id: str | None = None,
        start_time: str | None = None,
    ) -> list[XTweet]:
        effective_query = query
        if since_id:
            effective_query = f"{effective_query} since_id:{since_id}"
        if start_time:
            effective_query = f"{effective_query} since:{start_time[:10]}"
        target = max(1, min(max_results, 500))
        return self._paged(
            "twitter/tweet/advanced_search",
            params={"q": effective_query, "product": "Latest"},
            result_key="tweets",
            target=target,
            page_size=20,
            parser=self._tweet,
        )

    def get_user_by_username(self, username: str) -> XAccount | None:
        payload = self._get(
            "twitter/user/info", params={"userName": username.lstrip("@")}
        )
        value = payload.get("data")
        return self._account(value) if isinstance(value, dict) else None

    def _get_user_by_id(self, user_id: str) -> XAccount | None:
        payload = self._get("twitter/user/info_by_id", params={"userId": user_id})
        value = payload.get("data")
        return self._account(value) if isinstance(value, dict) else None

    def get_users_by_usernames(self, usernames: list[str]) -> list[XAccount]:
        output: list[XAccount] = []
        for username in dict.fromkeys(value.lstrip("@") for value in usernames if value):
            account = self.get_user_by_username(username)
            if account is not None:
                output.append(account)
        return self._dedupe(output)

    def get_user_tweets(
        self,
        user_id: str,
        max_results: int = 10,
        *,
        username: str | None = None,
        include_replies: bool = False,
        start_time: str | None = None,
    ) -> list[XTweet]:
        target = max(1, min(max_results, 500))
        path = (
            "twitter/user/tweets_and_replies"
            if include_replies and username
            else "twitter/user/tweets"
        )
        params = (
            {"userName": username.lstrip("@")}
            if username
            else {"userId": str(user_id)}
        )
        values = self._paged(
            path,
            params=params,
            result_key="tweets",
            target=target,
            page_size=20,
            parser=self._tweet,
        )
        if start_time:
            return [item for item in values if not item.created_at or item.created_at >= start_time]
        return values

    def get_tweet(self, tweet_id: str) -> XTweet | None:
        payload = self._get("twitter/tweet/detail", params={"id": str(tweet_id)})
        value = payload.get("data")
        return self._tweet(value) if isinstance(value, dict) else None

    def get_trends(
        self,
        max_results: int = 20,
        *,
        category: str | None = None,
        locale: str | None = None,
    ) -> list[XTrend]:
        payload = self._get(
            "twitter/trends",
            params={"woeid": str(self._trend_woeid), "count": max(1, min(50, max_results))},
        )
        values = payload.get("trends") or []
        output: list[XTrend] = []
        for fallback_rank, item in enumerate(values, 1):
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            try:
                volume = max(0, int(item.get("tweet_volume") or 0))
            except (TypeError, ValueError):
                volume = 0
            output.append(
                XTrend(
                    name=name,
                    rank=max(1, int(item.get("rank") or fallback_rank)),
                    post_count=volume,
                    url=item.get("search_url")
                    or f"https://x.com/search?q={quote_plus(str(item.get('query') or name))}",
                    raw={"source_provider": self.name, "context": category, **item},
                )
            )
        return output[:max_results]

    def get_followings(self, username: str, max_results: int = 20) -> list[XAccount]:
        target = max(1, min(max_results, 1000))
        return self._paged(
            "twitter/user/following",
            params={"userName": username.lstrip("@")},
            result_key="following",
            target=target,
            page_size=200,
            parser=self._account,
        )

    def get_verified_followers(
        self, user_id: str, max_results: int = 20, *, username: str | None = None
    ) -> list[XAccount]:
        handle = username.lstrip("@") if username else ""
        if not handle:
            account = self._get_user_by_id(str(user_id))
            handle = account.handle if account else ""
        if not handle:
            return []
        target = max(1, min(max_results, 500))
        return self._paged(
            "twitter/user/verified_followers",
            params={"userName": handle},
            result_key="verified_followers",
            target=target,
            page_size=20,
            parser=self._account,
        )
