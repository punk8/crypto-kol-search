from __future__ import annotations

import re
from typing import Any

import httpx

from kol_search.models import Account, BackendCapabilities, Post
from kol_search.twitter.base import TwitterBackendError

API_BASE = "https://api.x.com/2"


def _user_fields() -> str:
    return (
        "created_at,description,public_metrics,verified,profile_image_url,url,"
        "location,entities,protected"
    )


def _tweet_fields() -> str:
    return (
        "created_at,public_metrics,lang,entities,author_id,conversation_id,"
        "in_reply_to_user_id,referenced_tweets"
    )


def _parse_account(data: dict[str, Any]) -> Account:
    metrics = data.get("public_metrics") or {}
    return Account(
        id=str(data["id"]),
        username=data.get("username", ""),
        name=data.get("name"),
        description=data.get("description"),
        followers_count=int(metrics.get("followers_count", 0)),
        following_count=int(metrics.get("following_count", 0)),
        tweet_count=int(metrics.get("tweet_count", 0)),
        listed_count=int(metrics.get("listed_count", 0)),
        verified=bool(data.get("verified", False)),
        created_at=data.get("created_at"),
        profile_image_url=data.get("profile_image_url"),
        url=data.get("url"),
        location=data.get("location"),
        entities=data.get("entities") or {},
        protected=bool(data.get("protected", False)),
        raw=data,
    )


def _mentions_from_entities(entities: dict[str, Any] | None) -> list[str]:
    if not entities:
        return []
    out: list[str] = []
    for m in entities.get("mentions") or []:
        u = m.get("username")
        if u:
            out.append(u.lstrip("@"))
    return out


def _parse_post(data: dict[str, Any], users_by_id: dict[str, Account] | None = None) -> Post:
    metrics = data.get("public_metrics") or {}
    author_id = str(data.get("author_id", ""))
    author_username = None
    if users_by_id and author_id in users_by_id:
        author_username = users_by_id[author_id].username
    references = data.get("referenced_tweets") or []
    reference = references[0] if references else {}
    post_id = str(data["id"])
    return Post(
        id=post_id,
        author_id=author_id,
        author_username=author_username,
        text=data.get("text", ""),
        created_at=data.get("created_at"),
        like_count=int(metrics.get("like_count", 0)),
        retweet_count=int(metrics.get("retweet_count", 0)),
        reply_count=int(metrics.get("reply_count", 0)),
        quote_count=int(metrics.get("quote_count", 0)),
        view_count=int(metrics.get("impression_count", 0)),
        bookmark_count=int(metrics.get("bookmark_count", 0)),
        lang=data.get("lang"),
        url=f"https://x.com/{author_username}/status/{post_id}" if author_username else None,
        conversation_id=str(data.get("conversation_id") or post_id),
        in_reply_to_user_id=(
            str(data["in_reply_to_user_id"]) if data.get("in_reply_to_user_id") else None
        ),
        referenced_post_id=str(reference["id"]) if reference.get("id") else None,
        reference_type=reference.get("type"),
        mentioned_usernames=_mentions_from_entities(data.get("entities")),
        raw=data,
    )


