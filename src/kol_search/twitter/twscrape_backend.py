from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from kol_search.twitter.models import XAccount, XReadCapabilities, XTweet
from kol_search.twitter.base import TwitterBackendError


def _parse_user(u: Any) -> XAccount:
    return XAccount(
        id=str(getattr(u, "id", "") or ""),
        username=str(getattr(u, "username", "") or getattr(u, "screen_name", "") or ""),
        name=getattr(u, "displayname", None) or getattr(u, "name", None),
        description=getattr(u, "rawDescription", None) or getattr(u, "description", None),
        followers_count=int(getattr(u, "followersCount", 0) or getattr(u, "followers_count", 0) or 0),
        following_count=int(getattr(u, "friendsCount", 0) or getattr(u, "friends_count", 0) or 0),
        tweet_count=int(getattr(u, "statusesCount", 0) or getattr(u, "statuses_count", 0) or 0),
        verified=bool(getattr(u, "verified", False) or getattr(u, "blue", False)),
        created_at=str(getattr(u, "created", "") or getattr(u, "created_at", "") or "") or None,
        profile_image_url=getattr(u, "profileImageUrl", None) or getattr(u, "profile_image_url", None),
        raw={"repr": repr(u)},
    )


def _parse_tweet(t: Any) -> XTweet:
    user = getattr(t, "user", None)
    author_username = None
    author_id = str(getattr(t, "user_id", "") or getattr(t, "userId", "") or "")
    if user is not None:
        author_username = getattr(user, "username", None) or getattr(user, "screen_name", None)
        author_id = author_id or str(getattr(user, "id", "") or "")
    mentions: list[str] = []
    for m in getattr(t, "mentionedUsers", None) or getattr(t, "mentions", None) or []:
        un = getattr(m, "username", None) or (m if isinstance(m, str) else None)
        if un:
            mentions.append(str(un).lstrip("@"))
    post_id = str(getattr(t, "id", "") or getattr(t, "id_str", "") or "")
    reply_to_id = getattr(t, "inReplyToTweetId", None) or getattr(t, "in_reply_to_status_id", None)
    reply_to_user = getattr(t, "inReplyToUser", None)
    reply_to_username = (
        getattr(reply_to_user, "username", None)
        or getattr(reply_to_user, "screen_name", None)
        or (reply_to_user if isinstance(reply_to_user, str) else None)
    )
    quoted = getattr(t, "quotedTweet", None)
    quoted_id = getattr(quoted, "id", None) if quoted is not None else None
    return XTweet(
        id=post_id,
        author_id=author_id,
        author_username=str(author_username).lstrip("@") if author_username else None,
        text=str(getattr(t, "rawContent", None) or getattr(t, "text", None) or ""),
        created_at=str(getattr(t, "date", "") or getattr(t, "created_at", "") or "") or None,
        like_count=int(getattr(t, "likeCount", 0) or getattr(t, "favorite_count", 0) or 0),
        retweet_count=int(getattr(t, "retweetCount", 0) or getattr(t, "retweet_count", 0) or 0),
        reply_count=int(getattr(t, "replyCount", 0) or getattr(t, "reply_count", 0) or 0),
        quote_count=int(getattr(t, "quoteCount", 0) or 0),
        view_count=int(getattr(t, "viewCount", 0) or getattr(t, "views", 0) or 0),
        bookmark_count=int(getattr(t, "bookmarkCount", 0) or 0),
        url=(
            getattr(t, "url", None)
            or (f"https://x.com/{author_username}/status/{post_id}" if author_username and post_id else None)
        ),
        conversation_id=str(getattr(t, "conversationId", None) or post_id),
        in_reply_to_user_id=(
            str(getattr(t, "inReplyToUserId", None))
            if getattr(t, "inReplyToUserId", None)
            else None
        ),
        in_reply_to_username=(
            str(reply_to_username).lstrip("@") if reply_to_username else None
        ),
        referenced_post_id=str(reply_to_id or quoted_id) if reply_to_id or quoted_id else None,
        reference_type="replied_to" if reply_to_id else ("quoted" if quoted_id else None),
        mentioned_usernames=mentions,
        raw={},
    )


