from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from kol_search.contacts import UnsafeURLError, assert_public_url
from kol_search.db import Store
from kol_search.postiz import PostizError, PostizPublisher, PostizRequestUncertain
from kol_search.settings import Settings


class PublishingError(RuntimeError):
    pass


ALLOWED_IMAGE_TYPES = {
    "image/jpeg",
    "image/png",
    "image/gif",
    "image/webp",
}
MAX_IMAGE_BYTES = 10 * 1024 * 1024


def local_schedule_to_utc(value: str, timezone_name: str = "Asia/Shanghai") -> str:
    if not value:
        raise ValueError("定时发布必须选择发布时间")
    local = datetime.fromisoformat(value)
    if local.tzinfo is None:
        local = local.replace(tzinfo=ZoneInfo(timezone_name))
    return local.astimezone(timezone.utc).isoformat()


def owned_post_idempotency_key(post: dict[str, Any]) -> str:
    snapshot = {
        "integration": post["integration_id"],
        "mode": post["mode"],
        "scheduled_at_utc": post.get("scheduled_at_utc") or "now",
        "who_can_reply": post["who_can_reply"],
        "made_with_ai": bool(post["made_with_ai"]),
        "items": [item["content"] for item in post["items"]],
        "media": [
            {
                "item_position": item["item_position"],
                "source_type": item["source_type"],
                "source_value": item["source_value"],
            }
            for item in post["media"]
        ],
    }
    material = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class OwnedPublishingService:
    def __init__(
        self,
        store: Store,
        settings: Settings,
        publisher: PostizPublisher | None = None,
    ) -> None:
        self.store = store
        self.settings = settings
        self.publisher = publisher

    def _client(self) -> PostizPublisher:
        if self.publisher is not None:
            return self.publisher
        if not self.settings.postiz_api_key:
            raise PublishingError("缺少 POSTIZ_API_KEY")
        self.publisher = PostizPublisher(
            api_key=self.settings.postiz_api_key,
            base_url=self.settings.postiz_api_url,
        )
        return self.publisher

    def close(self) -> None:
        if self.publisher is not None:
            self.publisher.close()

    def sync_integrations(self, *, force: bool = False, actor: str = "admin") -> list[dict[str, Any]]:
        cached = self.store.list_postiz_integrations()
        if cached and not force:
            captured = max(
                (datetime.fromisoformat(row["captured_at"]) for row in cached),
                default=datetime.min.replace(tzinfo=timezone.utc),
            )
            age = (datetime.now(timezone.utc) - captured.astimezone(timezone.utc)).total_seconds()
            if age < max(60, self.settings.postiz_integration_cache_minutes * 60):
                return cached
        try:
            payload = self._client().list_integrations()
        except PostizError as exc:
            raise PublishingError(str(exc)) from exc
        self.store.sync_postiz_integrations(payload, actor=actor)
        return self.store.list_postiz_integrations()

    def approve(self, post_id: int, *, actor: str) -> None:
        post = self.store.get_owned_post(post_id)
        if not post:
            raise KeyError(post_id)
        if post["mode"] == "schedule":
            if not post.get("scheduled_at_utc"):
                raise PublishingError("定时发布缺少 UTC 时间")
            if datetime.fromisoformat(post["scheduled_at_utc"]) <= datetime.now(timezone.utc):
                raise PublishingError("定时发布时间已经过去，请先修改时间")
        self.store.approve_owned_post(
            post_id,
            idempotency_key=owned_post_idempotency_key(post),
            actor=actor,
        )

    def submit(self, post_id: int, *, actor: str) -> dict[str, Any]:
        post = self.store.get_owned_post(post_id)
        if not post:
            raise KeyError(post_id)
        if post["status"] != "approved":
            raise PublishingError("帖子尚未逐条审批")
        if post["integration_disabled"]:
            raise PublishingError("所选 Postiz X integration 已禁用")
        if post["mode"] == "schedule":
            if not post.get("scheduled_at_utc"):
                raise PublishingError("定时发布缺少 UTC 时间")
            if datetime.fromisoformat(post["scheduled_at_utc"]) <= datetime.now(timezone.utc):
                self.store.finish_owned_post(
                    post_id,
                    status="failed",
                    error="定时发布时间已经过去，请先编辑草稿并重新审批",
                    actor=actor,
                )
                raise PublishingError("定时发布时间已经过去，请先编辑草稿并重新审批")
        post = self.store.claim_owned_post(post_id)
        try:
            assets = self._upload_media(post)
        except (PostizError, PublishingError, OSError, UnsafeURLError, ValueError) as exc:
            self.store.finish_owned_post(post_id, status="failed", error=str(exc), actor=actor)
            raise PublishingError(str(exc)) from exc
        values: list[dict[str, Any]] = []
        for item in post["items"]:
            values.append(
                {
                    "content": item["content"],
                    "image": assets.get(int(item["position"]), []),
                }
            )
        publish_at = (
            datetime.fromisoformat(post["scheduled_at_utc"])
            if post["mode"] == "schedule" and post.get("scheduled_at_utc")
            else None
        )
        try:
            receipt = self._client().create_owned_post(
                integration_id=post["integration_id"],
                platform="x",
                items=values,
                mode=post["mode"],
                publish_at=publish_at,
                settings={
                    "who_can_reply_post": post["who_can_reply"],
                    "made_with_ai": bool(post["made_with_ai"]),
                },
            )
        except PostizRequestUncertain as exc:
            self.store.finish_owned_post(
                post_id, status="confirmation_required", error=str(exc), actor=actor
            )
            raise PublishingError(str(exc)) from exc
        except PublishingError as exc:
            self.store.finish_owned_post(post_id, status="failed", error=str(exc), actor=actor)
            raise
        except ValueError as exc:
            self.store.finish_owned_post(post_id, status="failed", error=str(exc), actor=actor)
            raise PublishingError(str(exc)) from exc
        except PostizError as exc:
            self.store.finish_owned_post(post_id, status="failed", error=str(exc), actor=actor)
            raise PublishingError(str(exc)) from exc
        row = receipt[0] if isinstance(receipt, list) and receipt else receipt
        postiz_post_id = str(row.get("postId") or row.get("id") or "") if isinstance(row, dict) else ""
        if not postiz_post_id:
            self.store.finish_owned_post(
                post_id,
                status="confirmation_required",
                receipt=receipt,
                error="Postiz 创建响应缺少 postId，禁止自动重试",
                actor=actor,
            )
            raise PublishingError("Postiz 创建响应缺少 postId，需人工确认")
        if post["mode"] == "schedule":
            self.store.finish_owned_post(
                post_id,
                status="scheduled",
                postiz_post_id=postiz_post_id,
                receipt=receipt,
                actor=actor,
            )
            return self.store.get_owned_post(post_id) or post
        self.store.finish_owned_post(
            post_id,
            status="confirmation_required",
            postiz_post_id=postiz_post_id,
            receipt=receipt,
            error="Postiz 已接收立即发布任务，等待 releaseURL 回读",
            actor=actor,
        )
        return self.reconcile(post_id, actor=actor)

    def reconcile(self, post_id: int, *, actor: str = "system") -> dict[str, Any]:
        post = self.store.get_owned_post(post_id)
        if not post:
            raise KeyError(post_id)
        around = datetime.fromisoformat(
            post.get("scheduled_at_utc") or post.get("submitted_at") or post["created_at"]
        )
        try:
            rows = self._client().recent_posts(around=around, hours=24)
        except (PostizError, PublishingError) as exc:
            raise PublishingError(str(exc)) from exc
        wanted_content = str(post["items"][0]["content"]).strip()
        matched = next(
            (
                row for row in rows
                if str(row.get("id") or "") == str(post.get("postiz_post_id") or "")
                or (
                    str((row.get("integration") or {}).get("id") or "") == post["integration_id"]
                    and str(row.get("content") or "").strip() == wanted_content
                )
            ),
            None,
        )
        if not matched:
            return post
        release_url = str(matched.get("releaseURL") or matched.get("releaseUrl") or "").strip()
        if not release_url:
            return post
        self.store.finish_owned_post(
            post_id,
            status="published",
            postiz_post_id=str(matched.get("id") or post.get("postiz_post_id") or ""),
            release_url=release_url,
            receipt=matched,
            actor=actor,
        )
        if post.get("source_cluster_id"):
            self.store.update_topic_cluster(
                int(post["source_cluster_id"]), status="published", published_url=release_url
            )
        return self.store.get_owned_post(post_id) or post

    def pause(self, post_id: int, *, actor: str) -> None:
        post = self._require_remote(post_id, {"scheduled"})
        try:
            receipt = self._client().change_post_status(post["postiz_post_id"], "draft")
        except (PostizError, PublishingError) as exc:
            raise PublishingError(str(exc)) from exc
        self.store.finish_owned_post(post_id, status="paused", receipt=receipt, actor=actor)

    def resume(self, post_id: int, *, actor: str) -> None:
        post = self._require_remote(post_id, {"paused"})
        try:
            receipt = self._client().change_post_status(post["postiz_post_id"], "schedule")
        except (PostizError, PublishingError) as exc:
            raise PublishingError(str(exc)) from exc
        self.store.finish_owned_post(post_id, status="scheduled", receipt=receipt, actor=actor)

    def delete(self, post_id: int, *, actor: str) -> None:
        post = self._require_remote(post_id, {"scheduled", "paused"})
        try:
            receipt = self._client().delete_post(post["postiz_post_id"])
        except (PostizError, PublishingError) as exc:
            if "(404)" not in str(exc):
                raise PublishingError(str(exc)) from exc
            receipt = {"already_deleted": True}
        self.store.finish_owned_post(post_id, status="cancelled", receipt=receipt, actor=actor)

    def _require_remote(self, post_id: int, states: set[str]) -> dict[str, Any]:
        post = self.store.get_owned_post(post_id)
        if not post:
            raise KeyError(post_id)
        if post["status"] not in states or not post.get("postiz_post_id"):
            raise PublishingError("当前状态不允许此操作")
        return post

    def _upload_media(self, post: dict[str, Any]) -> dict[int, list[dict[str, str]]]:
        output: dict[int, list[dict[str, str]]] = {}
        media_root = self.settings.publishing_media_path().resolve()
        for row in post["media"]:
            try:
                if row.get("postiz_asset_id") and row.get("postiz_asset_path"):
                    payload = {"id": row["postiz_asset_id"], "path": row["postiz_asset_path"]}
                elif row["source_type"] == "url":
                    safe_url = assert_public_url(str(row["source_value"]))
                    if urlparse(safe_url).scheme != "https":
                        raise UnsafeURLError("图片 URL 必须使用 HTTPS")
                    payload = self._client().upload_from_url(safe_url)
                else:
                    path = Path(str(row.get("storage_path") or "")).resolve()
                    if media_root not in path.parents:
                        raise PublishingError("本地媒体路径越界")
                    content = path.read_bytes()
                    content_type = str(row.get("content_type") or "")
                    if content_type not in ALLOWED_IMAGE_TYPES:
                        raise PublishingError(f"不支持的图片类型：{content_type or '未知'}")
                    if len(content) > MAX_IMAGE_BYTES:
                        raise PublishingError("单张图片不能超过 10MB")
                    payload = self._client().upload_file(
                        filename=str(row.get("filename") or path.name),
                        content=content,
                        content_type=content_type,
                    )
                self.store.update_owned_media_result(
                    int(row["id"]),
                    status="uploaded",
                    asset_id=str(payload["id"]),
                    asset_path=str(payload["path"]),
                )
                output.setdefault(int(row["item_position"]), []).append(
                    {"id": str(payload["id"]), "path": str(payload["path"])}
                )
            except Exception as exc:
                self.store.update_owned_media_result(
                    int(row["id"]), status="failed", error=str(exc)
                )
                raise
        return output
