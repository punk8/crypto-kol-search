from datetime import datetime, timezone
from pathlib import Path

from kol_search.db import Store
from kol_search.discovery.signal_ai import ReplyDraftResult
from kol_search.discovery.signals import SignalPipeline
from kol_search.settings import Settings


def test_signal_pipeline_builds_idempotent_reply_and_topic_queues(tmp_path: Path):
    settings = Settings(
        _env_file=None,
        TWITTER_BACKEND="mock",
        KOL_DB_PATH=str(tmp_path / "signals.db"),
        KOL_SIGNAL_CORE_ACCOUNTS=20,
        KOL_SIGNAL_ROTATION_ACCOUNTS=0,
        KOL_SIGNAL_REPLY_LIMIT=20,
        KOL_SIGNAL_TOPIC_LIMIT=10,
    )
    store = Store(settings.db_path())
    store.save_brand_profile(
        brand_name="Signal Labs",
        x_handle="signal_labs",
        description="Onchain research and discovery",
        audience="Crypto researchers",
        tone="professional and concise",
        allowed_claims=[],
        forbidden_terms=["guaranteed"],
    )
    now = datetime.now(timezone.utc)
    pipeline = SignalPipeline(store, settings, now_provider=lambda: now)

    first_run = store.create_run(
        query="signals", backend="mock", kind="signal_scan", use_ai=False
    )
    first = pipeline.run(store.get_run(first_run))
    replies = store.list_reply_opportunities(status="all")
    topics = store.list_topic_clusters(status="all")

    assert first
    assert replies
    assert topics
    assert all(item["post_url"].startswith("https://x.com/") for item in replies)
    assert all(item["score_payload"] for item in replies)
    assert all(item["metrics"]["post_count"] >= 3 for item in topics)

    opportunity_id = replies[0]["id"]
    store.update_reply_opportunity(opportunity_id, draft="Keep this human edit")
    second_run = store.create_run(
        query="signals", backend="mock", kind="signal_scan", use_ai=False
    )
    pipeline.run(store.get_run(second_run))

    after = store.list_reply_opportunities(status="all")
    assert len(after) == len(replies)
    assert next(item for item in after if item["id"] == opportunity_id)["draft"] == "Keep this human edit"
    assert store.get_scan_checkpoint("mock:search:bitcoin OR BTC") is not None


def test_signal_auto_publish_respects_sender_setting(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(
        "kol_search.discovery.signals.generate_reply_draft",
        lambda **kwargs: ReplyDraftResult(
            suitable=True,
            reason="Relevant creator workflow discussion",
            draft=f"{kwargs['post']['text'][:80]} — creator workflow with LaunchVibes",
        ),
    )
    settings = Settings(
        _env_file=None,
        TWITTER_BACKEND="mock",
        KOL_DB_PATH=str(tmp_path / "auto.db"),
        KOL_SIGNAL_ROTATION_ACCOUNTS=0,
        OPENAI_API_KEY="test-key",
    )
    store = Store(settings.db_path())
    store.save_brand_profile(
        brand_name="LaunchVibes",
        x_handle="launchvibes",
        description="Creator workflow and content planning",
        audience="Creators",
        tone="clear",
        allowed_claims=[],
        forbidden_terms=["guaranteed"],
    )
    sender_id = store.save_x_sender(
        label="LaunchVibes",
        x_handle="launchvibes",
        sender_type="brand",
        send_method="mock",
        enabled=True,
        comment_auto_publish=True,
        daily_comment_limit=20,
        daily_dm_limit=0,
    )
    run_id = store.create_run(
        query="signals",
        backend="mock",
        kind="signal_scan",
        use_ai=True,
        config={
            "sender_account_id": sender_id,
            "comment_style": "brand",
            "publish_mode": "auto",
            "campaign_goal": "Introduce LaunchVibes when relevant",
        },
    )
    SignalPipeline(store, settings).run(store.get_run(run_id))
    opportunities = store.list_reply_opportunities(status="all")
    assert opportunities
    assert any(item["status"] == "replied" for item in opportunities)
    assert all(item["sender_account_id"] == sender_id for item in opportunities)

    disabled_id = store.save_x_sender(
        label="Review only",
        x_handle="review_only",
        sender_type="brand",
        send_method="mock",
        enabled=True,
        comment_auto_publish=False,
        daily_comment_limit=20,
        daily_dm_limit=0,
    )
    second_id = store.create_run(
        query="signals",
        backend="mock",
        kind="signal_scan",
        use_ai=True,
        config={
            "sender_account_id": disabled_id,
            "comment_style": "brand",
            "publish_mode": "auto",
        },
    )
    SignalPipeline(store, settings).run(store.get_run(second_id))
    assert not any(
        item["status"] == "replied" and item["sender_account_id"] == disabled_id
        for item in store.list_reply_opportunities(status="all")
    )


def test_uncertain_auto_publish_drafts_require_review(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(
        "kol_search.discovery.signals.generate_reply_draft",
        lambda **kwargs: ReplyDraftResult(
            suitable=True,
            reason="Relevant creator discussion",
            draft=(
                f"{kwargs['post']['text'][:100]} — LaunchVibes doubles engagement."
            ),
        ),
    )
    settings = Settings(
        _env_file=None,
        TWITTER_BACKEND="mock",
        KOL_DB_PATH=str(tmp_path / "uncertain-auto.db"),
        KOL_SIGNAL_ROTATION_ACCOUNTS=0,
        OPENAI_API_KEY="test-key",
    )
    store = Store(settings.db_path())
    store.save_brand_profile(
        brand_name="LaunchVibes", x_handle="launchvibes",
        description="Creator workflow and content planning", audience="Creators",
        tone="clear", allowed_claims=[], forbidden_terms=[],
    )
    sender_id = store.save_x_sender(
        label="LaunchVibes", x_handle="launchvibes", sender_type="brand",
        send_method="mock", enabled=True, comment_auto_publish=True,
        daily_comment_limit=20, daily_dm_limit=0,
    )
    run_id = store.create_run(
        query="signals", backend="mock", kind="signal_scan", use_ai=True,
        config={
            "sender_account_id": sender_id,
            "comment_style": "brand",
            "publish_mode": "auto",
        },
    )

    SignalPipeline(store, settings).run(store.get_run(run_id))
    opportunities = store.list_reply_opportunities(status="all")

    assert opportunities
    assert all(item["status"] == "review_required" for item in opportunities)
    assert all(item["validation_status"] == "failed" for item in opportunities)
    assert not any(item["status"] == "replied" for item in opportunities)