class TwscrapeTwitterClient:
    """
    Backend using twscrape (account pool). Optional dependency.

    Install: pip install -e ".[twscrape]"
    Accounts file: username:password:email:email_password per line
    """

    name = "twscrape"
    capabilities = XReadCapabilities(
        user_search=False,
        batch_user_lookup=False,
        user_search_page_size=0,
        post_search_page_size=100,
        batch_user_lookup_size=1,
    )

    def __init__(self, accounts_file: str | None = None) -> None:
        try:
            from twscrape import API  # type: ignore
        except ImportError as e:
            raise TwitterBackendError(
                "twscrape is not installed.",
                hint='Install with: pip install -e ".[twscrape]" then configure TWSCRAPE_ACCOUNTS_FILE. '
                "See README.md. Research use only — account ban / ToS risk.",
            ) from e

        self._API = API
        self._api = API()
        self._accounts_file = accounts_file
        self._ready = False

    async def _ensure_ready(self) -> None:
        if self._ready:
            return
        if self._accounts_file:
            path = Path(self._accounts_file)
            if path.exists():
                # twscrape load accounts from file if supported
                try:
                    await self._api.pool.load_accounts(str(path))  # type: ignore[attr-defined]
                except Exception:
                    # fallback: add_account line by line
                    for line in path.read_text(encoding="utf-8").splitlines():
                        line = line.strip()
                        if not line or line.startswith("#"):
                            continue
                        parts = line.split(":")
                        if len(parts) >= 4:
                            await self._api.pool.add_account(parts[0], parts[1], parts[2], parts[3])
                try:
                    await self._api.pool.login_all()
                except Exception as e:
                    raise TwitterBackendError(
                        f"twscrape login failed: {e}",
                        hint="Check account credentials and 2FA/email access.",
                    ) from e
        self._ready = True

    def _run(self, coro):  # noqa: ANN001
        return asyncio.run(coro)

    def search_tweets(
        self,
        query: str,
        max_results: int = 40,
        since_id: str | None = None,
    ) -> list[XTweet]:
        async def _inner() -> list[XTweet]:
            await self._ensure_ready()
            posts: list[XTweet] = []
            n = 0
            async for t in self._api.search(query, limit=max_results):
                posts.append(_parse_tweet(t))
                n += 1
                if n >= max_results:
                    break
            return posts

        try:
            return self._run(_inner())
        except TwitterBackendError:
            raise
        except Exception as e:
            raise TwitterBackendError(
                f"twscrape search failed: {e}",
                hint="Research-only. Prefer official/third_party when possible. See README.md.",
            ) from e

    def search_users(self, query: str, max_results: int = 100) -> list[XAccount]:
        return []

    def get_user_by_username(self, username: str) -> XAccount | None:
        username = username.lstrip("@")

        async def _inner() -> XAccount | None:
            await self._ensure_ready()
            u = await self._api.user_by_login(username)
            return _parse_user(u) if u else None

        try:
            return self._run(_inner())
        except TwitterBackendError:
            raise
        except Exception as e:
            raise TwitterBackendError(f"twscrape get user failed: {e}") from e

    def get_users_by_usernames(self, usernames: list[str]) -> list[XAccount]:
        out: list[XAccount] = []
        for u in usernames:
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
        async def _inner() -> list[XTweet]:
            await self._ensure_ready()
            posts: list[XTweet] = []
            n = 0
            # twscrape uses user id int often
            try:
                uid = int(user_id)
            except ValueError:
                uid = user_id
            async for t in self._api.user_tweets(uid, limit=max_results):
                posts.append(_parse_tweet(t))
                n += 1
                if n >= max_results:
                    break
            return posts

        try:
            return self._run(_inner())
        except TwitterBackendError:
            raise
        except Exception as e:
            raise TwitterBackendError(f"twscrape user tweets failed: {e}") from e
