from __future__ import annotations

from typing import Any
from urllib.parse import urljoin

import httpx

from kol_search.twitter.models import XAccount, XReadCapabilities, XTweet
from kol_search.twitter.base import TwitterBackendError


def _as_int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _parse_user(data: dict[str, Any]) -> XAccount:
    """Best-effort mapping across common third-party shapes."""
    metrics = data.get("public_metrics") or data.get("legacy") or {}
    profile_bio = data.get("profile_bio") or {}
    if not isinstance(profile_bio, dict):
        profile_bio = {}
    entities = data.get("entities") or profile_bio.get("entities") or {}
    if not isinstance(entities, dict):
        entities = {}

    website_url: str | None = None
    for url_item in (entities.get("url") or {}).get("urls") or []:
        if isinstance(url_item, dict):
            website_url = url_item.get("expanded_url") or url_item.get("url")
            if website_url:
                break
    username = (
        data.get("username")
        or data.get("screen_name")
        or data.get("userName")
        or ""
    )
    uid = str(data.get("id") or data.get("id_str") or data.get("user_id") or username)
    followers = (
        data.get("followers_count")
        or metrics.get("followers_count")
        or data.get("followers")
        or 0
    )
    return XAccount(
        id=uid,
        username=str(username).lstrip("@"),
        name=data.get("name") or data.get("display_name"),
        description=(
            data.get("description")
            or data.get("bio")
            or profile_bio.get("description")
        ),
        followers_count=_as_int(followers),
        following_count=_as_int(
            data.get("following_count")
            or metrics.get("following_count")
            or data.get("friends_count")
            or data.get("following")
        ),
        tweet_count=_as_int(
            data.get("tweet_count")
            or metrics.get("tweet_count")
            or data.get("statuses_count")
            or data.get("statusesCount")
        ),
        verified=bool(data.get("verified") or data.get("isVerified") or data.get("isBlueVerified")),
        created_at=data.get("created_at") or data.get("createdAt"),
        profile_image_url=(
            data.get("profile_image_url")
            or data.get("profile_image_url_https")
            or data.get("profilePicture")
        ),
        url=website_url or data.get("website"),
        location=data.get("location"),
        entities=entities,
        protected=bool(data.get("protected", False)),
        raw=data,
    )


def _parse_tweet(data: dict[str, Any]) -> XTweet:
    user = data.get("user") or data.get("author") or {}
    author_username = (
        data.get("author_username")
        or user.get("username")
        or user.get("userName")
        or user.get("screen_name")
        or data.get("screen_name")
    )
    author_id = str(
        data.get("author_id")
        or user.get("id")
        or user.get("id_str")
        or data.get("user_id")
        or author_username
        or ""
    )
    metrics = data.get("public_metrics") or {}
    text = data.get("text") or data.get("full_text") or data.get("content") or ""
    mentions: list[str] = []
    entities = data.get("entities") or {}
    for m in entities.get("user_mentions") or entities.get("mentions") or []:
        u = m.get("username") or m.get("screen_name")
        if u:
            mentions.append(str(u).lstrip("@"))
    # also scrape @handles from text as fallback
    if not mentions and text:
        import re

        mentions = [m.lstrip("@") for m in re.findall(r"@([A-Za-z0-9_]{1,15})", text)]

    post_id = str(data.get("id") or data.get("id_str") or data.get("tweet_id") or "")
    references = data.get("referenced_tweets") or []
    reference = references[0] if references and isinstance(references[0], dict) else {}
    reference_type = (
        reference.get("type")
        or ("replied_to" if data.get("inReplyToId") or data.get("in_reply_to_status_id") else None)
        or ("quoted" if data.get("quoted_tweet_id") or data.get("quotedTweet") else None)
    )
    reference_aliases = {
        "reply": "replied_to",
        "replied_to": "replied_to",
        "quote": "quoted",
        "quoted": "quoted",
        "retweet": "retweeted",
        "retweeted": "retweeted",
    }
    reference_type = reference_aliases.get(str(reference_type).lower()) if reference_type else None
    referenced_post_id = (
        reference.get("id")
        or data.get("inReplyToId")
        or data.get("in_reply_to_status_id")
        or data.get("quoted_tweet_id")
        or (data.get("quotedTweet") or {}).get("id")
    )
    in_reply_to_username = (
        data.get("inReplyToUsername")
        or data.get("in_reply_to_username")
        or data.get("in_reply_to_screen_name")
        or data.get("replyToUsername")
    )

    return XTweet(
        id=post_id,
        author_id=author_id,
        author_username=str(author_username).lstrip("@") if author_username else None,
        text=text,
        created_at=data.get("created_at") or data.get("createdAt"),
        like_count=_as_int(
            data.get("like_count")
            or data.get("likeCount")
            or data.get("favorite_count")
            or data.get("likes")
            or metrics.get("like_count")
        ),
        retweet_count=_as_int(
            data.get("retweet_count")
            or data.get("retweetCount")
            or data.get("retweets")
            or metrics.get("retweet_count")
        ),
        reply_count=_as_int(
            data.get("reply_count")
            or data.get("replyCount")
            or data.get("replies")
            or metrics.get("reply_count")
        ),
        quote_count=_as_int(
            data.get("quote_count") or data.get("quoteCount") or metrics.get("quote_count")
        ),
        view_count=_as_int(
            data.get("view_count")
            or data.get("viewCount")
            or data.get("views")
            or metrics.get("impression_count")
        ),
        bookmark_count=_as_int(
            data.get("bookmark_count")
            or data.get("bookmarkCount")
            or metrics.get("bookmark_count")
        ),
        lang=data.get("lang"),
        url=(
            data.get("url")
            or data.get("tweet_url")
            or (f"https://x.com/{str(author_username).lstrip('@')}/status/{post_id}" if author_username and post_id else None)
        ),
        conversation_id=str(data.get("conversation_id") or data.get("conversationId") or post_id),
        in_reply_to_user_id=(
            str(data.get("in_reply_to_user_id") or data.get("inReplyToUserId"))
            if data.get("in_reply_to_user_id") or data.get("inReplyToUserId")
            else None
        ),
        in_reply_to_username=(
            str(in_reply_to_username).lstrip("@") if in_reply_to_username else None
        ),
        referenced_post_id=str(referenced_post_id) if referenced_post_id else None,
        reference_type=reference_type,
        mentioned_usernames=mentions,
        raw=data,
    )