class OfficialTwitterClient:
    """X API v2 with Bearer token (app-only)."""

    name = "official"
    capabilities = BackendCapabilities(user_search=True)

    def __init__(self, bearer_token: str, timeout: float = 30.0) -> None:
        if not bearer_token:
            raise TwitterBackendError(
                "X_BEARER_TOKEN is empty.",
                hint="See README.md to configure an X developer Bearer Token.",
            )
        self._client = httpx.Client(
            base_url=API_BASE,
            headers={
                "Authorization": f"Bearer {bearer_token}",
                "User-Agent": "kol-search/0.1 (research)",
            },
            timeout=timeout,
        )

    def close(self) -> None:
        self._client.close()

    def _request(self, method: str, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            resp = self._client.request(method, path, params=params)
        except httpx.HTTPError as e:
            raise TwitterBackendError(f"Network error calling X API: {e}") from e

        if resp.status_code == 401:
            raise TwitterBackendError(
                "Unauthorized — invalid or expired Bearer Token.",
                status_code=401,
                hint="Regenerate Bearer Token in console.x.com and update X_BEARER_TOKEN.",
            )
        if resp.status_code in (402, 403):
            raise TwitterBackendError(
                "Access denied for this endpoint or plan.",
                status_code=resp.status_code,
                hint=(
                    "Your X API tier may not include this endpoint (e.g. recent search). "
                    "Upgrade plan, or switch TWITTER_BACKEND to third_party / twscrape / mock. "
                    "See README.md for backend configuration."
                ),
            )
        if resp.status_code == 429:
            raise TwitterBackendError(
                "Rate limited by X API.",
                status_code=429,
                hint="Wait and retry, reduce search_max_queries, or use another backend.",
            )
        if resp.status_code >= 400:
            detail = resp.text[:500]
            raise TwitterBackendError(
                f"X API error: {detail}",
                status_code=resp.status_code,
                hint="See README.md",
            )
        return resp.json()

    def search_tweets(
        self,
        query: str,
        max_results: int = 40,
        since_id: str | None = None,
    ) -> list[Post]:
        # API allows 10–100
        n = max(10, min(100, max_results))
        params: dict[str, Any] = {
            "query": query,
            "max_results": n,
            "tweet.fields": _tweet_fields(),
            "expansions": "author_id",
            "user.fields": _user_fields(),
        }
        if since_id:
            params["since_id"] = since_id
        data = self._request("GET", "/tweets/search/recent", params=params)
        users_by_id: dict[str, Account] = {}
        for u in (data.get("includes") or {}).get("users") or []:
            acc = _parse_account(u)
            users_by_id[acc.id] = acc
        posts: list[Post] = []
        for t in data.get("data") or []:
            posts.append(_parse_post(t, users_by_id))
        return posts[:max_results]

    def search_users(self, query: str, max_results: int = 100) -> list[Account]:
        remaining = max(1, min(1000, max_results))
        token: str | None = None
        out: list[Account] = []
        while remaining > 0:
            params: dict[str, Any] = {
                "query": query[:50],
                "max_results": min(100, remaining),
                "user.fields": _user_fields(),
            }
            if token:
                params["next_token"] = token
            payload = self._request("GET", "/users/search", params=params)
            for item in payload.get("data") or []:
                out.append(_parse_account(item))
            remaining = max_results - len(out)
            token = (payload.get("meta") or {}).get("next_token")
            if not token or not payload.get("data"):
                break
        return out[:max_results]

    def get_user_by_username(self, username: str) -> Account | None:
        username = username.lstrip("@")
        data = self._request(
            "GET",
            f"/users/by/username/{username}",
            params={"user.fields": _user_fields()},
        )
        if not data.get("data"):
            return None
        return _parse_account(data["data"])

    def get_users_by_usernames(self, usernames: list[str]) -> list[Account]:
        # API allows up to 100 per request
        clean = [u.lstrip("@") for u in usernames if u]
        out: list[Account] = []
        for i in range(0, len(clean), 100):
            batch = clean[i : i + 100]
            data = self._request(
                "GET",
                "/users/by",
                params={
                    "usernames": ",".join(batch),
                    "user.fields": _user_fields(),
                },
            )
            for u in data.get("data") or []:
                out.append(_parse_account(u))
        return out

    def get_user_tweets(
        self,
        user_id: str,
        max_results: int = 10,
        *,
        username: str | None = None,
        include_replies: bool = False,
    ) -> list[Post]:
        n = max(5, min(100, max_results))
        data = self._request(
            "GET",
            f"/users/{user_id}/tweets",
            params={
                "max_results": n,
                "tweet.fields": _tweet_fields(),
                "exclude": "retweets" if include_replies else "retweets,replies",
            },
        )
        posts = [_parse_post(t) for t in data.get("data") or []]
        # Timeline responses omit author expansions; fill known identity locally.
        for p in posts:
            if not p.author_id:
                p.author_id = str(user_id)
            if username and not p.author_username:
                p.author_username = username.lstrip("@")
            if p.author_username and not p.url:
                p.url = f"https://x.com/{p.author_username}/status/{p.id}"
        return posts[:max_results]


# silence unused import if re used later
_ = re
