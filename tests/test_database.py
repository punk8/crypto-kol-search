from pathlib import Path
import sqlite3

from kol_search.db import Store
from kol_search.models import Post


def test_job_lifecycle_and_retry(tmp_path: Path):
    store = Store(tmp_path / "kol.db")
    run_id = store.create_run(query="DeFi", backend="mock")
    job = store.claim_next_job()
    assert job and job["run_id"] == run_id
    assert store.get_run(run_id)["status"] == "running"
    assert store.interrupt_running_jobs() == 1
    assert store.get_run(run_id)["status"] == "interrupted"

    retried = store.retry_run(run_id)
    assert retried != run_id
    assert store.get_run(retried)["parent_run_id"] == run_id
    assert store.get_run(retried)["status"] == "queued"


def test_migrates_original_research_schema(tmp_path: Path):
    path = tmp_path / "legacy.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, domain TEXT NOT NULL, backend TEXT NOT NULL,
            started_at TEXT NOT NULL, finished_at TEXT, candidate_count INTEGER DEFAULT 0,
            meta_json TEXT DEFAULT '{}'
        );
        CREATE TABLE accounts (
            id TEXT PRIMARY KEY, username TEXT NOT NULL, name TEXT, description TEXT,
            followers_count INTEGER DEFAULT 0, following_count INTEGER DEFAULT 0,
            tweet_count INTEGER DEFAULT 0, verified INTEGER DEFAULT 0,
            raw_json TEXT DEFAULT '{}', updated_at TEXT NOT NULL
        );
        CREATE TABLE posts (
            id TEXT PRIMARY KEY, author_id TEXT, author_username TEXT, text TEXT, created_at TEXT,
            like_count INTEGER DEFAULT 0, retweet_count INTEGER DEFAULT 0,
            reply_count INTEGER DEFAULT 0, quote_count INTEGER DEFAULT 0, raw_json TEXT DEFAULT '{}'
        );
        """
    )
    connection.close()
    store = Store(path)
    run_id = store.create_run(query="bitcoin", backend="mock")
    assert store.get_run(run_id)["status"] == "queued"


def test_account_identity_reuses_existing_handle(tmp_path: Path):
    store = Store(tmp_path / "identity.db")
    assert store.upsert_account({"id": "123", "username": "Alice"}) == "123"
    assert store.resolve_account_id("alice", "opencli:alice") == "123"
    assert store.upsert_account({"id": "opencli:alice", "username": "alice"}) == "123"
    assert store.get_account("123")["username"] == "alice"


def test_post_metric_snapshots_preserve_history(tmp_path: Path):
    store = Store(tmp_path / "metrics.db")
    post = Post(
        id="post-1",
        author_id="account-1",
        author_username="alice",
        text="DeFi market update",
        created_at="2026-07-16T01:00:00+00:00",
        like_count=10,
        retweet_count=2,
        view_count=100,
        conversation_id="conversation-1",
        url="https://x.com/alice/status/post-1",
    )
    store.save_posts([post], captured_at="2026-07-16T01:05:00+00:00")

    post.like_count = 25
    post.retweet_count = 6
    post.view_count = 240
    store.save_posts([post], captured_at="2026-07-16T01:35:00+00:00")

    history = store.get_post_metric_history("post-1")
    assert [item["like_count"] for item in history] == [10, 25]
    assert [item["view_count"] for item in history] == [100, 240]
    with store.connection() as connection:
        saved = connection.execute(
            "SELECT url, conversation_id, like_count FROM posts WHERE id='post-1'"
        ).fetchone()
    assert dict(saved) == {
        "url": "https://x.com/alice/status/post-1",
        "conversation_id": "conversation-1",
        "like_count": 25,
    }


def test_scan_checkpoint_round_trip(tmp_path: Path):
    store = Store(tmp_path / "checkpoint.db")
    assert store.get_scan_checkpoint("official:search:defi") is None

    store.upsert_scan_checkpoint(
        "official:search:defi",
        since_id="12345",
        cursor="next-page",
        metadata={"query": "defi", "result_count": 20},
        succeeded_at="2026-07-16T01:00:00+00:00",
    )

    assert store.get_scan_checkpoint("official:search:defi") == {
        "source_key": "official:search:defi",
        "since_id": "12345",
        "cursor": "next-page",
        "last_success_at": "2026-07-16T01:00:00+00:00",
        "updated_at": "2026-07-16T01:00:00+00:00",
        "metadata": {"query": "defi", "result_count": 20},
    }


def test_brand_reply_and_topic_workflows(tmp_path: Path):
    store = Store(tmp_path / "signals.db")
    store.save_brand_profile(
        brand_name="Signal Labs",
        x_handle="signal_labs",
        description="Onchain research tools",
        audience="Crypto researchers",
        tone="concise",
        allowed_claims=["Public onchain data"],
        forbidden_terms=["guaranteed"],
    )
    assert store.get_brand_profile()["x_handle"] == "signal_labs"

    account_id = store.upsert_account({"id": "1", "username": "alice"})
    store.save_posts([
        Post(
            id="100",
            author_id=account_id,
            author_username="alice",
            text="DeFi liquidity is changing",
            created_at="2026-07-16T01:00:00+00:00",
            url="https://x.com/alice/status/100",
        )
    ])
    opportunity_id = store.upsert_reply_opportunity(
        post_id="100",
        account_id=account_id,
        score=80,
        language="en",
        score_payload={"relevance": 30},
        reasons=["Relevant"],
        draft="Initial draft",
        draft_source="rules",
        expires_at="2026-07-17T01:00:00+00:00",
    )
    store.update_reply_opportunity(opportunity_id, draft="Human draft")
    store.upsert_reply_opportunity(
        post_id="100",
        account_id=account_id,
        score=90,
        language="en",
        score_payload={"relevance": 30},
        reasons=["Updated"],
        draft="Generated replacement",
        draft_source="rules",
        expires_at="2026-07-17T01:00:00+00:00",
    )
    assert store.list_reply_opportunities(status="all")[0]["draft"] == "Human draft"
    store.update_reply_opportunity(
        opportunity_id,
        status="replied",
        reply_url="https://x.com/signal_labs/status/200",
    )
    assert store.list_reply_opportunities(status="replied")[0]["next_check_at"]

    cluster_id = store.upsert_topic_cluster(
        fingerprint="topic:defi",
        title="DeFi",
        summary="Rising discussion",
        language="en",
        lifecycle="rising",
        heat_score=75,
        metrics={"post_count": 3, "unique_authors": 2},
        outline="Initial outline",
        draft="Initial topic draft",
        draft_source="rules",
        native_trend=True,
        post_ids=[("100", 1.0)],
    )
    store.update_topic_cluster(cluster_id, status="adopted", draft="Human topic draft")
    assert store.list_topic_clusters(status="adopted")[0]["draft"] == "Human topic draft"
    store.upsert_topic_cluster(
        fingerprint="topic:defi",
        title="DeFi refreshed",
        summary="Latest snapshot",
        language="en",
        lifecycle="declining",
        heat_score=20,
        metrics={"post_count": 0, "unique_authors": 0},
        outline="Generated replacement",
        draft="Generated replacement",
        draft_source="rules",
        native_trend=False,
        post_ids=[],
        replace_posts=True,
    )
    refreshed = store.get_topic_cluster(cluster_id)
    assert refreshed is not None
    assert refreshed["posts"] == []
    assert refreshed["draft"] == "Human topic draft"
