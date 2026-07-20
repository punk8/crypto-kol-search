from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import threading
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from kol_search.models import Account, Post, TrendSignal
from kol_search.platforms.base import PlatformHealth
from kol_search.twitter.base import TwitterBackendError
from kol_search.twitter.opencli import OpenCliTwitterClient


def _as_int(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value or "").strip().lower().replace(",", "")
    multiplier = 1
    for suffix, factor in (("万", 10_000), ("w", 10_000), ("k", 1_000)):
        if text.endswith(suffix):
            multiplier = factor
            text = text[: -len(suffix)]
            break
    try:
        return int(float(text) * multiplier)
    except ValueError:
        return 0


def _timestamp(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    now = datetime.now(timezone.utc)
    if text.endswith("分钟前"):
        try:
            from datetime import timedelta

            return (now - timedelta(minutes=int(text.removesuffix("分钟前")))).isoformat()
        except ValueError:
            return text
    for candidate in (text, text.replace("Z", "+00:00")):
        try:
            parsed = datetime.fromisoformat(candidate)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.isoformat()
        except ValueError:
            continue
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%m-%d"):
        try:
            parsed = datetime.strptime(text, fmt)
            if fmt == "%m-%d":
                parsed = parsed.replace(year=now.year)
            return parsed.replace(tzinfo=timezone.utc).isoformat()
        except ValueError:
            continue
    return text


def _stable_token(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:20]


def _note_id(item: dict[str, Any]) -> str:
    direct = item.get("note_id") or item.get("noteId") or item.get("id")
    if direct:
        return str(direct)
    url = str(item.get("url") or item.get("note_url") or "")
    match = re.search(r"/(?:explore|discovery/item)/([A-Za-z0-9]+)", url)
    return match.group(1) if match else _stable_token(url or json.dumps(item, sort_keys=True))


class OpenCliXReader:
    platform = "x"
    provider = "opencli"

    def __init__(self, client: OpenCliTwitterClient) -> None:
        self.client = client

    def _normalize_account(self, account: Account) -> Account:
        account.platform = "x"
        account.external_id = account.external_id or account.id
        account.source_provider = self.provider
        account.captured_at = account.captured_at or datetime.now(timezone.utc).isoformat()
        account.url = account.url or f"https://x.com/{account.handle}"
        return account

    def _normalize_post(self, post: Post) -> Post:
        post.platform = "x"
        post.external_id = post.external_id or post.id
        post.source_provider = self.provider
        post.captured_at = post.captured_at or datetime.now(timezone.utc).isoformat()
        return post

    def search_posts(self, query: str, limit: int = 20) -> list[Post]:
        return [self._normalize_post(post) for post in self.client.search_tweets(query, limit)]

    def get_account(self, external_id: str) -> Account | None:
        account = self.client.get_user_by_username(external_id.removeprefix("opencli:"))
        return self._normalize_account(account) if account else None

    def get_account_posts(self, external_id: str, limit: int = 20) -> list[Post]:
        handle = external_id.removeprefix("opencli:")
        return [
            self._normalize_post(post)
            for post in self.client.get_user_tweets(external_id, limit, username=handle)
        ]

    def get_trends(self, limit: int = 20) -> list[TrendSignal]:
        output = self.client.get_trends(limit)
        for trend in output:
            trend.platform = "x"
        return output

    def get_comments(self, post_url: str, limit: int = 20) -> list[dict]:
        match = re.search(r"/status/(\d+)", post_url)
        if not match:
            return []
        return [post.model_dump() for post in self.search_posts(f"conversation_id:{match.group(1)}", limit)]

    def health_check(self) -> PlatformHealth:
        if not hasattr(self.client, "_run"):
            return PlatformHealth(
                platform="x",
                ready=True,
                detail=f"{getattr(self.client, 'name', 'x')} backend configured",
            )
        try:
            rows = self.client._run("whoami")  # type: ignore[attr-defined]
            row = rows[0] if rows else {}
            account = str(row.get("screen_name") or row.get("username") or "").lstrip("@")
            return PlatformHealth(platform="x", ready=bool(account), account=account or None)
        except Exception as exc:
            return PlatformHealth(platform="x", ready=False, detail=str(exc))

    def close(self) -> None:
        self.client.close()


class OpenCliXiaohongshuReader:
    platform = "xiaohongshu"
    provider = "opencli"
    _lock = threading.Lock()

    def __init__(
        self,
        *,
        command: str = "opencli",
        profile: str,
        timeout: float = 90.0,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self.command = command
        self.profile = profile
        self.timeout = timeout
        self._runner = runner

    def _run(self, *args: str) -> list[dict[str, Any]]:
        if not shutil.which(self.command) and "/" not in self.command:
            raise TwitterBackendError(f"OpenCLI command not found: {self.command}")
        command = [
            self.command,
            "--profile",
            self.profile,
            "xiaohongshu",
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
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise TwitterBackendError(f"小红书 OpenCLI 启动失败：{exc}") from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "unknown error").strip()[-800:]
            raise TwitterBackendError(
                f"小红书 OpenCLI 调用失败：{detail}",
                hint=f"确认 Browser Bridge profile {self.profile!r} 已连接并登录小红书。",
            )
        try:
            payload = json.loads(result.stdout or "[]")
        except json.JSONDecodeError as exc:
            raise TwitterBackendError("小红书 OpenCLI 返回了无效 JSON") from exc
        if isinstance(payload, dict):
            if payload.get("ok") is False or payload.get("error"):
                raise TwitterBackendError(str(payload.get("error") or payload))
            payload = payload.get("items") or payload.get("data") or [payload]
        if not isinstance(payload, list):
            raise TwitterBackendError("小红书 OpenCLI 返回结构不符合预期")
        return [row for row in payload if isinstance(row, dict)]

    def _account(self, item: dict[str, Any]) -> Account:
        user = item.get("user") if isinstance(item.get("user"), dict) else {}
        author = str(
            item.get("author")
            or item.get("nickname")
            or item.get("username")
            or user.get("nickname")
            or user.get("name")
            or "未知作者"
        ).strip()
        external = str(
            item.get("userId")
            or item.get("user_id")
            or item.get("author_id")
            or user.get("userId")
            or user.get("id")
            or _stable_token(author)
        )
        profile_url = item.get("profileUrl") or item.get("profile_url") or user.get("url")
        return Account(
            id=f"xiaohongshu:{external}",
            platform="xiaohongshu",
            external_id=external,
            source_provider=self.provider,
            captured_at=datetime.now(timezone.utc).isoformat(),
            username=author,
            name=author,
            description=item.get("bio") or user.get("desc"),
            followers_count=_as_int(item.get("followers") or user.get("followers")),
            following_count=_as_int(item.get("following") or user.get("following")),
            verified=bool(item.get("verified") or user.get("verified")),
            profile_image_url=item.get("avatar") or user.get("avatar"),
            url=profile_url or (f"https://www.xiaohongshu.com/user/profile/{external}" if external else None),
            raw={"source_backend": self.provider, "opencli": item},
        )

    def _post(self, item: dict[str, Any]) -> Post:
        account = self._account(item)
        external = _note_id(item)
        title = str(item.get("title") or "").strip()
        body = str(item.get("text") or item.get("content") or item.get("desc") or "").strip()
        text = "\n".join(value for value in (title, body) if value)
        url = str(item.get("url") or item.get("note_url") or "") or None
        return Post(
            id=f"xiaohongshu:{external}",
            platform="xiaohongshu",
            external_id=external,
            source_provider=self.provider,
            captured_at=datetime.now(timezone.utc).isoformat(),
            author_id=account.id,
            author_username=account.username,
            text=text,
            created_at=_timestamp(
                item.get("published_at") or item.get("time") or item.get("created_at")
            ),
            like_count=_as_int(item.get("likes") or item.get("liked_count")),
            reply_count=_as_int(item.get("comments") or item.get("comment_count")),
            bookmark_count=_as_int(item.get("collects") or item.get("collected_count")),
            view_count=_as_int(item.get("views") or item.get("view_count")),
            lang="zh",
            url=url,
            conversation_id=external,
            raw={"source_backend": self.provider, "opencli": item},
        )

    def search_posts(self, query: str, limit: int = 20) -> list[Post]:
        rows = self._run("search", query, "--limit", str(max(1, min(limit, 100))))
        return [self._post(row) for row in rows][:limit]

    def get_account(self, external_id: str) -> Account | None:
        target = external_id.removeprefix("xiaohongshu:")
        rows = self._run("user", target, "--limit", "1")
        if not rows:
            return None
        account = self._account(rows[0])
        if not account.external_id or account.external_id == _stable_token(account.username):
            account.external_id = target
            account.id = f"xiaohongshu:{target}"
        return account

    def get_account_posts(self, external_id: str, limit: int = 20) -> list[Post]:
        target = external_id.removeprefix("xiaohongshu:")
        rows = self._run("user", target, "--limit", str(max(1, min(limit, 100))))
        posts = [self._post(row) for row in rows]
        for post in posts:
            if post.author_id.startswith("xiaohongshu:"):
                continue
            post.author_id = f"xiaohongshu:{target}"
        return posts[:limit]

    def get_trends(self, limit: int = 20) -> list[TrendSignal]:
        rows = self._run("feed", "--limit", str(max(1, min(limit, 100))))
        output: list[TrendSignal] = []
        for rank, row in enumerate(rows, 1):
            post = self._post(row)
            name = (post.text.splitlines() or [""])[0].strip()
            if not name:
                continue
            output.append(
                TrendSignal(
                    name=name,
                    platform="xiaohongshu",
                    rank=rank,
                    post_count=1,
                    url=post.url,
                    raw={"post": post.model_dump(), "opencli": row},
                )
            )
        return output[:limit]

    def get_comments(self, post_url: str, limit: int = 20) -> list[dict]:
        parsed = urlparse(post_url)
        if parsed.netloc not in {"www.xiaohongshu.com", "xiaohongshu.com"}:
            raise ValueError("小红书评论读取仅接受 xiaohongshu.com 链接")
        return self._run("comments", post_url, "--limit", str(max(1, min(limit, 50))))

    def health_check(self) -> PlatformHealth:
        try:
            rows = self._run("whoami")
            row = rows[0] if rows else {}
            account = str(
                row.get("username")
                or row.get("nickname")
                or row.get("name")
                or row.get("userId")
                or ""
            )
            return PlatformHealth(
                platform="xiaohongshu",
                ready=bool(rows),
                account=account or None,
                detail=None if rows else "未检测到登录账号",
            )
        except Exception as exc:
            return PlatformHealth(platform="xiaohongshu", ready=False, detail=str(exc))

    def close(self) -> None:
        return None