class ThirdPartyTwitterClient:
    """
    Generic REST adapter for third-party Twitter data APIs.

    Expected routes (relative to TWITTER_TP_BASE_URL):
      GET /search/tweets?query=...&max_results=...
      GET /users/by/username/{username}
      GET /users/by?usernames=a,b,c
      GET /users/{id}/tweets?max_results=...

    Response shapes accepted:
      - { "data": [ ... ] } or { "data": { ... } }
      - { "tweets": [ ... ] } / { "users": [ ... ] }
      - raw list
    """

    name = "third_party"

    def __init__(
        self,
        base_url: str,
        api_key: str,
        api_key_header: str = "x-api-key",
        timeout: float = 30.0,
        supports_user_search: bool = False,
    ) -> None:
        if not base_url or not api_key:
            raise TwitterBackendError(
                "TWITTER_TP_BASE_URL and TWITTER_TP_API_KEY are required for third_party backend.",
                hint="See README.md for vendor configuration.",
            )
        self._base = base_url.rstrip("/") + "/"
        self.capabilities = XReadCapabilities(user_search=supports_user_search)
        self._client = httpx.Client(
            headers={
                api_key_header: api_key,
                "Authorization": f"Bearer {api_key}",
                "User-Agent": "kol-search/0.1 (research)",
            },
            timeout=timeout,
        )

    def close(self) -> None:
        self._client.close()

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = urljoin(self._base, path.lstrip("/"))
        try:
            resp = self._client.get(url, params=params)
        except httpx.HTTPError as e:
            raise TwitterBackendError(f"Third-party network error: {e}") from e
        if resp.status_code >= 400:
            raise TwitterBackendError(
                f"Third-party API error: {resp.text[:400]}",
                status_code=resp.status_code,
                hint="Check TWITTER_TP_BASE_URL path mapping described in README.md.",
            )
        return resp.json()

    @staticmethod
    def _extract_list(payload: Any, keys: tuple[str, ...]) -> list[dict[str, Any]]:
        if isinstance(payload, list):
            return [x for x in payload if isinstance(x, dict)]
        if not isinstance(payload, dict):
            return []
        for k in keys:
            v = payload.get(k)
            if isinstance(v, list):
                return [x for x in v if isinstance(x, dict)]
            if isinstance(v, dict) and k == "data":
                return [v]
        data = payload.get("data")
        if isinstance(data, dict) and "data" in data:
            return ThirdPartyTwitterClient._extract_list(data, keys)
        return []

    def search_tweets(
        self,
        query: str,
        max_results: int = 40,
        since_id: str | None = None,
    ) -> list[XTweet]:
        params: dict[str, Any] = {"query": query, "max_results": max_results, "limit": max_results}
        if since_id:
            params["since_id"] = since_id
        payload = self._get("search/tweets", params=params)
        items = self._extract_list(payload, ("data", "tweets", "results"))
        return [_parse_tweet(t) for t in items][:max_results]

    def search_users(self, query: str, max_results: int = 100) -> list[XAccount]:
        if not self.capabilities.user_search:
            return []
        payload = self._get("search/users", params={"query": query, "max_results": max_results})
        items = self._extract_list(payload, ("data", "users", "results"))
        return [_parse_user(item) for item in items][:max_results]

    def get_user_by_username(self, username: str) -> XAccount | None:
        username = username.lstrip("@")
        payload = self._get(f"users/by/username/{username}")
        if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
            return _parse_user(payload["data"])
        items = self._extract_list(payload, ("data", "users", "user"))
        if items:
            return _parse_user(items[0])
        if isinstance(payload, dict) and (payload.get("username") or payload.get("screen_name")):
            return _parse_user(payload)
        return None

    def get_users_by_usernames(self, usernames: list[str]) -> list[XAccount]:
        clean = [u.lstrip("@") for u in usernames if u]
        if not clean:
            return []
        payload = self._get("users/by", params={"usernames": ",".join(clean)})
        items = self._extract_list(payload, ("data", "users"))
        if items:
            return [_parse_user(u) for u in items]
        # fallback: sequential
        out: list[XAccount] = []
        for u in clean:
            acc = self.get_user_by_username(u)
            if acc:
                out.append(acc)
        return out

    def get_user_tweets(
        self,
        user_id: str,
        max_results: int = 10,
        *,
        username: str | None = None,
        include_replies: bool = False,
    ) -> list[XTweet]:
        payload = self._get(
            f"users/{user_id}/tweets",
            params={
                "max_results": max_results,
                "limit": max_results,
                "include_replies": str(include_replies).lower(),
            },
        )
        items = self._extract_list(payload, ("data", "tweets", "results"))
        posts = [_parse_tweet(t) for t in items]
        for p in posts:
            if not p.author_id:
                p.author_id = str(user_id)
        return posts[:max_results]
