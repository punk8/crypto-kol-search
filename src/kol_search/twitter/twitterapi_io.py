from __future__ import annotations

from math import ceil
import time
from typing import Any
from urllib.parse import urljoin

import httpx

from kol_search.models import Account, BackendCapabilities, Post
from kol_search.twitter.base import TwitterBackendError
from kol_search.twitter.third_party import _parse_tweet, _parse_user


class TwitterApiIoClient:
    """Read-only adapter for the documented TwitterAPI.io REST endpoints."""

    name = "twitterapi_io"
    capabilities = BackendCapabilities(
        user_search=True,
        post_search=True,
        batch_user_lookup=True,
        user_timeline=True,
        followings=True,
        verified_followers=True,
        # The free tier is limited to one request every five seconds. Expose a
        # single documented page to the pipeline; callers may still request
        # more explicitly and this client will paginate safely.
        user_search_page_size=20,
        post_search_page_size=20,
        batch_user_lookup_size=20,
        followings_page_size=200,
        verified_followers_page_size=20,
    )

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.twitterapi.io",
        timeout: float = 30.0,
    ) -> None:
        if not api_key:
            raise TwitterBackendError(
                "TwitterAPI.io backend requires TWITTERAPI_IO_API_KEY.",
                hint="API_KEY is also accepted for the initial local setup.",
            )
        self._base = base_url.rstrip("/") + "/"
        self._last_request_started = 0.0
        self._min_interval_seconds = 0.0
        self._client = httpx.Client(
            headers={
                "X-API-Key": api_key,
                "Accept": "application/json",
                "User-Agent": "kol-search/0.1 (internal research)",
            },
            timeout=timeout,
        )

    def close(self) -> None:
        self._client.close()

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        url = urljoin(self._base, path.lstrip("/"))
        response: httpx.Response | None = None
        for attempt in range(3):
            interval_wait = (
                self._last_request_started + self._min_interval_seconds - time.monotonic()
            )
            if interval_wait > 0:
                time.sleep(interval_wait)
            self._last_request_started = time.monotonic()
            try:
                response = self._client.get(url, params=params)
            except httpx.HTTPError as exc:
                raise TwitterBackendError(f"TwitterAPI.io network error: {exc}") from exc
            if response.status_code != 429 or attempt == 2:
                break

            retry_after = response.headers.get("Retry-After")
            try:
                wait_seconds = max(0.25, float(retry_after)) if retry_after else 5.1
            except ValueError:
                wait_seconds = 5.1
            if "one request every 5 seconds" in response.text.lower():
                wait_seconds = max(wait_seconds, 5.1)
                self._min_interval_seconds = max(self._min_interval_seconds, 5.1)
            time.sleep(wait_seconds)

        if response is None:
            raise TwitterBackendError("TwitterAPI.io request did not produce a response.")
        if response.status_code >= 400:
            raise TwitterBackendError(
                f"TwitterAPI.io error: {response.text[:400]}",
                status_code=response.status_code,
                hint="Check API key, balance, endpoint availability, and request parameters.",
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise TwitterBackendError("TwitterAPI.io returned invalid JSON.") from exc
        if not isinstance(payload, dict):
            raise TwitterBackendError("TwitterAPI.io returned an unexpected response shape.")
        if payload.get("status") == "error" or payload.get("error"):
            message = payload.get("message") or payload.get("msg") or payload.get("error")
            raise TwitterBackendError(f"TwitterAPI.io error: {message}")
        return payload

    @staticmethod
    def _dedupe(items: list[Account]) -> list[Account]:
        output: list[Account] = []
        seen: set[str] = set()
        for item in items:
            key = item.id or item.username.lower()
            if key in seen:
                continue
            seen.add(key)
            output.append(item)
        return output

    @staticmethod
    def _profile_values(payload: dict[str, Any], *keys: str) -> list[dict[str, Any]]:
        for container in (payload, payload.get("data")):
            if not isinstance(container, dict):
                continue
            for key in keys:
                values = container.get(key)
                if isinstance(values, list):
                    return [item for item in values if isinstance(item, dict)]
        return []

    def search_users(self, query: str, max_results: int = 100) -> list[Account]:
        target = max(1, min(max_results, 500))
        cursor = ""
        output: list[Account] = []
        seen_cursors: set[str] = set()

        for _ in range(max(1, ceil(target / 20) + 1)):
            payload = self._get(
                "twitter/user/search",
                params={"query": query, "cursor": cursor},
            )
            users = payload.get("users") or []
            if not isinstance(users, list):
                break
            output.extend(_parse_user(item) for item in users if isinstance(item, dict))
            output = self._dedupe(output)
            if len(output) >= target or not payload.get("has_next_page"):
                break
            next_cursor = str(payload.get("next_cursor") or "")
            if not next_cursor or next_cursor in seen_cursors:
                break
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        return output[:target]

    def search_tweets(
        self,
        query: str,
        max_results: int = 40,
        since_id: str | None = None,
    ) -> list[Post]:
        # The provider currently recommends time-windowed queries instead of
        # cursor pagination for advanced search, so fetch one page intentionally.
        payload = self._get(
            "twitter/tweet/advanced_search",
            params={"query": query, "queryType": "Latest"},
        )
        tweets = payload.get("tweets") or []
        if not isinstance(tweets, list):
            return []
        return [_parse_tweet(item) for item in tweets if isinstance(item, dict)][:max_results]

    def get_user_by_username(self, username: str) -> Account | None:
        payload = self._get(
            "twitter/user/info",
            params={"userName": username.lstrip("@")},
        )
        data = payload.get("data")
        return _parse_user(data) if isinstance(data, dict) else None

    def get_users_by_usernames(self, usernames: list[str]) -> list[Account]:
        output: list[Account] = []
        for username in dict.fromkeys(u.lstrip("@") for u in usernames if u):
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
    ) -> list[Post]:
        target = max(1, min(max_results, 100))
        cursor = ""
        output: list[Post] = []
        seen_ids: set[str] = set()
        seen_cursors: set[str] = set()

        for _ in range(max(1, ceil(target / 20))):
            payload = self._get(
                "twitter/user/last_tweets",
                params={
                    "userId": user_id,
                    "includeReplies": "true" if include_replies else "false",
                    "cursor": cursor,
                },
            )
            data = payload.get("data")
            nested_tweets = data.get("tweets") if isinstance(data, dict) else None
            tweets = payload.get("tweets") or nested_tweets or []
            if not isinstance(tweets, list):
                break
            for item in tweets:
                if not isinstance(item, dict):
                    continue
                post = _parse_tweet(item)
                if not post.author_id:
                    post.author_id = str(user_id)
                if post.id not in seen_ids:
                    seen_ids.add(post.id)
                    output.append(post)
            if len(output) >= target or not payload.get("has_next_page"):
                break
            next_cursor = str(payload.get("next_cursor") or "")
            if not next_cursor or next_cursor in seen_cursors:
                break
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        return output[:target]

    def get_followings(self, username: str, max_results: int = 20) -> list[Account]:
        target = max(1, min(max_results, 1000))
        cursor = ""
        output: list[Account] = []
        seen_cursors: set[str] = set()
        for _ in range(max(1, ceil(target / 200))):
            payload = self._get(
                "twitter/user/followings",
                params={
                    "userName": username.lstrip("@"),
                    "cursor": cursor,
                    "pageSize": min(200, target - len(output)),
                },
            )
            values = self._profile_values(payload, "followings", "users")
            output.extend(_parse_user(item) for item in values)
            output = self._dedupe(output)
            if len(output) >= target or not payload.get("has_next_page"):
                break
            next_cursor = str(payload.get("next_cursor") or "")
            if not next_cursor or next_cursor in seen_cursors:
                break
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        return output[:target]

    def get_verified_followers(
        self, user_id: str, max_results: int = 20, *, username: str | None = None
    ) -> list[Account]:
        target = max(1, min(max_results, 200))
        cursor = ""
        output: list[Account] = []
        seen_cursors: set[str] = set()
        for _ in range(max(1, ceil(target / 20))):
            payload = self._get(
                "twitter/user/verifiedFollowers",
                params={"user_id": str(user_id), "cursor": cursor},
            )
            values = self._profile_values(payload, "followers", "verifiedFollowers", "users")
            output.extend(_parse_user(item) for item in values)
            output = self._dedupe(output)
            if len(output) >= target or not payload.get("has_next_page"):
                break
            next_cursor = str(payload.get("next_cursor") or "")
            if not next_cursor or next_cursor in seen_cursors:
                break
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        return output[:target]
