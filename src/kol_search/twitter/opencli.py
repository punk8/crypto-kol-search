from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
from collections.abc import Callable
from datetime import datetime
from typing import Any

from kol_search.twitter.models import XAccount, XReadCapabilities, XTweet, XTrend
from kol_search.twitter.base import TwitterBackendError


AccountIdResolver = Callable[[str, str], str]


def _as_int(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    text = str(value or "").strip().replace(",", "")
    try:
        return int(float(text))
    except ValueError:
        return 0


def _mentions(text: str) -> list[str]:
    return list(dict.fromkeys(re.findall(r"@([A-Za-z0-9_]{1,15})", text or "")))


def _timestamp(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).isoformat()
    except ValueError:
        pass
    try:
        return datetime.strptime(text, "%a %b %d %H:%M:%S %z %Y").isoformat()
    except ValueError:
        return text


class OpenCliTwitterClient:
    """Read-only Twitter backend implemented through an authenticated OpenCLI session."""

    name = "opencli"
    capabilities = XReadCapabilities(
        user_search=True,
        post_search=True,
        batch_user_lookup=True,
        user_timeline=True,
        followings=True,
        verified_followers=False,
        trends=True,
        user_search_page_size=20,
        post_search_page_size=100,
        batch_user_lookup_size=10,
        followings_page_size=50,
        verified_followers_page_size=0,
    )
    _lock = threading.Lock()

    def __init__(
        self,
        *,
        command: str = "opencli",
        profile: str = "ddd",
        timeout: float = 90.0,
        resolve_account_id: AccountIdResolver | None = None,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self.command = command
        self.profile = profile
        self.timeout = timeout
        self._resolve_account_id = resolve_account_id
        self._runner = runner
        self.diagnostics: dict[str, Any] = {
            "active_backend": self.name,
            "backend_calls": {self.name: 0},
            "fallback_count": 0,
        }
        self.warnings: list[str] = []

    def begin_run(self) -> None:
        """Reset per-job diagnostics while preserving the connected profile."""

        self.diagnostics = {
            "active_backend": self.name,
            "backend_calls": {self.name: 0},
            "fallback_count": 0,
        }
        self.warnings.clear()

    def _id(self, username: str) -> str:
        handle = username.lstrip("@").strip()
        proposed = f"opencli:{handle.lower()}"
        if self._resolve_account_id:
            return self._resolve_account_id(handle, proposed)
        return proposed

    def _run(self, *args: str) -> list[dict[str, Any]]:
        if not shutil.which(self.command) and "/" not in self.command:
            raise TwitterBackendError(
                f"OpenCLI command not found: {self.command}",
                hint="Install OpenCLI or set OPENCLI_COMMAND.",
            )
        command = [
            self.command,
            "--profile",
            self.profile,
            "twitter",
            *args,
            "-f",
            "json",
        ]
        try:
            with self._lock:
                result = self._runner(
                    command,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout,
                    check=False,
                )
        except subprocess.TimeoutExpired as exc:
            raise TwitterBackendError(
                f"OpenCLI timed out after {self.timeout:g}s",
                hint=f"Keep Chrome profile {self.profile!r} open and reduce query size.",
            ) from exc
        except OSError as exc:
            raise TwitterBackendError(f"OpenCLI failed to start: {exc}") from exc
        self.diagnostics["backend_calls"][self.name] += 1
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "unknown OpenCLI error").strip()[-800:]
            raise TwitterBackendError(
                f"OpenCLI command failed: {detail}",
                status_code=77 if "AUTH_REQUIRED" in detail or "login" in detail.lower() else None,
                hint="Run `opencli doctor`, connect Browser Bridge, and verify X is logged in.",
            )
        try:
            payload = json.loads(result.stdout or "[]")
        except json.JSONDecodeError as exc:
            raise TwitterBackendError("OpenCLI returned invalid JSON.") from exc
        if isinstance(payload, dict):
            if payload.get("ok") is False or payload.get("error"):
                raise TwitterBackendError(f"OpenCLI error: {payload.get('error') or payload}")
            payload = [payload]
        if not isinstance(payload, list):
            raise TwitterBackendError("OpenCLI returned an unexpected response shape.")
        return [item for item in payload if isinstance(item, dict)]

    def _account(self, item: dict[str, Any]) -> XAccount:
        user = item.get("user") or item.get("author_info") or {}
        if not isinstance(user, dict):
            user = {}
        username = str(
            item.get("screen_name")
            or item.get("author")
            or item.get("username")
            or user.get("screen_name")
            or user.get("username")
            or ""
        ).lstrip("@")
        return XAccount(
            id=self._id(username),
            username=username,
            name=item.get("name") or user.get("name"),
            description=item.get("bio") or user.get("bio") or user.get("description"),
            followers_count=_as_int(item.get("followers") or user.get("followers")),
            following_count=_as_int(item.get("following") or user.get("following")),
            tweet_count=_as_int(item.get("tweets") or user.get("tweets")),
            verified=bool(item.get("verified", False) or user.get("verified", False)),
            created_at=_timestamp(item.get("created_at") or user.get("created_at")),
            url=item.get("url") or user.get("url") or None,
            location=item.get("location") or user.get("location") or None,
            raw={"source_backend": self.name, "opencli": item},
        )

    def _post(self, item: dict[str, Any]) -> XTweet:
        username = str(item.get("author") or "").lstrip("@") or None
        text = str(item.get("text") or "")
        post_id = str(item.get("id") or "")
        reply_to = item.get("in_reply_to_status_id") or item.get("reply_to_id")
        quoted_id = item.get("quoted_tweet_id") or item.get("quote_id")
        return XTweet(
            id=post_id,
            author_id=self._id(username) if username else "",
            author_username=username,
            text=text,
            created_at=_timestamp(item.get("created_at")),
            like_count=_as_int(item.get("likes")),
            retweet_count=_as_int(item.get("retweets")),
            reply_count=_as_int(item.get("replies")),
            quote_count=_as_int(item.get("quotes")),
            view_count=_as_int(item.get("views")),
            bookmark_count=_as_int(item.get("bookmarks")),
            url=(
                item.get("url")
                or (f"https://x.com/{username}/status/{post_id}" if username and post_id else None)
            ),
            conversation_id=str(item.get("conversation_id") or post_id),
            in_reply_to_username=(
                str(item.get("in_reply_to_username") or item.get("reply_to_username")).lstrip("@")
                if item.get("in_reply_to_username") or item.get("reply_to_username")
                else None
            ),
            referenced_post_id=str(reply_to or quoted_id) if reply_to or quoted_id else None,
            reference_type="replied_to" if reply_to else ("quoted" if quoted_id else None),
            mentioned_usernames=_mentions(text),
            raw={"source_backend": self.name, "opencli": item},
        )

    def search_tweets(
        self,
        query: str,
        max_results: int = 40,
        since_id: str | None = None,
        start_time: str | None = None,
    ) -> list[XTweet]:
        effective = query
        if since_id:
            # OpenCLI/X search does not support since_id directly; keep the API surface compatible.
            effective = f"{query} since_id:{since_id}"
        if start_time:
            effective = f"{effective} since:{start_time[:10]}"
        rows = self._run(
            "search", effective, "--product", "live", "--limit", str(max_results)
        )
        return [self._post(item) for item in rows][:max_results]

    def search_users(self, query: str, max_results: int = 100) -> list[XAccount]:
        target = max(1, min(max_results, self.capabilities.user_search_page_size))
        rows = self._run("search", query, "--product", "top", "--limit", str(target * 2))
        handles = list(
            dict.fromkeys(str(item.get("author") or "").lstrip("@") for item in rows)
        )
        # Profile hydration is one Browser Bridge command per handle. Bound that
        # work before issuing requests; slicing only after hydration made a
        # 20-result search perform as many as 40 slow profile calls.
        selected = [handle for handle in handles if handle][:target]
        return self.get_users_by_usernames(selected)

    def get_user_by_username(self, username: str) -> XAccount | None:
        handle = username.lstrip("@")
        rows = self._run("profile", handle)
        if not rows:
            return None
        account = self._account(rows[0])
        if not account.username:
            account.username = handle
            account.id = self._id(handle)
        return account

    def get_users_by_usernames(self, usernames: list[str]) -> list[XAccount]:
        output: list[XAccount] = []
        for username in dict.fromkeys(value.lstrip("@") for value in usernames if value):
            try:
                account = self.get_user_by_username(username)
            except TwitterBackendError as exc:
                warning = f"OpenCLI profile @{username}: {exc}"
                if warning not in self.warnings:
                    self.warnings.append(warning)
                continue
            if account:
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
        handle = (username or user_id.removeprefix("opencli:")).lstrip("@")
        rows = self._run("tweets", handle, "--limit", str(max_results))
        posts = [self._post(item) for item in rows]
        for post in posts:
            post.author_id = self._id(handle)
            post.author_username = post.author_username or handle
        return posts[:max_results]

    def get_followings(self, username: str, max_results: int = 20) -> list[XAccount]:
        rows = self._run("following", username.lstrip("@"), "--limit", str(max_results))
        return [self._account(item) for item in rows][:max_results]

    def get_verified_followers(
        self,
        user_id: str,
        max_results: int = 20,
        *,
        username: str | None = None,
    ) -> list[XAccount]:
        return []

    def get_trends(self, max_results: int = 20) -> list[XTrend]:
        rows = self._run("trending", "--limit", str(max_results))
        output: list[XTrend] = []
        for rank, item in enumerate(rows, 1):
            name = str(
                item.get("name")
                or item.get("trend")
                or item.get("topic")
                or item.get("query")
                or ""
            ).strip()
            if not name:
                continue
            output.append(
                XTrend(
                    name=name,
                    rank=_as_int(item.get("rank")) or rank,
                    post_count=_as_int(
                        item.get("post_count")
                        or item.get("tweet_count")
                        or item.get("volume")
                    ),
                    url=item.get("url"),
                    raw=item,
                )
            )
        return output[:max_results]

    def close(self) -> None:
        return None
