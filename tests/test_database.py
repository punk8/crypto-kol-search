from pathlib import Path
import sqlite3

from kol_search.db import Store


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
