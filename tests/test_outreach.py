import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from kol_search.db import Store
from kol_search.models import Post
from kol_search.outreach import (
    DMBatchProcessor,
    DMSendResult,
    official_dm_readiness,
    render_dm_template,
    validate_comment,
    verify_opencli_sender,
)
from kol_search.replies import ReplyOpportunityError, publish_validated_comment
from kol_search.settings import Settings
from kol_search.twitter.reply import MockTwitterReplyClient


def _account(store: Store, account_id: str, username: str, name: str | None = None) -> str:
    return store.upsert_account({"id": account_id, "username": username, "name": name})


def _sender(store: Store, handle: str = "florus", method: str = "mock") -> int:
    return store.save_x_sender(
        label="Florus",
        x_handle=handle,
        sender_type="brand",
        send_method=method,
        opencli_profile="brand-profile" if method == "opencli_reply" else None,
        enabled=True,
        comment_auto_publish=True,
        daily_comment_limit=5,
        daily_dm_limit=5,
    )


def test_sender_uniqueness_templates_and_no_secret_columns(tmp_path: Path):
    store = Store(tmp_path / "outreach.db")
    _sender(store, "Florus")
    with pytest.raises(sqlite3.IntegrityError):
        _sender(store, "florus")
    with store.connection() as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(x_sender_accounts)")}
    assert not columns & {"password", "token", "secret", "cookie", "session"}

    assert "Alice" in render_dm_template(
        "launchvibes_invitation",
        {"username": "alice", "name": "Alice"},
        "https://launchvibes.example",
    )
    fallback = render_dm_template(
        "creator_growth_invitation",
        {"username": "bob", "name": None},
        "https://launchvibes.example",
    )
    assert "bob" in fallback


def test_dm_batch_pause_resume_counts_and_cancel_preserves_sent(tmp_path: Path):
    store = Store(tmp_path / "batch.db")
    alice = _account(store, "1", "alice", "Alice")
    bob = _account(store, "2", "bob")
    sender_id = _sender(store)
    batch_id = store.create_dm_batch(
        sender_account_id=sender_id,
        template_key="launchvibes_invitation",
        messages=[
            {"account_id": alice, "recipient_x_user_id": "1", "recipient_handle": "alice", "rendered_text": "Hi Alice"},
            {"account_id": alice, "recipient_x_user_id": "1", "recipient_handle": "alice", "rendered_text": "duplicate"},
            {"account_id": bob, "recipient_x_user_id": "2", "recipient_handle": "bob", "rendered_text": "Hi bob"},
        ],
    )
    assert len(store.list_dm_messages(batch_id)) == 2
    store.set_dm_batch_status(batch_id, "start")
    processor = DMBatchProcessor(store, Settings(kol_db_path=str(tmp_path / "unused.db")))
    assert processor.process_one()
    store.set_dm_batch_status(batch_id, "pause")
    assert processor.process_one() is False
    assert store.get_dm_batch(batch_id)["sent_count"] == 0
    assert store.get_dm_batch(batch_id)["skipped_count"] == 1
    store.set_dm_batch_status(batch_id, "resume")
    assert processor.process_one()
    assert store.get_dm_batch(batch_id)["status"] == "completed"

    cancel_id = store.create_dm_batch(
        sender_account_id=sender_id,
        template_key="creator_growth_invitation",
        messages=[
            {"account_id": alice, "recipient_x_user_id": "1", "recipient_handle": "alice", "rendered_text": "one"},
            {"account_id": bob, "recipient_x_user_id": "2", "recipient_handle": "bob", "rendered_text": "two"},
        ],
    )
    store.set_dm_batch_status(cancel_id, "start")
    first = store.claim_next_dm_message()
    store.finish_dm_message(
        first["id"], status="sent", provider="official_x_dm", provider_receipt="event-1"
    )
    store.set_dm_batch_status(cancel_id, "cancel")
    statuses = [item["status"] for item in store.list_dm_messages(cancel_id)]
    assert statuses == ["sent", "skipped"]
    assert store.get_dm_batch(cancel_id)["sent_count"] == 1


