from __future__ import annotations

import asyncio
from pathlib import Path
from threading import Lock
from typing import Any

from kol_search.twitter.models import XAccount, XReadCapabilities, XTrend, XTweet
from kol_search.twitter.base import TwitterBackendError


def _parse_user(u: Any) -> XAccount:
    return XAccount(
        id=str(getattr(u, "id", "") or ""),
        source_provider="twscrape",
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
        source_provider="twscrape",
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
    Prefer a dedicated account's auth_token + ct0 cookies. Password login files
    remain supported only for compatibility with existing internal setups.
    """

    name = "twscrape"
    capabilities = XReadCapabilities(
        user_search=True,
        post_lookup=True,
        batch_user_lookup=False,
        followings=True,
        verified_followers=True,
        trends=True,
        user_search_page_size=0,
        post_search_page_size=100,
        batch_user_lookup_size=1,
    )

    def __init__(
        self,
        accounts_file: str | None = None,
        *,
        accounts_db: str = "data/twscrape_accounts.db",
        username: str = "kol-search-test",
        auth_token: str | None = None,
        ct0: str | None = None,
        wait_timeout: float = 15.0,
    ) -> None:
        try:
            from twscrape import API  # type: ignore
            from twscrape.logger import set_log_level  # type: ignore
        except ImportError as e:
            raise TwitterBackendError(
                "twscrape is not installed.",
                hint='Install with: pip install -e ".[twscrape]" then configure TWSCRAPE_ACCOUNTS_FILE. '
                "See README.md. Research use only — account ban / ToS risk.",
            ) from e

        # twscrape's default INFO/WARNING output includes the pool username.
        # Keep backend logs free of account identifiers and session diagnostics.
        set_log_level("ERROR")

        accounts_path = Path(accounts_db)
        accounts_path.parent.mkdir(parents=True, exist_ok=True)
        self._api = API(
            str(accounts_path),
            raise_when_no_account=True,
        )
        self._accounts_file = accounts_file
        self._username = username.strip() or "kol-search-test"
        self._auth_token = auth_token
        self._ct0 = ct0
        self._wait_timeout = max(1.0, wait_timeout)
        self._operation_lock = Lock()
        self._ready = False
        self._unavailable_reason: str | None = None

    async def _ensure_ready(self) -> None:
        if self._ready:
            return
        if self._unavailable_reason:
            raise TwitterBackendError(self._unavailable_reason)
        if self._auth_token and self._ct0:
            existing = await self._api.pool.get_account(self._username)
            if existing is None:
                cookies = f"auth_token={self._auth_token}; ct0={self._ct0}"
                await self._api.pool.add_account_cookies(self._username, cookies)
            else:
                # Cookie sessions rotate. Refresh the persisted pool entry from
                # backend secrets without requiring operators to delete the DB.
                existing.cookies.update(
                    {"auth_token": self._auth_token, "ct0": self._ct0}
                )
                existing.active = True
                existing.error_msg = None
                await self._api.pool.save(existing)
        elif self._accounts_file:
            path = Path(self._accounts_file)
            if path.exists():
                try:
                    await self._api.pool.load_from_file(
                        str(path), "username:password:email:email_password"
                    )
                    await self._api.pool.login_all()
                except Exception as e:
                    raise TwitterBackendError(
                        f"twscrape login failed: {e}",
                        hint="Check account credentials and 2FA/email access.",
                    ) from e
        accounts = await self._api.pool.get_all()
        if not accounts:
            raise TwitterBackendError(
                "twscrape has no configured account session.",
                hint="Set TWSCRAPE_AUTH_TOKEN and TWSCRAPE_CT0 for a dedicated test account.",
            )

        # twscrape can swallow a GraphQL 404 and yield an empty iterator. Probe
        # one stable public account so that an upstream/session failure triggers
        # the connector fallback instead of being misreported as zero results.
        try:
            probe = await self._api.user_by_login("X")
            probe_post = None
            if probe is not None:
                async for value in self._api.search("from:X", limit=1):
                    probe_post = value
                    break
        except Exception as exc:
            self._unavailable_reason = "twscrape session probe failed."
            raise TwitterBackendError(
                self._unavailable_reason,
                hint="The X web API or transaction-ID flow is currently unavailable.",
            ) from exc
        if probe is None or probe_post is None:
            self._unavailable_reason = "twscrape session probe returned no data."
            raise TwitterBackendError(
                self._unavailable_reason,
                hint="The X web API or transaction-ID flow is currently unavailable.",
            )
        self._ready = True

    def _run(self, coro):  # noqa: ANN001
        with self._operation_lock:
            return asyncio.run(asyncio.wait_for(coro, timeout=self._wait_timeout))

    def search_tweets(
        self,
        query: str,
        max_results: int = 40,
        since_id: str | None = None,
        start_time: str | None = None,
    ) -> list[XTweet]:
        effective_query = query
        if start_time:
            effective_query = f"{query} since:{start_time[:10]}"

        async def _inner() -> list[XTweet]:
            await self._ensure_ready()
            posts: list[XTweet] = []
            n = 0
            async for t in self._api.search(effective_query, limit=max_results):
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
        async def _inner() -> list[XAccount]:
            await self._ensure_ready()
            users: list[XAccount] = []
            async for user in self._api.search_user(query, limit=max_results):
                users.append(_parse_user(user))
                if len(users) >= max_results:
                    break
            return users

        try:
            return self._run(_inner())
        except TwitterBackendError:
            raise
        except Exception as e:
            raise TwitterBackendError(f"twscrape user search failed: {e}") from e

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
        start_time: str | None = None,
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

    def get_tweet(self, tweet_id: str) -> XTweet | None:
        async def _inner() -> XTweet | None:
            await self._ensure_ready()
            post = await self._api.tweet_details(int(tweet_id))
            return _parse_tweet(post) if post is not None else None

        try:
            return self._run(_inner())
        except TwitterBackendError:
            raise
        except Exception as e:
            raise TwitterBackendError(f"twscrape tweet lookup failed: {e}") from e

    def get_followings(self, username: str, max_results: int = 20) -> list[XAccount]:
        account = self.get_user_by_username(username)
        if account is None:
            return []

        async def _inner() -> list[XAccount]:
            await self._ensure_ready()
            output: list[XAccount] = []
            async for user in self._api.following(int(account.id), limit=max_results):
                output.append(_parse_user(user))
                if len(output) >= max_results:
                    break
            return output

        try:
            return self._run(_inner())
        except TwitterBackendError:
            raise
        except Exception as e:
            raise TwitterBackendError(f"twscrape followings failed: {e}") from e

    def get_verified_followers(
        self,
        user_id: str,
        max_results: int = 20,
        *,
        username: str | None = None,
    ) -> list[XAccount]:
        async def _inner() -> list[XAccount]:
            await self._ensure_ready()
            output: list[XAccount] = []
            async for user in self._api.verified_followers(
                int(user_id), limit=max_results
            ):
                output.append(_parse_user(user))
                if len(output) >= max_results:
                    break
            return output

        try:
            return self._run(_inner())
        except TwitterBackendError:
            raise
        except Exception as e:
            raise TwitterBackendError(f"twscrape verified followers failed: {e}") from e

    def get_trends(
        self,
        max_results: int = 20,
        *,
        category: str | None = None,
        locale: str | None = None,
    ) -> list[XTrend]:
        category_aliases = {
            "sports": "sport",
            "sport": "sport",
            "news": "news",
            "entertainment": "entertainment",
            "trending": "trending",
        }
        trend_id = category_aliases.get((category or "trending").lower(), "trending")

        async def _inner() -> list[XTrend]:
            await self._ensure_ready()
            output: list[XTrend] = []
            async for value in self._api.trends(trend_id, limit=max_results):
                metadata = getattr(value, "trend_metadata", None)
                trend_url = getattr(value, "trend_url", None)
                output.append(
                    XTrend(
                        name=str(getattr(value, "name", "") or ""),
                        rank=int(getattr(value, "rank", 0) or len(output) + 1),
                        url=getattr(trend_url, "url", None),
                        raw={
                            "source_provider": self.name,
                            "id": getattr(value, "id", None),
                            "context": getattr(metadata, "domain_context", None),
                            "description": getattr(metadata, "meta_description", None),
                            "requested_category": category,
                            "requested_locale": locale,
                        },
                    )
                )
                if len(output) >= max_results:
                    break
            return [item for item in output if item.name]

        try:
            return self._run(_inner())
        except TwitterBackendError:
            raise
        except Exception as e:
            raise TwitterBackendError(f"twscrape trends failed: {e}") from e
