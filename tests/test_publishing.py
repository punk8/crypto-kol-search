from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from kol_search.db import Store
from kol_search.postiz import PostizRequestUncertain
from kol_search.publishing import (
    OwnedPublishingService,
    PublishingError,
    local_schedule_to_utc,
    owned_post_idempotency_key,
)
from kol_search.settings import Settings


class FakePostiz:
    def __init__(self) -> None:
        self.created: list[dict] = []
        self.posts: list[dict] = []
        self.status_changes: list[tuple[str, str]] = []
        self.deleted: list[str] = []
        self.uploaded: list[tuple[str, bytes, str]] = []

    def list_integrations(self):
        return [
            {"id": "x1", "identifier": "x", "name": "Brand", "profile": "brand"},
            {"id": "linkedin1", "identifier": "linkedin", "name": "Ignore"},
        ]

    def create_owned_post(self, **kwargs):
        self.created.append(kwargs)
        return [{"postId": "postiz-1"}]

    def upload_file(self, *, filename, content, content_type):
        self.uploaded.append((filename, content, content_type))
        return {"id": "asset-1", "path": "https://uploads.postiz.com/image.png"}

    def recent_posts(self, **kwargs):
        return self.posts

    def change_post_status(self, post_id, status):
        self.status_changes.append((post_id, status))
        return {"id": post_id, "status": status}

    def delete_post(self, post_id):
        self.deleted.append(post_id)
        return {"id": post_id, "deleted": True}

    def close(self):
        return None


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        KOL_DB_PATH=str(tmp_path / "unused.db"),
        KOL_PUBLISHING_MEDIA_DIR=str(tmp_path / "media"),
        POSTIZ_API_KEY="test-only",
    )


def _draft(store: Store, *, mode: str = "now", scheduled: str | None = None) -> int:
    return store.create_owned_post(
        integration_id="x1",
        items=["First segment", "Second segment"],
        mode=mode,
        scheduled_at_local="2026-07-21T10:00" if scheduled else None,
        scheduled_at_utc=scheduled,
        made_with_ai=True,
        who_can_reply="following",
    )


def test_timezone_integration_sync_default_and_idempotency(tmp_path: Path):
    assert local_schedule_to_utc("2026-07-20T18:30") == "2026-07-20T10:30:00+00:00"
    store = Store(tmp_path / "publishing.db")
    fake = FakePostiz()
    service = OwnedPublishingService(store, _settings(tmp_path), publisher=fake)
    rows = service.sync_integrations(force=True)
    assert [row["external_id"] for row in rows] == ["x1"]
    assert rows[0]["is_default"] == 1
    post_id = _draft(store)
    key = owned_post_idempotency_key(store.get_owned_post(post_id))
    service.approve(post_id, actor="admin")
    assert store.get_owned_post(post_id)["idempotency_key"] == key
    with pytest.raises(ValueError, match="相同账号"):
        duplicate = _draft(store)
        service.approve(duplicate, actor="admin")
    store.close()


def test_immediate_publish_reconciles_release_url_and_thread_payload(tmp_path: Path):
    store = Store(tmp_path / "publish.db")
    fake = FakePostiz()
    service = OwnedPublishingService(store, _settings(tmp_path), publisher=fake)
    service.sync_integrations(force=True)
    post_id = _draft(store)
    service.approve(post_id, actor="admin")
    fake.posts = [
        {
            "id": "postiz-1",
            "content": "First segment",
            "integration": {"id": "x1"},
            "releaseURL": "https://x.com/brand/status/123",
        }
    ]
    result = service.submit(post_id, actor="admin")
    assert result["status"] == "published"
    assert result["release_url"] == "https://x.com/brand/status/123"
    assert [value["content"] for value in fake.created[0]["items"]] == [
        "First segment",
        "Second segment",
    ]
    assert fake.created[0]["settings"] == {
        "who_can_reply_post": "following",
        "made_with_ai": True,
    }
    actions = [row["action"] for row in store.list_audit_events()]
    assert "owned_post.approve" in actions
    assert "owned_post.published" in actions
    store.close()


def test_local_image_upload_is_deferred_until_after_approval(tmp_path: Path):
    store = Store(tmp_path / "media.db")
    fake = FakePostiz()
    settings = _settings(tmp_path)
    service = OwnedPublishingService(store, settings, publisher=fake)
    service.sync_integrations(force=True)
    media_root = settings.publishing_media_path()
    media_root.mkdir(parents=True)
    image = media_root / "image.png"
    image.write_bytes(b"test-png-content")
    post_id = store.create_owned_post(
        integration_id="x1",
        items=["Image post"],
        media=[
            {
                "item_position": 0,
                "source_type": "local",
                "source_value": "image.png",
                "filename": "image.png",
                "content_type": "image/png",
                "storage_path": str(image),
            }
        ],
    )
    assert fake.uploaded == []
    service.approve(post_id, actor="admin")
    service.submit(post_id, actor="admin")
    assert fake.uploaded == [("image.png", b"test-png-content", "image/png")]
    assert fake.created[0]["items"][0]["image"] == [
        {"id": "asset-1", "path": "https://uploads.postiz.com/image.png"}
    ]
    assert store.get_owned_post(post_id)["media"][0]["status"] == "uploaded"
    store.close()


def test_schedule_pause_resume_delete_and_uncertain_no_retry(tmp_path: Path):
    store = Store(tmp_path / "schedule.db")
    fake = FakePostiz()
    service = OwnedPublishingService(store, _settings(tmp_path), publisher=fake)
    service.sync_integrations(force=True)
    scheduled = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    post_id = _draft(store, mode="schedule", scheduled=scheduled)
    service.approve(post_id, actor="admin")
    assert service.submit(post_id, actor="admin")["status"] == "scheduled"
    service.pause(post_id, actor="admin")
    assert store.get_owned_post(post_id)["status"] == "paused"
    service.resume(post_id, actor="admin")
    assert store.get_owned_post(post_id)["status"] == "scheduled"
    service.delete(post_id, actor="admin")
    assert store.get_owned_post(post_id)["status"] == "cancelled"
    assert fake.status_changes == [("postiz-1", "draft"), ("postiz-1", "schedule")]
    assert fake.deleted == ["postiz-1"]

    uncertain_id = store.create_owned_post(integration_id="x1", items=["Uncertain"])
    service.approve(uncertain_id, actor="admin")

    def uncertain(**kwargs):
        raise PostizRequestUncertain("timeout")

    fake.create_owned_post = uncertain
    with pytest.raises(PublishingError, match="timeout"):
        service.submit(uncertain_id, actor="admin")
    assert store.get_owned_post(uncertain_id)["status"] == "confirmation_required"
    store.close()
