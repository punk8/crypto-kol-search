import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from kol_search.db import Store
from kol_search.models import Post
from kol_search.replies import ReplyOpportunityError, ReplyPublisher
from kol_search.settings import Settings
from kol_search.twitter.reply import (
    MockTwitterReplyClient,
    OpenCliTwitterReplyClient,
    ReplyBackendUnavailableError,
    ReplyConfirmationRequiredError,
    ReplyPublishRequest,
    create_twitter_reply_client,
)


def _queued_opportunity(store: Store, post_id: str = "post-1") -> int:
    store.upsert_account(
        {
            "id": "account-1",
            "username": "alice",
            "name": "Alice",
            "followers_count": 10_000,
        }
    )
    store.save_posts(
        [
            Post(
                id=post_id,
                author_id="account-1",
                author_username="alice",
                text="DeFi liquidity is changing",
                created_at="2026-07-16T01:00:00+00:00",
                url=f"https://x.com/alice/status/{post_id}",
            )
        ]
    )
    return store.upsert_reply_opportunity(
        post_id=post_id,
        account_id="account-1",
        score=88,
        language="en",
        score_payload={"relevance": 30},
        reasons=["Relevant discussion"],
        draft="Which liquidity metric are you watching next?",
        draft_source="rules",
        expires_at="2026-07-17T01:00:00+00:00",
    )


def test_reply_request_rejects_blank_and_oversized_text():
    with pytest.raises(ValidationError):
        ReplyPublishRequest(
            target_post_id="post-1",
            target_post_url="https://x.com/alice/status/1",
            text=" ",
            actor_handle="signal_labs",
            idempotency_key="key-1",
        )
    with pytest.raises(ValidationError):
        ReplyPublishRequest(
            target_post_id="post-1",
            target_post_url="https://x.com/alice/status/1",
            text="x" * 281,
            actor_handle="signal_labs",
            idempotency_key="key-2",
        )


def test_mock_reply_client_is_idempotent():
    now = datetime(2026, 7, 16, 2, 0, tzinfo=timezone.utc)
    client = MockTwitterReplyClient(now_provider=lambda: now)
    request = ReplyPublishRequest(
        target_post_id="post-1",
        target_post_url="https://x.com/alice/status/1",
        text="Useful signal. What changes your view?",
        actor_handle="@signal_labs",
        idempotency_key="key-1",
    )

    first = client.publish_reply(request)
    second = client.publish_reply(request)

    assert first == second
    assert first.backend == "mock"
    assert first.target_post_id == "post-1"
    assert first.reply_url.startswith("https://x.com/signal_labs/status/")


def test_reply_publisher_persists_provider_receipt(tmp_path: Path):
    store = Store(tmp_path / "reply.db")
    opportunity_id = _queued_opportunity(store)
    client = MockTwitterReplyClient()
    publisher = ReplyPublisher(store, client, "signal_labs")

    receipt = publisher.publish(opportunity_id)
    saved = store.get_reply_opportunity(opportunity_id)

    assert saved and saved["status"] == "replied"
    assert saved["reply_url"] == receipt.reply_url
    assert saved["replied_at"]
    assert saved["next_check_at"]
    with pytest.raises(ReplyOpportunityError):
        publisher.publish(opportunity_id)


def test_opencli_reply_client_submits_and_confirms_receipt():
    calls: list[list[str]] = []

    def runner(command, **kwargs):
        calls.append(command)
        if command[4] == "reply":
            payload = [{
                "status": "success",
                "message": "Reply posted successfully.",
                "text": "Useful signal. What changes your view?",
            }]
        elif command[4] == "tweets":
            payload = [{
                "id": "987654321",
                "text": "Useful signal. What changes your view?",
                "url": "https://x.com/i/status/987654321",
            }]
        else:
            raise AssertionError(command)
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(payload), stderr="")

    client = OpenCliTwitterReplyClient(
        command="/usr/bin/true",
        profile="ddd",
        runner=runner,
        now_provider=lambda: datetime(2026, 7, 16, 2, 0, tzinfo=timezone.utc),
        sleep=lambda _: None,
    )
    receipt = client.publish_reply(ReplyPublishRequest(
        target_post_id="123456789",
        target_post_url="https://x.com/alice/status/123456789",
        text="Useful signal. What changes your view?",
        actor_handle="signal_labs",
        idempotency_key="reply-opportunity:1",
    ))

    assert receipt.backend == "opencli"
    assert receipt.reply_post_id == "987654321"
    assert receipt.reply_url == "https://x.com/i/status/987654321"
    assert calls[0] == [
        "/usr/bin/true", "--profile", "ddd", "twitter", "reply",
        "https://x.com/alice/status/123456789",
        "Useful signal. What changes your view?", "-f", "json",
    ]
    assert calls[1][3:8] == [
        "twitter", "tweets", "signal_labs", "--limit", "20"
    ]


def test_opencli_unconfirmed_submission_is_not_retried(tmp_path: Path):
    calls: list[list[str]] = []

    def runner(command, **kwargs):
        calls.append(command)
        payload = (
            [{"status": "success", "message": "Reply posted successfully."}]
            if command[4] == "reply"
            else []
        )
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(payload), stderr="")

    store = Store(tmp_path / "unconfirmed.db")
    opportunity_id = _queued_opportunity(store, post_id="123456789")
    client = OpenCliTwitterReplyClient(
        command="/usr/bin/true",
        runner=runner,
        sleep=lambda _: None,
        verification_attempts=1,
    )

    with pytest.raises(ReplyConfirmationRequiredError):
        ReplyPublisher(store, client, "signal_labs").publish(opportunity_id)

    assert store.get_reply_opportunity(opportunity_id)["status"] == "confirmation_required"
    assert sum(1 for command in calls if command[4] == "reply") == 1


def test_reply_backend_factory_exposes_opencli_only():
    settings = Settings(_env_file=None)
    with pytest.raises(ReplyBackendUnavailableError, match="twitterapi_io"):
        create_twitter_reply_client("twitterapi_io", settings)
    assert isinstance(
        create_twitter_reply_client("opencli", settings),
        OpenCliTwitterReplyClient,
    )
