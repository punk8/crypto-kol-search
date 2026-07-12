from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
from collections.abc import Callable
from typing import Any

from kol_search.models import Account, BackendCapabilities, Post
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


class OpenCliTwitterClient:
    """Read-only Twitter backend implemented through an authenticated OpenCLI profile."""

    name = "opencli"
    capabilities = BackendCapabilities(
        user_search=True,
        post_search=True,
        batch_user_lookup=True,
        user_timeline=True,
        followings=True,
        verified_followers=False,
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
                hint=f"Verify `opencli --profile {self.profile} twitter whoami`.",
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

    def _account(self, item: dict[str, Any]) -> Account:
        username = str(item.get("screen_name") or item.get("author") or "").lstrip("@")
        return Account(
            id=self._id(username),
            username=username,
            name=item.get("name"),
            description=item.get("bio"),
            followers_count=_as_int(item.get("followers")),
            following_count=_as_int(item.get("following")),
            tweet_count=_as_int(item.get("tweets")),
            verified=bool(item.get("verified", False)),
            created_at=item.get("created_at"),
            url=item.get("url") or None,
            location=item.get("location") or None,
            raw={"source_backend": self.name, "opencli": item},
        )

    def _post(self, item: dict[str, Any]) -> Post:
        username = str(item.get("author") or "").lstrip("@") or None
        text = str(item.get("text") or "")
        return Post(
            id=str(item.get("id") or ""),
            author_id=self._id(username) if username else "",
            author_username=username,
            text=text,
            created_at=item.get("created_at"),
            like_count=_as_int(item.get("likes")),
            retweet_count=_as_int(item.get("retweets")),
            reply_count=_as_int(item.get("replies")),
            quote_count=_as_int(item.get("quotes")),
            mentioned_usernames=_mentions(text),
            raw={"source_backend": self.name, "opencli": item},
        )

    def search_tweets(
        self, query: str, max_results: int = 40, since_id: str | None = None
    ) -> list[Post]:
        effective = query
        if since_id:
            # OpenCLI/X search does not support since_id directly; keep the API surface compatible.
            effective = f"{query} since_id:{since_id}"
        rows = self._run("search", effective, "--product", "live", "--limit", str(max_results))
        return [self._post(item) for item in rows][:max_results]

    def search_users(self, query: str, max_results: int = 100) -> list[Account]:
        target = max(1, min(max_results, self.capabilities.user_search_page_size))
        rows = self._run("search", query, "--product", "top", "--limit", str(target * 2))
        handles = list(
            dict.fromkeys(str(item.get("author") or "").lstrip("@") for item in rows)
        )
        return self.get_users_by_usernames([handle for handle in handles if handle])[:target]

    def get_user_by_username(self, username: str) -> Account | None:
        rows = self._run("profile", username.lstrip("@"))
        return self._account(rows[0]) if rows else None

    def get_users_by_usernames(self, usernames: list[str]) -> list[Account]:
        output: list[Account] = []
        for username in dict.fromkeys(value.lstrip("@") for value in usernames if value):
            try:
                account = self.get_user_by_username(username)
            except TwitterBackendError as exc:
                if "not found" in str(exc).lower() or "empty" in str(exc).lower():
                    continue
                raise
            if account:
                output.append(account)
        return output

    def get_user_tweets(
        self,
        user_id: str,
        max_results: int = 10,
        *,
        username: str | None = None,
    ) -> list[Post]:
        handle = (username or user_id.removeprefix("opencli:")).lstrip("@")
        rows = self._run("tweets", handle, "--limit", str(max_results))
        posts = [self._post(item) for item in rows]
        for post in posts:
            post.author_id = self._id(handle)
            post.author_username = post.author_username or handle
        return posts[:max_results]

    def get_followings(self, username: str, max_results: int = 20) -> list[Account]:
        rows = self._run("following", username.lstrip("@"), "--limit", str(max_results))
        return [self._account(item) for item in rows][:max_results]

    def get_verified_followers(
        self,
        user_id: str,
        max_results: int = 20,
        *,
        username: str | None = None,
    ) -> list[Account]:
        return []

    def close(self) -> None:
        return None