def test_ambiguous_dm_is_not_retried_and_real_dm_is_blocked(tmp_path: Path):
    store = Store(tmp_path / "ambiguous.db")
    account_id = _account(store, "1", "alice")
    sender_id = _sender(store)
    batch_id = store.create_dm_batch(
        sender_account_id=sender_id,
        template_key="launchvibes_invitation",
        messages=[{"account_id": account_id, "recipient_x_user_id": "1", "recipient_handle": "alice", "rendered_text": "hello"}],
    )
    store.set_dm_batch_status(batch_id, "start")
    calls = []

    def ambiguous(message):
        calls.append(message["id"])
        return DMSendResult(status="confirmation_required", provider="test")

    processor = DMBatchProcessor(store, Settings(), sender=ambiguous)
    assert processor.process_one()
    assert processor.process_one() is False
    assert calls == [store.list_dm_messages(batch_id)[0]["id"]]
    assert store.list_dm_messages(batch_id)[0]["status"] == "confirmation_required"
    ready, reason = official_dm_readiness(Settings())
    assert ready is False
    assert "OAuth user-context" in reason


def test_comment_validation_and_opencli_sender_identity():
    valid, reason = validate_comment(
        post_text="Creator workflow planning takes too much time",
        draft="Creator workflow planning is exactly what we are building LaunchVibes to improve.",
        suitable=True,
        style="brand",
        sender_type="brand",
        allowed_claims=[],
        forbidden_terms=[],
    )
    assert valid, reason
    assert validate_comment(
        post_text="Creator workflow planning takes time",
        draft="I use LaunchVibes every day and users love it",
        suitable=True,
        style="conversational",
        sender_type="employee",
        allowed_claims=[],
        forbidden_terms=[],
    )[0] is False
    assert validate_comment(
        post_text="Bitcoin price update",
        draft=None,
        suitable=False,
        style="brand",
        sender_type="brand",
        allowed_claims=[],
        forbidden_terms=[],
    )[0] is False

    sender = {"x_handle": "florus", "opencli_profile": "profile"}

    def runner(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, json.dumps({"username": "florus"}), "")

    assert verify_opencli_sender(Settings(), sender, runner=runner)[0] is True

    def mismatch(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, json.dumps({"username": "other"}), "")

    assert verify_opencli_sender(Settings(), sender, runner=mismatch)[0] is False


@pytest.mark.parametrize(
    ("draft", "style", "sender_type", "reason"),
    [
        (
            "As a LaunchVibes founder, I've been using LaunchVibes for creator workflow planning.",
            "conversational", "founder", "product-use",
        ),
        (
            "Creator workflow planning matters. Creators recommend LaunchVibes to everyone.",
            "brand", "brand", "testimonial",
        ),
        (
            "Creator workflow planning matters. LaunchVibes boosts engagement.",
            "brand", "brand", "outcome",
        ),
        (
            "Creator workflow planning is hard. LaunchVibes can help organize it.",
            "conversational", "employee", "affiliation",
        ),
    ],
)
def test_comment_validation_blocks_uncertain_promotional_claims(
    draft: str, style: str, sender_type: str, reason: str
):
    valid, validation_reason = validate_comment(
        post_text="Creator workflow planning takes too much time for small teams",
        draft=draft,
        suitable=True,
        style=style,
        sender_type=sender_type,
        allowed_claims=[],
        forbidden_terms=[],
    )
    assert valid is False
    assert reason in validation_reason


def test_comment_validation_accepts_clear_affiliation_and_permitted_claim():
    valid, reason = validate_comment(
        post_text="Creator workflow planning and team productivity are difficult",
        draft=(
            "I work at LaunchVibes, and creator workflow planning is why we focus on this. "
            "LaunchVibes improves creator planning productivity."
        ),
        suitable=True,
        style="conversational",
        sender_type="employee",
        allowed_claims=["LaunchVibes improves creator planning productivity"],
        forbidden_terms=[],
    )
    assert valid, reason


def test_dry_runs_do_not_count_as_real_contact_or_daily_usage(tmp_path: Path):
    store = Store(tmp_path / "dry-run.db")
    account_id = _account(store, "1", "alice")
    sender_id = _sender(store)
    batch_id = store.create_dm_batch(
        sender_account_id=sender_id,
        template_key="launchvibes_invitation",
        messages=[{
            "account_id": account_id,
            "recipient_x_user_id": "1",
            "recipient_handle": "alice",
            "rendered_text": "hello",
        }],
    )
    store.set_dm_batch_status(batch_id, "start")
    processor = DMBatchProcessor(store, Settings(_env_file=None))
    assert processor.process_one()
    message = store.list_dm_messages(batch_id)[0]
    assert message["status"] == "skipped"
    assert message["provider"] == "dry_run"
    assert store.get_dm_batch(batch_id)["sent_count"] == 0
    assert store.dm_block_reason(
        account_id, template_key="launchvibes_invitation"
    ) is None
    store.save_x_sender(
        sender_id=sender_id,
        label="Florus",
        x_handle="florus",
        sender_type="brand",
        send_method="official_x_dm",
        enabled=True,
        daily_comment_limit=5,
        daily_dm_limit=1,
    )
    real_batch = store.create_dm_batch(
        sender_account_id=sender_id,
        template_key="launchvibes_invitation",
        messages=[{
            "account_id": account_id,
            "recipient_x_user_id": "1",
            "recipient_handle": "alice",
            "rendered_text": "hello again",
        }],
    )
    store.set_dm_batch_status(real_batch, "start")
    assert store.claim_next_dm_message()["status"] == "sending"


