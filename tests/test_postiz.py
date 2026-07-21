from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
import pytest

from kol_search.platform_modules.x_postiz import (
    XPostizPublisher,
    XPostizRequestUncertain,
)


def test_x_postiz_creates_owned_post():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=[{"postId": "p1", "integration": "x1"}])

    publisher = XPostizPublisher(api_key="secret", transport=httpx.MockTransport(handler))
    result = publisher.create_x_post(
        integration_id="x1", content="Owned content", mode="draft"
    )
    assert result[0]["postId"] == "p1"
    assert requests[0].url.path.endswith("/public/v1/posts")
    publisher.close()


def test_postiz_thread_schedule_payload_and_x_defaults():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=[{"postId": "thread-1"}])

    publisher = XPostizPublisher(api_key="secret", transport=httpx.MockTransport(handler))
    publisher.create_x_post(
        integration_id="x-account",
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


def test_x_postiz_lists_integrations_and_post_receipts():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/integrations"):
            return httpx.Response(200, json={"integrations": [{"id": "x1", "identifier": "x"}]})
        return httpx.Response(200, json={"posts": [{"postId": "p1"}]})

    publisher = XPostizPublisher(api_key="secret", transport=httpx.MockTransport(handler))
    assert publisher.list_integrations()[0]["id"] == "x1"
    around = datetime(2026, 7, 20, 10, 30, tzinfo=timezone.utc)
    assert publisher.recent_posts(around=around, hours=12)[0]["postId"] == "p1"
    assert [request.method for request in requests] == ["GET", "GET"]
    query = requests[1].url.params
    assert query["startDate"] == "2026-07-19T22:30:00+00:00"
    assert query["endDate"] == "2026-07-20T22:30:00+00:00"
    publisher.close()


def test_postiz_create_timeout_is_uncertain_and_not_a_regular_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    publisher = XPostizPublisher(api_key="secret", transport=httpx.MockTransport(handler))
    with pytest.raises(XPostizRequestUncertain, match="未返回明确结果"):
        publisher.create_x_post(
            integration_id="x1", content="Do not retry", mode="now"
        )
    publisher.close()
