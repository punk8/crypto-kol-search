from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Literal

import httpx


class PostizError(RuntimeError):
    pass


class PostizRequestUncertain(PostizError):
    """The create request may have reached Postiz; callers must reconcile before retrying."""


class PostizPublisher:
    """Postiz Cloud client for approved, owned-account X publishing."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://api.postiz.com/public/v1",
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": api_key},
            timeout=timeout,
            transport=transport,
        )

    def _request(self, method: str, path: str, *, uncertain: bool = False, **kwargs: Any) -> Any:
        try:
            response = self.client.request(method, path, **kwargs)
        except httpx.TransportError as exc:
            error_type = PostizRequestUncertain if uncertain else PostizError
            raise error_type(f"Postiz {method} {path} 未返回明确结果：{exc}") from exc
        if response.status_code >= 500 and uncertain:
            raise PostizRequestUncertain(
                f"Postiz {method} {path} 返回 {response.status_code}，创建结果不确定"
            )
        if response.status_code >= 400:
            raise PostizError(
                f"Postiz {method} {path} failed ({response.status_code}): {response.text[:500]}"
            )
        try:
            return response.json()
        except ValueError as exc:
            raise PostizError("Postiz returned invalid JSON") from exc

    def list_integrations(self) -> list[dict[str, Any]]:
        payload = self._request("GET", "/integrations")
        if isinstance(payload, dict):
            payload = payload.get("integrations") or payload.get("data") or []
        return payload if isinstance(payload, list) else []

    def upload_file(self, *, filename: str, content: bytes, content_type: str) -> dict[str, Any]:
        payload = self._request(
            "POST",
            "/upload",
            files={"file": (filename, content, content_type)},
        )
        if not isinstance(payload, dict) or not payload.get("id") or not payload.get("path"):
            raise PostizError("Postiz media upload response is missing id/path")
        return payload

    def upload_from_url(self, url: str) -> dict[str, Any]:
        payload = self._request("POST", "/upload-from-url", json={"url": url})
        if not isinstance(payload, dict) or not payload.get("id") or not payload.get("path"):
            raise PostizError("Postiz URL upload response is missing id/path")
        return payload

    def create_owned_post(
        self,
        *,
        integration_id: str,
        platform: str,
        content: str | None = None,
        items: list[dict[str, Any]] | None = None,
        mode: Literal["draft", "schedule", "now"] = "draft",
        publish_at: datetime | None = None,
        media_urls: list[str] | None = None,
        settings: dict[str, Any] | None = None,
    ) -> Any:
        if platform == "xiaohongshu":
            raise PostizError("Postiz 当前不支持小红书；不能作为小红书写入通道")
        if platform != "x":
            raise PostizError("本项目首期仅启用 Postiz 的 X 自有内容发布")
        if mode == "schedule" and publish_at is None:
            raise ValueError("Scheduled Postiz posts require publish_at")
        values = items or [{"content": content or "", "image": media_urls or []}]
        if not values or any(not str(item.get("content") or "").strip() for item in values):
            raise ValueError("Every X post/thread item requires content")
        date = publish_at or datetime.now(timezone.utc)
        post_settings = {
            "__type": "x",
            "who_can_reply_post": "everyone",
            "community": "",
            "made_with_ai": False,
            "paid_partnership": False,
            **(settings or {}),
        }
        payload = {
            "type": mode,
            "date": date.astimezone(timezone.utc).isoformat(),
            "shortLink": False,
            "tags": [],
            "posts": [
                {
                    "integration": {"id": integration_id},
                    "value": values,
                    "settings": post_settings,
                }
            ],
        }
        return self._request("POST", "/posts", json=payload, uncertain=True)

    def list_posts(self, *, start: datetime, end: datetime) -> list[dict[str, Any]]:
        payload = self._request(
            "GET",
            "/posts",
            params={
                "startDate": start.astimezone(timezone.utc).isoformat(),
                "endDate": end.astimezone(timezone.utc).isoformat(),
            },
        )
        if isinstance(payload, dict):
            payload = payload.get("posts") or payload.get("data") or []
        return payload if isinstance(payload, list) else []

    def recent_posts(self, *, around: datetime, hours: int = 24) -> list[dict[str, Any]]:
        return self.list_posts(
            start=around - timedelta(hours=max(1, hours)),
            end=around + timedelta(hours=max(1, hours)),
        )

    def change_post_status(self, post_id: str, status: Literal["draft", "schedule"]) -> Any:
        return self._request("PUT", f"/posts/{post_id}/status", json={"status": status})

    def delete_post(self, post_id: str) -> Any:
        return self._request("DELETE", f"/posts/{post_id}")

    def post_analytics(self, post_id: str, days: int = 7) -> Any:
        return self._request(
            "GET", f"/analytics/post/{post_id}", params={"date": max(1, min(days, 365))}
        )

    def close(self) -> None:
        self.client.close()
