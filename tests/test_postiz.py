from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
import pytest

from kol_search.postiz import PostizError, PostizPublisher, PostizRequestUncertain


def test_postiz_is_owned_x_publish_only_and_rejects_xiaohongshu():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=[{"postId": "p1", "integration": "x1"}])

    publisher = PostizPublisher(api_key="secret", transport=httpx.MockTransport(handler))
    result = publisher.create_owned_post(
        integration_id="x1", platform="x", content="Owned content", mode="draft"
    )
    assert result[0]["postId"] == "p1"
    assert requests[0].url.path.endswith("/public/v1/posts")
    with pytest.raises(PostizError, match="不支持小红书"):
        publisher.create_owned_post(
            integration_id="xhs1", platform="xiaohongshu", content="No", mode="draft"
        )
    publisher.close()


def test_postiz_thread_schedule_payload_and_x_defaults():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=[{"postId": "thread-1"}])

    publisher = PostizPublisher(api_key="secret", transport=httpx.MockTransport(handler))
    publisher.create_owned_post(
        integration_id="x-account",
        platform="x",
        items=[
            {"content": "First", "image": [{"id": "asset-1", "path": "/a.png"}]},
            {"content": "Second", "image": []},
        ],
        mode="schedule",
        publish_at=datetime(2026, 7, 20, 10, 30, tzinfo=timezone.utc),
        settings={"made_with_ai": True, "who_can_reply_post": "following"},
    )
    payload = json.loads(requests[0].content)
    assert payload["type"] == "schedule"
    assert payload["date"] == "2026-07-20T10:30:00+00:00"
    assert payload["posts"][0]["integration"] == {"id": "x-account"}
    assert payload["posts"][0]["value"][0]["image"][0]["id"] == "asset-1"
    assert payload["posts"][0]["settings"] == {
        "__type": "x",
        "who_can_reply_post": "following",
        "community": "",
        "made_with_ai": True,
        "paid_partnership": False,
    }
    publisher.close()


def test_postiz_integrations_upload_lifecycle_and_analytics():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/integrations"):
            return httpx.Response(200, json={"integrations": [{"id": "x1", "identifier": "x"}]})
        if request.url.path.endswith("/upload") or request.url.path.endswith("/upload-from-url"):
            return httpx.Response(200, json={"id": "asset", "path": "/asset.png"})
        return httpx.Response(200, json={"ok": True})

    publisher = PostizPublisher(api_key="secret", transport=httpx.MockTransport(handler))
    assert publisher.list_integrations()[0]["id"] == "x1"
    assert publisher.upload_file(
        filename="image.png", content=b"png", content_type="image/png"
    )["id"] == "asset"
    assert publisher.upload_from_url("https://example.com/image.png")["path"] == "/asset.png"
    publisher.change_post_status("p1", "draft")
    publisher.delete_post("p1")
    publisher.post_analytics("p1")
    assert [request.method for request in requests] == ["GET", "POST", "POST", "PUT", "DELETE", "GET"]
    publisher.close()


def test_postiz_create_timeout_is_uncertain_and_not_a_regular_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    publisher = PostizPublisher(api_key="secret", transport=httpx.MockTransport(handler))
    with pytest.raises(PostizRequestUncertain, match="未返回明确结果"):
        publisher.create_owned_post(
            integration_id="x1", platform="x", content="Do not retry", mode="now"
        )
    publisher.close()
