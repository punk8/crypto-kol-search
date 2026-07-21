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

from kol_search.platforms.kernel import PlatformHealthResult


class XiaohongshuReadError(RuntimeError):
    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.hint = hint

    def __str__(self) -> str:
        message = super().__str__()
        return f"{message} Hint: {self.hint}" if self.hint else message


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


def _profile_user_id(value: object) -> str:
    url = str(value or "").strip()
    if not url:
        return ""
    parsed = urlparse(url)
    if parsed.netloc not in {"www.xiaohongshu.com", "xiaohongshu.com"}:
        return ""
    match = re.fullmatch(r"/user/profile/([A-Za-z0-9_-]+)/*", parsed.path)
    return match.group(1) if match else ""


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
            raise XiaohongshuReadError(f"OpenCLI command not found: {self.command}")
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
            raise XiaohongshuReadError(f"小红书 OpenCLI 启动失败：{exc}") from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "unknown error").strip()[-800:]
            raise XiaohongshuReadError(
                f"小红书 OpenCLI 调用失败：{detail}",
                hint=f"确认 Browser Bridge profile {self.profile!r} 已连接并登录小红书。",
            )
        try:
            payload = json.loads(result.stdout or "[]")
        except json.JSONDecodeError as exc:
            raise XiaohongshuReadError("小红书 OpenCLI 返回了无效 JSON") from exc
        if isinstance(payload, dict):
            if payload.get("ok") is False or payload.get("error"):
                raise XiaohongshuReadError(str(payload.get("error") or payload))
            payload = payload.get("items") or payload.get("data") or [payload]
        if not isinstance(payload, list):
            raise XiaohongshuReadError("小红书 OpenCLI 返回结构不符合预期")
        return [row for row in payload if isinstance(row, dict)]

    def _account(self, item: dict[str, Any]) -> dict[str, Any]:
        user = item.get("user") if isinstance(item.get("user"), dict) else {}
        author = str(
            item.get("author")
            or item.get("nickname")
            or item.get("username")
            or user.get("nickname")
            or user.get("name")
            or "未知作者"
        ).strip()
        profile_url = (
            item.get("profileUrl")
            or item.get("profile_url")
            or item.get("authorUrl")
            or item.get("author_url")
            or user.get("url")
        )
        native_external = str(
            item.get("userId")
            or item.get("user_id")
            or item.get("author_id")
            or user.get("userId")
            or user.get("id")
            or _profile_user_id(profile_url)
            or ""
        ).strip()
        native_id_resolved = bool(native_external)
        external = native_external or f"unresolved:{_stable_token(author)}"
        return {
            "id": f"xiaohongshu:{external}",
            "external_id": external,
            "native_id_resolved": native_id_resolved,
            "source_provider": self.provider,
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "username": author,
            "name": author,
            "description": item.get("bio") or user.get("desc"),
            "followers_count": _as_int(item.get("followers") or user.get("followers")),
            "following_count": _as_int(item.get("following") or user.get("following")),
            "verified": bool(item.get("verified") or user.get("verified")),
            "profile_image_url": item.get("avatar") or user.get("avatar"),
            "url": profile_url
            or (
                f"https://www.xiaohongshu.com/user/profile/{external}"
                if native_id_resolved
                else None
            ),
            "raw": {"source_backend": self.provider, "opencli": item},
        }

    def _post(self, item: dict[str, Any]) -> dict[str, Any]:
        account = self._account(item)
        external = _note_id(item)
        title = str(item.get("title") or "").strip()
        body = str(item.get("text") or item.get("content") or item.get("desc") or "").strip()
        text = "\n".join(value for value in (title, body) if value)
        url = str(item.get("url") or item.get("note_url") or "") or None
        return {
            "id": f"xiaohongshu:{external}",
            "external_id": external,
            "source_provider": self.provider,
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "author_id": account["id"],
            "author_native_id_resolved": account["native_id_resolved"],
            "author_username": account["username"],
            "text": text,
            "created_at": _timestamp(
                item.get("published_at") or item.get("time") or item.get("created_at")
            ),
            "like_count": _as_int(item.get("likes") or item.get("liked_count")),
            "reply_count": _as_int(item.get("comments") or item.get("comment_count")),
            "bookmark_count": _as_int(
                item.get("collects") or item.get("collected_count")
            ),
            "view_count": _as_int(item.get("views") or item.get("view_count")),
            "url": url,
            "raw": {"source_backend": self.provider, "opencli": item},
        }

    def search_posts(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        rows = self._run("search", query, "--limit", str(max(1, min(limit, 100))))
        return [self._post(row) for row in rows][:limit]

    def get_account(self, external_id: str) -> dict[str, Any] | None:
        target = external_id.removeprefix("xiaohongshu:")
        rows = self._run("user", target, "--limit", "1")
        if not rows:
            return None
        account = self._account(rows[0])
        if not account["native_id_resolved"]:
            account["external_id"] = target
            account["id"] = f"xiaohongshu:{target}"
            account["native_id_resolved"] = True
            account["url"] = (
                account.get("url")
                or f"https://www.xiaohongshu.com/user/profile/{target}"
            )
        return account

    def get_account_posts(
        self, external_id: str, limit: int = 20
    ) -> list[dict[str, Any]]:
        target = external_id.removeprefix("xiaohongshu:")
        rows = self._run("user", target, "--limit", str(max(1, min(limit, 100))))
        posts = [self._post(row) for row in rows]
        for post in posts:
            if post["author_native_id_resolved"]:
                continue
            post["author_id"] = f"xiaohongshu:{target}"
            post["author_native_id_resolved"] = True
        return posts[:limit]

    def get_trends(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self._run("feed", "--limit", str(max(1, min(limit, 100))))
        output: list[dict[str, Any]] = []
        for rank, row in enumerate(rows, 1):
            post = self._post(row)
            name = (str(post["text"]).splitlines() or [""])[0].strip()
            if not name:
                continue
            output.append(
                {
                    "name": name,
                    "rank": rank,
                    "post_count": 1,
                    "url": post["url"],
                    "raw": {"post": post, "opencli": row},
                }
            )
        return output[:limit]

    def get_comments(self, post_url: str, limit: int = 20) -> list[dict]:
        parsed = urlparse(post_url)
        if parsed.netloc not in {"www.xiaohongshu.com", "xiaohongshu.com"}:
            raise ValueError("小红书评论读取仅接受 xiaohongshu.com 链接")
        return self._run("comments", post_url, "--limit", str(max(1, min(limit, 50))))

    def health_check(self) -> PlatformHealthResult:
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
            return PlatformHealthResult(
                platform_id="xiaohongshu",
                ready=bool(rows),
                account=account or None,
                detail=None if rows else "未检测到登录账号",
            )
        except Exception as exc:
            return PlatformHealthResult(
                platform_id="xiaohongshu", ready=False, detail=str(exc)
            )

    def close(self) -> None:
        return None
