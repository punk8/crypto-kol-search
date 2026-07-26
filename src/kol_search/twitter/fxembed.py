from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx

from kol_search.twitter.base import TwitterBackendError
from kol_search.twitter.models import XAccount, XReadCapabilities, XTrend, XTweet
from kol_search.twitter.third_party import _parse_tweet, _parse_user


class FxEmbedTwitterClient:
    """Read-only adapter for the public FxEmbed/FxTwitter HTTP API."""

    name = "fxembed"
    capabilities = XReadCapabilities(
        user_search=False,
        post_search=True,
        post_lookup=True,
        batch_user_lookup=False,
        user_timeline=True,
        followings=False,
        verified_followers=False,
        trends=True,
        user_search_page_size=0,
        post_search_page_size=100,
        batch_user_lookup_size=1,
    )

    def __init__(
        self,
        base_url: str = "https://api.fxtwitter.com",
        timeout: float = 20.0,
    ) -> None:
        if not base_url:
            raise TwitterBackendError("FXEMBED_BASE_URL is empty.")
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={
                "Accept": "application/json",
                "User-Agent": "kol-search/0.1 (internal read-only connector)",
            },
            timeout=timeout,
            follow_redirects=True,
        )

    def close(self) -> None:
        self._client.close()

    def _get(
        self, path: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        try:
            response = self._client.get(path, params=params)
        except httpx.HTTPError as exc:
            raise TwitterBackendError(f"FxEmbed network error: {exc}") from exc
        if response.status_code >= 400:
            raise TwitterBackendError(
                f"FxEmbed error: {response.text[:300]}",
                status_code=response.status_code,
                hint="The public API is best-effort; retry later or use the next provider.",
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise TwitterBackendError("FxEmbed returned invalid JSON.") from exc
        if not isinstance(payload, dict):
            raise TwitterBackendError("FxEmbed returned an unexpected response shape.")
        code = int(payload.get("code") or response.status_code)
        if code >= 400:
            raise TwitterBackendError(
                f"FxEmbed upstream error: {payload.get('message') or payload.get('error') or code}",
                status_code=code,
            )
        return payload

    @staticmethod
    def _results(payload: dict[str, Any]) -> list[dict[str, Any]]:
        for key in ("results", "statuses", "tweets"):
            values = payload.get(key)
            if isinstance(values, list):
                return [
                    item
                    for item in values
                    if isinstance(item, dict)
                    and item.get("type", "status") == "status"
                    and item.get("id")
                ]
        return []

    def search_tweets(
        self,
        query: str,
        max_results: int = 40,
        since_id: str | None = None,
        start_time: str | None = None,
    ) -> list[XTweet]:
        effective_query = query
        if start_time:
            effective_query = f"{effective_query} since:{start_time[:10]}"
        payload = self._get(
            "/2/search",
            params={
                "q": effective_query,
                "feed": "latest",
                "count": max(1, min(max_results, 100)),
            },
        )
        posts = [_parse_tweet(item) for item in self._results(payload)]
        for post in posts:
            post.source_provider = self.name
        return posts[:max_results]

    def search_users(self, query: str, max_results: int = 100) -> list[XAccount]:
        return []

    def get_user_by_username(self, username: str) -> XAccount | None:
        handle = username.lstrip("@")
        payload = self._get(f"/2/profile/{quote(handle, safe='')}")
        for key in ("profile", "user", "author"):
            value = payload.get(key)
            if isinstance(value, dict):
                account = _parse_user(value)
                account.source_provider = self.name
                return account
        return None

    def get_users_by_usernames(self, usernames: list[str]) -> list[XAccount]:
        output: list[XAccount] = []
        for username in dict.fromkeys(value.lstrip("@") for value in usernames if value):
            account = self.get_user_by_username(username)
            if account is not None:
                output.append(account)
        return output

    def get_user_tweets(
        self,
        user_id: str,
        max_results: int = 10,
        *,
        username: str | None = None,
        include_replies: bool = False,
        start_time: str | None = None,
    ) -> list[XTweet]:
        handle = username.lstrip("@") if username else f"id:{user_id}"
        params: dict[str, Any] = {
            "count": max(1, min(max_results, 100)),
            "with_replies": "true" if include_replies else "false",
        }
        if start_time:
            from datetime import datetime

            try:
                params["since"] = datetime.fromisoformat(
                    start_time.replace("Z", "+00:00")
                ).timestamp()
            except ValueError:
                pass
        payload = self._get(
            f"/2/profile/{quote(handle, safe=':')}/statuses",
            params=params,
        )
        posts = [_parse_tweet(item) for item in self._results(payload)]
        for post in posts:
            post.source_provider = self.name
            if not post.author_id:
                post.author_id = str(user_id)
            if username and not post.author_username:
                post.author_username = username.lstrip("@")
        return posts[:max_results]

    def get_tweet(self, tweet_id: str) -> XTweet | None:
        payload = self._get(f"/2/status/{quote(str(tweet_id), safe='')}")
        value = payload.get("status")
        if not isinstance(value, dict) or value.get("type", "status") != "status":
            return None
        post = _parse_tweet(value)
        post.source_provider = self.name
        return post

    def get_followings(self, username: str, max_results: int = 20) -> list[XAccount]:
        return []

    def get_verified_followers(
        self,
        user_id: str,
        max_results: int = 20,
        *,
        username: str | None = None,
    ) -> list[XAccount]:
        return []

    def get_trends(
        self,
        max_results: int = 20,
        *,
        category: str | None = None,
        locale: str | None = None,
    ) -> list[XTrend]:
        payload = self._get(
            "/2/trends",
            params={"type": "trending", "count": max(1, min(max_results, 50))},
        )
        values = payload.get("trends") or payload.get("results") or []
        if not isinstance(values, list):
            return []
        output: list[XTrend] = []
        for position, item in enumerate(values, 1):
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            raw_rank = item.get("rank")
            rank = int(raw_rank) if isinstance(raw_rank, int) and raw_rank > 0 else position
            output.append(
                XTrend(
                    name=name,
                    rank=rank,
                    post_count=int(item.get("tweet_count") or item.get("post_count") or 0),
                    raw={
                        "source_provider": self.name,
                        "context": item.get("context"),
                        "requested_category": category,
                        "requested_locale": locale,
                        **item,
                    },
                )
            )
        return output[:max_results]
