from datetime import datetime, timezone
from pathlib import Path

from kol_search.db import Store
from kol_search.discovery.signals import SignalPipeline
from kol_search.settings import Settings


def test_signal_pipeline_builds_idempotent_reply_and_topic_queues(tmp_path: Path):
    settings = Settings(
        _env_file=None,
        TWITTER_BACKEND="mock",
        KOL_ENABLE_MOCK_BACKEND=True,
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
