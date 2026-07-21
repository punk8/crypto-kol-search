from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from kol_search.db import Store
from kol_search.postiz import (
    PostizClient,
    PostizIntegration,
    PostizSubmissionUncertain,
    safe_postiz_release_url,
)


def _topic(store: Store) -> int:
    topic_id = store.upsert_topic_cluster(
        fingerprint="topic:owned-content",
        title="Creator workflow",
        summary="Creators are discussing planning workflows",
        language="en",
        lifecycle="rising",
        heat_score=80,
        metrics={"post_count": 4},
        outline="Explain a planning workflow",
        draft="A practical creator planning workflow",
        draft_source="rules",
        native_trend=False,
        post_ids=[],
    )
    store.update_topic_cluster(topic_id, status="adopted")
    return topic_id


def test_postiz_client_uses_public_api_contract() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["Authorization"] == "test-api-key"
        if request.method == "GET" and request.url.path.endswith("/integrations"):
            return httpx.Response(
                200,
                json=[{"id": "int-1", "name": "LaunchVibes", "identifier": "x", "disabled": False}],
            )
        if request.method == "POST":
            payload = json.loads(request.content)
            assert payload["type"] == "schedule"
            assert payload["date"] == "2026-07-30T10:00:00+00:00"
            assert len(payload["posts"]) == 1
            assert payload["posts"][0]["integration"] == {"id": "int-1"}
            assert payload["posts"][0]["value"] == [
                {"content": "Owned content", "image": []}
            ]
            assert payload["posts"][0]["settings"] == {
                "who_can_reply_post": "everyone"
            }
            return httpx.Response(200, json=[{"postId": "post-1", "integration": "int-1"}])
        return httpx.Response(
            200,
            json=[
                {
                    "id": "post-1",
                    "state": "PUBLISHED",
                    "releaseURL": "https://x.com/launchvibes/status/1",
                }
            ],
        )

    client = PostizClient(
        "https://api.postiz.test",
        "test-api-key",
        transport=httpx.MockTransport(handler),
    )
    try:
        integration = client.list_integrations()[0]
        result = client.create_post(
            integration=integration,
            content="Owned content",
            publish_mode="schedule",
            publish_at="2026-07-30T10:00:00+00:00",
        )
        assert result.post_id == "post-1"
        status = client.get_post(
            "post-1",
            start_date="2026-07-29T00:00:00+00:00",
            end_date="2026-07-31T00:00:00+00:00",
        )
        assert status and status["state"] == "PUBLISHED"
    finally:
        client.close()
    assert [request.url.path for request in requests] == [
        "/public/v1/integrations",
        "/public/v1/posts",
        "/public/v1/posts",
    ]


def test_postiz_ambiguous_submission_is_not_treated_as_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("receipt timed out", request=request)

    client = PostizClient(
        "https://api.postiz.test",
        "test-api-key",
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(PostizSubmissionUncertain):
            client.create_post(
                integration=PostizIntegration("int-1", "LaunchVibes", "x", False),
                content="Owned content",
                publish_mode="now",
                publish_at="2026-07-30T10:00:00+00:00",
            )
    finally:
        client.close()


def test_postiz_release_url_rejects_unsafe_links() -> None:
    assert safe_postiz_release_url("https://x.com/launchvibes/status/1")
    assert safe_postiz_release_url("javascript:alert(1)") is None
    assert safe_postiz_release_url("https://user:secret@example.com/post") is None


def test_postiz_publication_is_single_result_and_restart_safe(tmp_path: Path) -> None:
    path = tmp_path / "postiz.db"
    store = Store(path)
    topic_id = _topic(store)
    publication_id = store.create_postiz_publication(
        topic_cluster_id=topic_id,
        integration_id="int-1",
        integration_name="LaunchVibes",
        integration_identifier="x",
        content_text="Exact approved text",
        publish_mode="now",
        scheduled_at=None,
    )
    with pytest.raises(ValueError, match="already has"):
        store.create_postiz_publication(
            topic_cluster_id=topic_id,
            integration_id="int-2",
            integration_name="Another account",
            integration_identifier="x",
            content_text="Duplicate",
            publish_mode="now",
            scheduled_at=None,
        )
    store.claim_postiz_submission(publication_id)

    restarted = Store(path)
    saved = restarted.get_postiz_publication(publication_id)
    assert saved and saved["status"] == "confirmation_required"
    assert "confirm in Postiz" in saved["sanitized_error"]