def test_existing_database_is_upgraded_without_recreating_reply_records(tmp_path: Path):
    path = tmp_path / "existing.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE reply_opportunities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            post_id TEXT NOT NULL UNIQUE,
            account_id TEXT NOT NULL,
            score REAL NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'pending',
            language TEXT NOT NULL DEFAULT 'unknown',
            score_json TEXT NOT NULL DEFAULT '{}',
            reasons_json TEXT NOT NULL DEFAULT '[]',
            draft TEXT NOT NULL DEFAULT '',
            draft_source TEXT NOT NULL DEFAULT 'rules',
            manually_edited INTEGER NOT NULL DEFAULT 0,
            expires_at TEXT NOT NULL,
            first_seen_at TEXT NOT NULL,
            last_scored_at TEXT NOT NULL,
            replied_at TEXT,
            reply_url TEXT,
            next_check_at TEXT,
            responded_at TEXT,
            outcome_json TEXT NOT NULL DEFAULT '{}'
        );
        INSERT INTO reply_opportunities(
            post_id, account_id, score, status, expires_at, first_seen_at, last_scored_at
        ) VALUES('post-1', 'account-1', 10, 'responded',
                 '2026-08-01T00:00:00+00:00', '2026-07-01T00:00:00+00:00',
                 '2026-07-01T00:00:00+00:00');
        """
    )
    connection.commit()
    connection.close()

    store = Store(path)
    store.migrate()
    with store.connection() as upgraded:
        row = upgraded.execute(
            "SELECT post_id, status FROM reply_opportunities WHERE id=1"
        ).fetchone()
        columns = {
            item[1] for item in upgraded.execute("PRAGMA table_info(reply_opportunities)")
        }
        sender_tables = upgraded.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='x_sender_accounts'"
        ).fetchone()[0]
    assert dict(row) == {"post_id": "post-1", "status": "responded"}
    assert {"sender_account_id", "suitable", "validation_status", "send_backend"} <= columns
    assert sender_tables == 1


def test_real_send_limits_reserve_in_flight_and_exclude_failures(tmp_path: Path):
    store = Store(tmp_path / "limits.db")
    first_account = _account(store, "1", "alice")
    second_account = _account(store, "2", "bob")
    sender_id = store.save_x_sender(
        label="Official",
        x_handle="florus",
        sender_type="brand",
        send_method="official_x_dm",
        enabled=True,
        daily_comment_limit=1,
        daily_dm_limit=1,
    )
    batch_id = store.create_dm_batch(
        sender_account_id=sender_id,
        template_key="launchvibes_invitation",
        messages=[
            {"account_id": first_account, "recipient_x_user_id": "1", "recipient_handle": "alice", "rendered_text": "one"},
            {"account_id": second_account, "recipient_x_user_id": "2", "recipient_handle": "bob", "rendered_text": "two"},
        ],
    )
    store.set_dm_batch_status(batch_id, "start")
    first = store.claim_next_dm_message()
    assert first and first["status"] == "sending"
    second = store.claim_next_dm_message()
    assert second and second["status"] == "skipped"
    assert "daily DM limit" in store.list_dm_messages(batch_id)[1]["sanitized_error"]
    store.finish_dm_message(first["id"], status="failed", provider="official_x_dm")
    retry_batch = store.create_dm_batch(
        sender_account_id=sender_id,
        template_key="launchvibes_invitation",
        messages=[{"account_id": second_account, "recipient_x_user_id": "2", "recipient_handle": "bob", "rendered_text": "retry after failure"}],
    )
    store.set_dm_batch_status(retry_batch, "start")
    assert store.claim_next_dm_message()["status"] == "sending"


def test_comment_limit_and_same_kol_window_are_atomic(tmp_path: Path):
    store = Store(tmp_path / "comment-guards.db")
    sender_id = store.save_x_sender(
        label="Florus",
        x_handle="florus",
        sender_type="brand",
        send_method="opencli_reply",
        opencli_profile="florus-profile",
        enabled=True,
        daily_comment_limit=1,
        daily_dm_limit=1,
    )
    for account_id, username in (("1", "alice"), ("2", "bob")):
        _account(store, account_id, username)
    store.save_posts([
        Post(id="101", author_id="1", author_username="alice", text="creator workflow", url="https://x.com/alice/status/101"),
        Post(id="102", author_id="2", author_username="bob", text="creator workflow", url="https://x.com/bob/status/102"),
        Post(id="103", author_id="1", author_username="alice", text="content planning", url="https://x.com/alice/status/103"),
    ])

    def opportunity(post_id: str, account_id: str) -> int:
        return store.upsert_reply_opportunity(
            post_id=post_id,
            account_id=account_id,
            score=10,
            language="en",
            score_payload={},
            reasons=[],
            draft="Creator workflow is relevant to LaunchVibes",
            draft_source="rules",
            expires_at="2099-01-01T00:00:00+00:00",
            sender_account_id=sender_id,
            initial_status="validated",
        )

    first_id = opportunity("101", "1")
    second_id = opportunity("102", "2")
    store.claim_reply_for_publish(
        first_id, sender_id=sender_id, backend="opencli", daily_limit=1
    )
    assert store.interrupt_publishing_replies() == 1
    assert store.get_reply_opportunity(first_id)["status"] == "confirmation_required"
    with pytest.raises(ValueError, match="daily comment limit"):
        store.claim_reply_for_publish(
            second_id, sender_id=sender_id, backend="opencli", daily_limit=1
        )
    store.update_reply_opportunity(first_id, status="failed")
    store.claim_reply_for_publish(
        second_id, sender_id=sender_id, backend="opencli", daily_limit=1
    )
    store.update_reply_opportunity(second_id, status="failed")

    store.update_reply_opportunity(
        first_id,
        status="replied",
        reply_url="https://x.com/florus/status/1001",
        send_backend="opencli",
    )
    third_id = opportunity("103", "1")
    with pytest.raises(ValueError, match="recent promotional comment"):
        store.claim_reply_for_publish(
            third_id, sender_id=sender_id, backend="opencli", daily_limit=10
        )


def test_rescan_preserves_published_reply_audit_fields(tmp_path: Path):
    store = Store(tmp_path / "audit.db")
    account_id = _account(store, "1", "alice")
    first_sender = _sender(store, "launchvibes")
    second_sender = _sender(store, "florus")
    store.save_posts([
        Post(
            id="201", author_id=account_id, author_username="alice",
            text="Creator workflow planning", url="https://x.com/alice/status/201",
        )
    ])
    opportunity_id = store.upsert_reply_opportunity(
        post_id="201", account_id=account_id, score=90, language="en",
        score_payload={"relevance": 30}, reasons=["original"],
        draft="Exact text sent by LaunchVibes", draft_source="openai",
        expires_at="2099-01-01T00:00:00+00:00",
        sender_account_id=first_sender, comment_style="brand", publish_mode="auto",
        campaign_goal="Original campaign goal", suitable=True,
        suitability_reason="Original suitability", validation_status="passed",
        validation_reason="Original validation", initial_status="validated",
    )
    store.update_reply_opportunity(
        opportunity_id, status="publishing", send_backend="opencli"
    )
    store.update_reply_opportunity(
        opportunity_id, status="replied",
        reply_url="https://x.com/launchvibes/status/9001",
        sanitized_error="sanitized historical note",
    )
    store.save_reply_outcome(
        opportunity_id, stage_hours=1, metrics={"like_count": 3},
        author_responded=False, payload={"safe": "outcome"},
    )
    before = store.get_reply_opportunity(opportunity_id)

    rescanned_id = store.upsert_reply_opportunity(
        post_id="201", account_id=account_id, score=99, language="en",
        score_payload={"relevance": 40}, reasons=["rescanned"],
        draft="Replacement generated text", draft_source="rules",
        expires_at="2099-02-01T00:00:00+00:00",
        sender_account_id=second_sender, comment_style="conversational",
        publish_mode="review", campaign_goal="Replacement goal", suitable=False,
        suitability_reason="Replacement suitability", validation_status="failed",
        validation_reason="Replacement validation", initial_status="unsuitable",
    )
    after = store.get_reply_opportunity(rescanned_id)

    assert rescanned_id == opportunity_id
    protected = {
        "status", "draft", "draft_source", "sender_account_id", "comment_style",
        "publish_mode", "campaign_goal", "suitable", "suitability_reason",
        "validation_status", "validation_reason", "send_backend", "attempted_at",
        "published_at", "replied_at", "reply_url", "sanitized_error", "outcome",
    }
    assert {key: after[key] for key in protected} == {
        key: before[key] for key in protected
    }
    assert after["score"] == 99


def test_publish_policy_blocks_remain_policy_errors(monkeypatch, tmp_path: Path):
    store = Store(tmp_path / "policy-errors.db")
    sender_id = store.save_x_sender(
        label="LaunchVibes", x_handle="launchvibes", sender_type="brand",
        send_method="opencli_reply", opencli_profile="launchvibes",
        enabled=True, comment_auto_publish=True, daily_comment_limit=1,
        daily_dm_limit=0,
    )
    for account_id, username in (("1", "alice"), ("2", "bob")):
        _account(store, account_id, username)
    store.save_posts([
        Post(id="301", author_id="1", author_username="alice", text="creator workflow", url="https://x.com/alice/status/301"),
        Post(id="302", author_id="1", author_username="alice", text="content planning", url="https://x.com/alice/status/302"),
        Post(id="303", author_id="2", author_username="bob", text="creator planning", url="https://x.com/bob/status/303"),
    ])

    def opportunity(post_id: str, account_id: str) -> int:
        return store.upsert_reply_opportunity(
            post_id=post_id, account_id=account_id, score=10, language="en",
            score_payload={}, reasons=[], draft="Creator planning with LaunchVibes",
            draft_source="openai", expires_at="2099-01-01T00:00:00+00:00",
            sender_account_id=sender_id, validation_status="passed",
            initial_status="validated",
        )

    monkeypatch.setattr("kol_search.replies.verify_opencli_sender", lambda *args: (True, "ok"))
    monkeypatch.setattr(
        "kol_search.replies.create_twitter_reply_client",
        lambda *args, **kwargs: MockTwitterReplyClient(),
    )
    settings = Settings(_env_file=None, KOL_DB_PATH=str(tmp_path / "unused.db"))
    first_id = opportunity("301", "1")
    publish_validated_comment(store, settings, first_id)

    with pytest.raises(ReplyOpportunityError, match="validated"):
        publish_validated_comment(store, settings, first_id)

    recent_id = opportunity("302", "1")
    with pytest.raises(ReplyOpportunityError, match="recent promotional comment"):
        publish_validated_comment(store, settings, recent_id)
    assert store.get_reply_opportunity(recent_id)["status"] == "validated"

    daily_id = opportunity("303", "2")
    with pytest.raises(ReplyOpportunityError, match="daily comment limit"):
        publish_validated_comment(store, settings, daily_id)
    assert store.get_reply_opportunity(daily_id)["status"] == "validated"


def test_dm_template_block_and_cross_channel_recent_contact_is_warning_only(tmp_path: Path):
    store = Store(tmp_path / "cross-channel.db")
    sender_id = store.save_x_sender(
        label="Official", x_handle="launchvibes", sender_type="brand",
        send_method="official_x_dm", enabled=True,
        daily_comment_limit=10, daily_dm_limit=10,
    )
    first_account = _account(store, "1", "alice")
    second_account = _account(store, "2", "bob")

    first_batch = store.create_dm_batch(
        sender_account_id=sender_id, template_key="launchvibes_invitation",
        messages=[{"account_id": first_account, "recipient_x_user_id": "1", "recipient_handle": "alice", "rendered_text": "hello"}],
    )
    store.set_dm_batch_status(first_batch, "start")
    first_message = store.claim_next_dm_message()
    store.finish_dm_message(first_message["id"], status="sent", provider="official_x_dm")
    assert "Same DM template" in store.dm_block_reason(
        first_account, template_key="launchvibes_invitation"
    )
    assert store.comment_block_reason(first_account) is None
    assert "recent DM" in store.cross_channel_outreach_warning(
        first_account, target_channel="comment"
    )

    store.save_posts([
        Post(id="401", author_id=second_account, author_username="bob", text="creator workflow", url="https://x.com/bob/status/401")
    ])
    comment_id = store.upsert_reply_opportunity(
        post_id="401", account_id=second_account, score=10, language="en",
        score_payload={}, reasons=[], draft="Creator workflow with LaunchVibes",
        draft_source="openai", expires_at="2099-01-01T00:00:00+00:00",
        sender_account_id=sender_id, initial_status="validated",
    )
    store.update_reply_opportunity(
        comment_id, status="replied", reply_url="https://x.com/launchvibes/status/4001",
        send_backend="opencli",
    )
    assert store.dm_block_reason(
        second_account, template_key="launchvibes_invitation"
    ) is None
    assert "recent Comment" in store.cross_channel_outreach_warning(
        second_account, target_channel="dm"
    )
