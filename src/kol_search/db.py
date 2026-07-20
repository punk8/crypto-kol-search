from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from kol_search.models import Candidate, ContactPoint, DiscoveryEdge, Post


SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    domain TEXT NOT NULL DEFAULT 'crypto',
    kind TEXT NOT NULL DEFAULT 'theme',
    query TEXT NOT NULL DEFAULT '',
    language TEXT NOT NULL DEFAULT 'all',
    account_type TEXT NOT NULL DEFAULT 'all',
    min_followers INTEGER NOT NULL DEFAULT 1000,
    result_limit INTEGER NOT NULL DEFAULT 100,
    backend TEXT NOT NULL,
    use_ai INTEGER NOT NULL DEFAULT 0,
    model TEXT,
    status TEXT NOT NULL DEFAULT 'queued',
    progress INTEGER NOT NULL DEFAULT 0,
    phase TEXT NOT NULL DEFAULT 'queued',
    started_at TEXT,
    finished_at TEXT,
    created_at TEXT NOT NULL,
    candidate_count INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    warnings_json TEXT NOT NULL DEFAULT '[]',
    config_json TEXT NOT NULL DEFAULT '{}',
    stats_json TEXT NOT NULL DEFAULT '{}',
    meta_json TEXT NOT NULL DEFAULT '{}',
    parent_run_id INTEGER
);

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    attempts INTEGER NOT NULL DEFAULT 0,
    queued_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    error TEXT,
    FOREIGN KEY (run_id) REFERENCES runs(id)
);

CREATE TABLE IF NOT EXISTS accounts (
    id TEXT PRIMARY KEY,
    username TEXT NOT NULL,
    name TEXT,
    description TEXT,
    followers_count INTEGER NOT NULL DEFAULT 0,
    following_count INTEGER NOT NULL DEFAULT 0,
    tweet_count INTEGER NOT NULL DEFAULT 0,
    listed_count INTEGER NOT NULL DEFAULT 0,
    verified INTEGER NOT NULL DEFAULT 0,
    protected INTEGER NOT NULL DEFAULT 0,
    created_at TEXT,
    profile_image_url TEXT,
    profile_url TEXT,
    location TEXT,
    entities_json TEXT NOT NULL DEFAULT '{}',
    account_type TEXT NOT NULL DEFAULT 'unknown',
    languages_json TEXT NOT NULL DEFAULT '[]',
    topics_json TEXT NOT NULL DEFAULT '[]',
    summary TEXT,
    raw_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_accounts_username_nocase ON accounts(username COLLATE NOCASE);

CREATE TABLE IF NOT EXISTS account_handles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT NOT NULL,
    username TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    UNIQUE(account_id, username COLLATE NOCASE),
    FOREIGN KEY (account_id) REFERENCES accounts(id)
);

CREATE TABLE IF NOT EXISTS posts (
    id TEXT PRIMARY KEY,
    author_id TEXT,
    author_username TEXT,
    text TEXT,
    created_at TEXT,
    like_count INTEGER NOT NULL DEFAULT 0,
    retweet_count INTEGER NOT NULL DEFAULT 0,
    reply_count INTEGER NOT NULL DEFAULT 0,
    quote_count INTEGER NOT NULL DEFAULT 0,
    view_count INTEGER NOT NULL DEFAULT 0,
    bookmark_count INTEGER NOT NULL DEFAULT 0,
    lang TEXT,
    url TEXT,
    conversation_id TEXT,
    in_reply_to_user_id TEXT,
    in_reply_to_username TEXT,
    referenced_post_id TEXT,
    reference_type TEXT,
    raw_json TEXT NOT NULL DEFAULT '{}',
    fetched_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS post_metric_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    like_count INTEGER NOT NULL DEFAULT 0,
    retweet_count INTEGER NOT NULL DEFAULT 0,
    reply_count INTEGER NOT NULL DEFAULT 0,
    quote_count INTEGER NOT NULL DEFAULT 0,
    view_count INTEGER NOT NULL DEFAULT 0,
    bookmark_count INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (post_id) REFERENCES posts(id)
);
CREATE INDEX IF NOT EXISTS idx_post_metrics_post_time
ON post_metric_snapshots(post_id, captured_at);

CREATE TABLE IF NOT EXISTS scan_checkpoints (
    source_key TEXT PRIMARY KEY,
    since_id TEXT,
    cursor TEXT,
    last_success_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS brand_profile (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    brand_name TEXT NOT NULL DEFAULT '',
    x_handle TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    audience TEXT NOT NULL DEFAULT '',
    tone TEXT NOT NULL DEFAULT 'professional, concise, conversational',
    allowed_claims_json TEXT NOT NULL DEFAULT '[]',
    forbidden_terms_json TEXT NOT NULL DEFAULT '[]',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS x_sender_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    label TEXT NOT NULL,
    x_handle TEXT NOT NULL COLLATE NOCASE UNIQUE,
    sender_type TEXT NOT NULL,
    send_method TEXT NOT NULL,
    opencli_profile TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    comment_auto_publish INTEGER NOT NULL DEFAULT 0,
    daily_comment_limit INTEGER NOT NULL DEFAULT 10,
    daily_dm_limit INTEGER NOT NULL DEFAULT 10,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS post_observations (
    run_id INTEGER NOT NULL,
    post_id TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_key TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    PRIMARY KEY (run_id, post_id, source_type, source_key),
    FOREIGN KEY (run_id) REFERENCES runs(id),
    FOREIGN KEY (post_id) REFERENCES posts(id)
);
CREATE INDEX IF NOT EXISTS idx_post_observations_post ON post_observations(post_id, observed_at);

CREATE TABLE IF NOT EXISTS reply_opportunities (
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
    outcome_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (post_id) REFERENCES posts(id),
    FOREIGN KEY (account_id) REFERENCES accounts(id)
);
CREATE INDEX IF NOT EXISTS idx_reply_opportunities_queue
ON reply_opportunities(status, score DESC, expires_at);

CREATE TABLE IF NOT EXISTS dm_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sender_account_id INTEGER NOT NULL,
    template_key TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',
    total_count INTEGER NOT NULL DEFAULT 0,
    sent_count INTEGER NOT NULL DEFAULT 0,
    failed_count INTEGER NOT NULL DEFAULT 0,
    skipped_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    cancelled_at TEXT,
    FOREIGN KEY (sender_account_id) REFERENCES x_sender_accounts(id)
);

CREATE TABLE IF NOT EXISTS dm_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL,
    account_id TEXT NOT NULL,
    recipient_x_user_id TEXT NOT NULL,
    recipient_handle TEXT NOT NULL,
    rendered_text TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempted_at TEXT,
    sent_at TEXT,
    provider TEXT,
    provider_receipt TEXT,
    sanitized_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(batch_id, account_id),
    FOREIGN KEY (batch_id) REFERENCES dm_batches(id),
    FOREIGN KEY (account_id) REFERENCES accounts(id)
);
CREATE INDEX IF NOT EXISTS idx_dm_messages_batch_status
ON dm_messages(batch_id, status, id);

CREATE TABLE IF NOT EXISTS reply_outcome_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    opportunity_id INTEGER NOT NULL,
    stage_hours INTEGER NOT NULL,
    captured_at TEXT NOT NULL,
    like_count INTEGER NOT NULL DEFAULT 0,
    retweet_count INTEGER NOT NULL DEFAULT 0,
    reply_count INTEGER NOT NULL DEFAULT 0,
    quote_count INTEGER NOT NULL DEFAULT 0,
    view_count INTEGER NOT NULL DEFAULT 0,
    author_responded INTEGER NOT NULL DEFAULT 0,
    payload_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE(opportunity_id, stage_hours),
    FOREIGN KEY (opportunity_id) REFERENCES reply_opportunities(id)
);

CREATE TABLE IF NOT EXISTS topic_clusters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    language TEXT NOT NULL DEFAULT 'unknown',
    lifecycle TEXT NOT NULL DEFAULT 'emerging',
    heat_score REAL NOT NULL DEFAULT 0,
    metrics_json TEXT NOT NULL DEFAULT '{}',
    outline TEXT NOT NULL DEFAULT '',
    draft TEXT NOT NULL DEFAULT '',
    draft_source TEXT NOT NULL DEFAULT 'rules',
    manually_edited INTEGER NOT NULL DEFAULT 0,
    editorial_status TEXT NOT NULL DEFAULT 'pending',
    native_trend INTEGER NOT NULL DEFAULT 0,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    published_at TEXT,
    published_url TEXT
);
CREATE INDEX IF NOT EXISTS idx_topic_clusters_radar
ON topic_clusters(editorial_status, heat_score DESC, last_seen_at DESC);

CREATE TABLE IF NOT EXISTS topic_cluster_posts (
    cluster_id INTEGER NOT NULL,
    post_id TEXT NOT NULL,
    relevance REAL NOT NULL DEFAULT 0,
    observed_at TEXT NOT NULL,
    PRIMARY KEY (cluster_id, post_id),
    FOREIGN KEY (cluster_id) REFERENCES topic_clusters(id),
    FOREIGN KEY (post_id) REFERENCES posts(id)
);

CREATE TABLE IF NOT EXISTS topic_metric_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cluster_id INTEGER NOT NULL,
    captured_at TEXT NOT NULL,
    post_count INTEGER NOT NULL DEFAULT 0,
    unique_authors INTEGER NOT NULL DEFAULT 0,
    engagement_total INTEGER NOT NULL DEFAULT 0,
    kol_count INTEGER NOT NULL DEFAULT 0,
    heat_score REAL NOT NULL DEFAULT 0,
    payload_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (cluster_id) REFERENCES topic_clusters(id)
);
CREATE INDEX IF NOT EXISTS idx_topic_metrics_cluster_time
ON topic_metric_snapshots(cluster_id, captured_at);

CREATE TABLE IF NOT EXISTS candidate_scores (
    run_id INTEGER NOT NULL,
    account_id TEXT NOT NULL,
    username TEXT NOT NULL,
    rank INTEGER,
    score_total REAL NOT NULL DEFAULT 0,
    domain_score REAL NOT NULL DEFAULT 0,
    is_seed INTEGER NOT NULL DEFAULT 0,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (run_id, account_id),
    FOREIGN KEY (run_id) REFERENCES runs(id),
    FOREIGN KEY (account_id) REFERENCES accounts(id)
);
CREATE INDEX IF NOT EXISTS idx_candidate_scores_run_rank ON candidate_scores(run_id, rank);

CREATE TABLE IF NOT EXISTS discovery_edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    target_username TEXT NOT NULL,
    source_type TEXT NOT NULL,
    from_username TEXT,
    evidence TEXT,
    query TEXT,
    weight REAL NOT NULL DEFAULT 1.0,
    FOREIGN KEY (run_id) REFERENCES runs(id)
);

CREATE TABLE IF NOT EXISTS topics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS account_topics (
    account_id TEXT NOT NULL,
    topic_id INTEGER NOT NULL,
    relevance REAL NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (account_id, topic_id),
    FOREIGN KEY (account_id) REFERENCES accounts(id),
    FOREIGN KEY (topic_id) REFERENCES topics(id)
);

CREATE TABLE IF NOT EXISTS contacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT NOT NULL,
    contact_type TEXT NOT NULL,
    value TEXT NOT NULL,
    url TEXT,
    source_url TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    evidence TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    UNIQUE(account_id, contact_type, value, source_url),
    FOREIGN KEY (account_id) REFERENCES accounts(id)
);
CREATE INDEX IF NOT EXISTS idx_contacts_account ON contacts(account_id, status);

CREATE TABLE IF NOT EXISTS crawl_cache (
    url TEXT PRIMARY KEY,
    status_code INTEGER NOT NULL,
    content_type TEXT,
    body TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ai_cache (
    cache_key TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS api_cache (
    cache_key TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS seed_sets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    version TEXT NOT NULL,
    target_size INTEGER NOT NULL,
    source_seed_set_id INTEGER,
    status TEXT NOT NULL DEFAULT 'draft',
    quotas_json TEXT NOT NULL DEFAULT '{}',
    config_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (source_seed_set_id) REFERENCES seed_sets(id)
);

CREATE TABLE IF NOT EXISTS seed_members (
    seed_set_id INTEGER NOT NULL,
    account_id TEXT NOT NULL,
    role TEXT NOT NULL,
    review_status TEXT NOT NULL,
    primary_topic TEXT NOT NULL,
    language_bucket TEXT NOT NULL,
    account_type_bucket TEXT NOT NULL,
    institution_kind TEXT,
    rank INTEGER,
    source_run_id INTEGER,
    network_seed_count INTEGER NOT NULL DEFAULT 0,
    score REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    reviewed_at TEXT,
    PRIMARY KEY (seed_set_id, account_id),
    FOREIGN KEY (seed_set_id) REFERENCES seed_sets(id),
    FOREIGN KEY (account_id) REFERENCES accounts(id),
    FOREIGN KEY (source_run_id) REFERENCES runs(id)
);
CREATE INDEX IF NOT EXISTS idx_seed_members_status ON seed_members(seed_set_id, review_status, rank);
"""


RUN_COLUMNS: dict[str, str] = {
    "kind": "TEXT NOT NULL DEFAULT 'theme'",
    "query": "TEXT NOT NULL DEFAULT ''",
    "language": "TEXT NOT NULL DEFAULT 'all'",
    "account_type": "TEXT NOT NULL DEFAULT 'all'",
    "min_followers": "INTEGER NOT NULL DEFAULT 1000",
    "result_limit": "INTEGER NOT NULL DEFAULT 100",
    "use_ai": "INTEGER NOT NULL DEFAULT 0",
    "model": "TEXT",
    "status": "TEXT NOT NULL DEFAULT 'queued'",
    "progress": "INTEGER NOT NULL DEFAULT 0",
    "phase": "TEXT NOT NULL DEFAULT 'queued'",
    "created_at": "TEXT",
    "error": "TEXT",
    "warnings_json": "TEXT NOT NULL DEFAULT '[]'",
    "config_json": "TEXT NOT NULL DEFAULT '{}'",
    "stats_json": "TEXT NOT NULL DEFAULT '{}'",
    "parent_run_id": "INTEGER",
}

ACCOUNT_COLUMNS: dict[str, str] = {
    "listed_count": "INTEGER NOT NULL DEFAULT 0",
    "protected": "INTEGER NOT NULL DEFAULT 0",
    "created_at": "TEXT",
    "profile_image_url": "TEXT",
    "profile_url": "TEXT",
    "location": "TEXT",
    "entities_json": "TEXT NOT NULL DEFAULT '{}'",
    "account_type": "TEXT NOT NULL DEFAULT 'unknown'",
    "languages_json": "TEXT NOT NULL DEFAULT '[]'",
    "topics_json": "TEXT NOT NULL DEFAULT '[]'",
    "summary": "TEXT",
}

POST_COLUMNS: dict[str, str] = {
    "lang": "TEXT",
    "fetched_at": "TEXT",
    "view_count": "INTEGER NOT NULL DEFAULT 0",
    "bookmark_count": "INTEGER NOT NULL DEFAULT 0",
    "url": "TEXT",
    "conversation_id": "TEXT",
    "in_reply_to_user_id": "TEXT",
    "in_reply_to_username": "TEXT",
    "referenced_post_id": "TEXT",
    "reference_type": "TEXT",
}

EDGE_COLUMNS: dict[str, str] = {
    "source_account_id": "TEXT",
    "target_account_id": "TEXT",
    "edge_type": "TEXT",
    "evidence_url": "TEXT",
    "first_seen_at": "TEXT",
    "last_seen_at": "TEXT",
}

REPLY_COLUMNS: dict[str, str] = {
    "sender_account_id": "INTEGER",
    "comment_style": "TEXT NOT NULL DEFAULT 'brand'",
    "publish_mode": "TEXT NOT NULL DEFAULT 'review'",
    "campaign_goal": "TEXT NOT NULL DEFAULT ''",
    "suitable": "INTEGER",
    "suitability_reason": "TEXT",
    "validation_status": "TEXT NOT NULL DEFAULT 'pending'",
    "validation_reason": "TEXT",
    "attempted_at": "TEXT",
    "published_at": "TEXT",
    "send_backend": "TEXT",
    "sanitized_error": "TEXT",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


class Store:
    """Thread-safe SQLite repository; every operation gets its own connection."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.migrate()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=10, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def migrate(self) -> None:
        with self.connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(SCHEMA)
            existing = {row[1] for row in connection.execute("PRAGMA table_info(runs)")}
            for name, definition in RUN_COLUMNS.items():
                if name not in existing:
                    connection.execute(f"ALTER TABLE runs ADD COLUMN {name} {definition}")
            account_existing = {row[1] for row in connection.execute("PRAGMA table_info(accounts)")}
            for name, definition in ACCOUNT_COLUMNS.items():
                if name not in account_existing:
                    connection.execute(f"ALTER TABLE accounts ADD COLUMN {name} {definition}")
            post_existing = {row[1] for row in connection.execute("PRAGMA table_info(posts)")}
            for name, definition in POST_COLUMNS.items():
                if name not in post_existing:
                    connection.execute(f"ALTER TABLE posts ADD COLUMN {name} {definition}")
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_posts_created_at ON posts(created_at)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_posts_conversation "
                "ON posts(conversation_id, created_at)"
            )
            edge_existing = {
                row[1] for row in connection.execute("PRAGMA table_info(discovery_edges)")
            }
            for name, definition in EDGE_COLUMNS.items():
                if name not in edge_existing:
                    connection.execute(
                        f"ALTER TABLE discovery_edges ADD COLUMN {name} {definition}"
                    )
            reply_existing = {
                row[1] for row in connection.execute("PRAGMA table_info(reply_opportunities)")
            }
            for name, definition in REPLY_COLUMNS.items():
                if name not in reply_existing:
                    connection.execute(
                        f"ALTER TABLE reply_opportunities ADD COLUMN {name} {definition}"
                    )
            connection.execute(
                "UPDATE runs SET created_at=COALESCE(created_at, started_at, ?) WHERE created_at IS NULL",
                (utc_now(),),
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(1, ?)",
                (utc_now(),),
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(2, ?)",
                (utc_now(),),
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(3, ?)",
                (utc_now(),),
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(4, ?)",
                (utc_now(),),
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(5, ?)",
                (utc_now(),),
            )

    def close(self) -> None:
        return None

    def create_run(
        self,
        *,
        query: str,
        backend: str,
        kind: str = "theme",
        language: str = "all",
        account_type: str = "all",
        min_followers: int = 1000,
        result_limit: int = 100,
        use_ai: bool = False,
        model: str | None = None,
        config: dict[str, Any] | None = None,
        parent_run_id: int | None = None,
    ) -> int:
        now = utc_now()
        with self.connection() as connection:
            cursor = connection.execute(
                """
                INSERT INTO runs (
                    domain, kind, query, language, account_type, min_followers, result_limit,
                    backend, use_ai, model, status, progress, phase, started_at, created_at, config_json,
                    parent_run_id, meta_json
                ) VALUES ('crypto', ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', 0, 'queued', ?, ?, ?, ?, '{}')
                """,
                (
                    kind,
                    query,
                    language,
                    account_type,
                    max(0, min_followers),
                    max(1, min(100, result_limit)),
                    backend,
                    int(use_ai),
                    model,
                    now,
                    now,
                    _json(config or {}),
                    parent_run_id,
                ),
            )
            run_id = int(cursor.lastrowid)
            connection.execute(
                "INSERT INTO jobs(run_id, status, queued_at) VALUES(?, 'queued', ?)",
                (run_id, now),
            )
        return run_id

    # Compatibility with the original research API.
    def start_run(self, domain: str, backend: str, meta: dict[str, Any] | None = None) -> int:
        return self.create_run(query=(meta or {}).get("query", domain), backend=backend, config=meta)

    def claim_next_job(self) -> dict[str, Any] | None:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM jobs WHERE status='queued' ORDER BY id LIMIT 1"
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            now = utc_now()
            connection.execute(
                "UPDATE jobs SET status='running', attempts=attempts+1, started_at=? WHERE id=?",
                (now, row["id"]),
            )
            connection.execute(
                "UPDATE runs SET status='running', phase='starting', progress=1, started_at=?, error=NULL WHERE id=?",
                (now, row["run_id"]),
            )
            connection.commit()
            return dict(row)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def interrupt_running_jobs(self) -> int:
        now = utc_now()
        with self.connection() as connection:
            rows = connection.execute("SELECT run_id FROM jobs WHERE status='running'").fetchall()
            run_ids = [int(row["run_id"]) for row in rows]
            connection.execute(
                "UPDATE jobs SET status='interrupted', finished_at=?, error='Application restarted' WHERE status='running'",
                (now,),
            )
            for run_id in run_ids:
                connection.execute(
                    "UPDATE runs SET status='interrupted', phase='interrupted', finished_at=?, error='应用重启，任务已中断' WHERE id=?",
                    (now, run_id),
                )
        return len(run_ids)

    def retry_run(self, run_id: int) -> int:
        original = self.get_run(run_id)
        if not original:
            raise KeyError(f"Run {run_id} does not exist")
        return self.create_run(
            query=original["query"],
            backend=original["backend"],
            kind=original["kind"],
            language=original["language"],
            account_type=original["account_type"],
            min_followers=original["min_followers"],
            result_limit=original["result_limit"],
            use_ai=bool(original["use_ai"]),
            model=original["model"],
            config=original.get("config", {}),
            parent_run_id=run_id,
        )

    def update_run(
        self,
        run_id: int,
        *,
        progress: int | None = None,
        phase: str | None = None,
        stats: dict[str, Any] | None = None,
        warnings: list[str] | None = None,
    ) -> None:
        fields: list[str] = []
        values: list[Any] = []
        if progress is not None:
            fields.append("progress=?")
            values.append(max(0, min(100, progress)))
        if phase is not None:
            fields.append("phase=?")
            values.append(phase)
        if stats is not None:
            fields.append("stats_json=?")
            values.append(_json(stats))
        if warnings is not None:
            fields.append("warnings_json=?")
            values.append(_json(warnings))
        if not fields:
            return
        values.append(run_id)
        with self.connection() as connection:
            connection.execute(f"UPDATE runs SET {', '.join(fields)} WHERE id=?", values)

    def finish_run(
        self,
        run_id: int,
        candidate_count: int,
        *,
        status: str = "completed",
        error: str | None = None,
        stats: dict[str, Any] | None = None,
        warnings: list[str] | None = None,
    ) -> None:
        now = utc_now()
        with self.connection() as connection:
            connection.execute(
                """
                UPDATE runs SET status=?, phase=?, progress=?, finished_at=?, candidate_count=?,
                    error=?, stats_json=?, warnings_json=? WHERE id=?
                """,
                (
                    status,
                    "done" if status.startswith("completed") else status,
                    100 if status.startswith("completed") else 0,
                    now,
                    candidate_count,
                    error,
                    _json(stats or {}),
                    _json(warnings or []),
                    run_id,
                ),
            )
            connection.execute(
                "UPDATE jobs SET status=?, finished_at=?, error=? WHERE run_id=? AND status='running'",
                (status, now, error, run_id),
            )

    def fail_run(self, run_id: int, error: str) -> None:
        self.finish_run(run_id, 0, status="failed", error=error)

    def resolve_account_id(self, username: str, proposed_id: str) -> str:
        """Reuse the canonical id already associated with a case-insensitive handle."""
        with self.connection() as connection:
            row = connection.execute(
                "SELECT id FROM accounts WHERE username=? COLLATE NOCASE",
                (username.lstrip("@"),),
            ).fetchone()
        return str(row["id"]) if row else str(proposed_id)

    def upsert_account(
        self, account_dict: dict[str, Any], enrichment: dict[str, Any] | None = None
    ) -> str:
        enrichment = enrichment or {}
        now = utc_now()
        effective_id = self.resolve_account_id(account_dict["username"], account_dict["id"])
        account_dict = dict(account_dict)
        account_dict["id"] = effective_id
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO accounts (
                    id, username, name, description, followers_count, following_count, tweet_count,
                    listed_count, verified, protected, created_at, profile_image_url, profile_url,
                    location, entities_json, account_type, languages_json, topics_json, summary,
                    raw_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    username=excluded.username, name=excluded.name, description=excluded.description,
                    followers_count=excluded.followers_count, following_count=excluded.following_count,
                    tweet_count=excluded.tweet_count, listed_count=excluded.listed_count,
                    verified=excluded.verified, protected=excluded.protected, created_at=excluded.created_at,
                    profile_image_url=excluded.profile_image_url, profile_url=excluded.profile_url,
                    location=excluded.location, entities_json=excluded.entities_json,
                    account_type=excluded.account_type, languages_json=excluded.languages_json,
                    topics_json=excluded.topics_json, summary=excluded.summary,
                    raw_json=excluded.raw_json, updated_at=excluded.updated_at
                """,
                (
                    account_dict["id"], account_dict["username"], account_dict.get("name"),
                    account_dict.get("description"), account_dict.get("followers_count", 0),
                    account_dict.get("following_count", 0), account_dict.get("tweet_count", 0),
                    account_dict.get("listed_count", 0), int(bool(account_dict.get("verified"))),
                    int(bool(account_dict.get("protected"))), account_dict.get("created_at"),
                    account_dict.get("profile_image_url"), account_dict.get("url"),
                    account_dict.get("location"), _json(account_dict.get("entities") or {}),
                    enrichment.get("account_type", "unknown"), _json(enrichment.get("languages", [])),
                    _json(enrichment.get("topics", [])), enrichment.get("summary"),
                    _json(account_dict.get("raw") or {}), now,
                ),
            )
            connection.execute(
                """
                INSERT INTO account_handles(account_id, username, first_seen_at, last_seen_at)
                VALUES(?, ?, ?, ?)
                ON CONFLICT(account_id, username) DO UPDATE SET last_seen_at=excluded.last_seen_at
                """,
                (account_dict["id"], account_dict["username"], now, now),
            )
        return effective_id

    def save_posts(self, posts: list[Post], *, captured_at: str | None = None) -> None:
        if not posts:
            return
        now = captured_at or utc_now()
        for post in posts:
            if post.author_username:
                post.author_id = self.resolve_account_id(post.author_username, post.author_id)
        with self.connection() as connection:
            connection.executemany(
                """
                INSERT INTO posts (
                    id, author_id, author_username, text, created_at, like_count, retweet_count,
                    reply_count, quote_count, view_count, bookmark_count, lang, url,
                    conversation_id, in_reply_to_user_id, in_reply_to_username,
                    referenced_post_id, reference_type, raw_json, fetched_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    text=excluded.text, like_count=excluded.like_count,
                    retweet_count=excluded.retweet_count, reply_count=excluded.reply_count,
                    quote_count=excluded.quote_count, view_count=excluded.view_count,
                    bookmark_count=excluded.bookmark_count, lang=excluded.lang,
                    url=COALESCE(excluded.url, posts.url),
                    conversation_id=COALESCE(excluded.conversation_id, posts.conversation_id),
                    in_reply_to_user_id=COALESCE(excluded.in_reply_to_user_id, posts.in_reply_to_user_id),
                    in_reply_to_username=COALESCE(excluded.in_reply_to_username, posts.in_reply_to_username),
                    referenced_post_id=COALESCE(excluded.referenced_post_id, posts.referenced_post_id),
                    reference_type=COALESCE(excluded.reference_type, posts.reference_type),
                    raw_json=excluded.raw_json, fetched_at=excluded.fetched_at
                """,
                [
                    (
                        post.id, post.author_id, post.author_username, post.text, post.created_at,
                        post.like_count, post.retweet_count, post.reply_count, post.quote_count,
                        post.view_count, post.bookmark_count, post.lang, post.url,
                        post.conversation_id, post.in_reply_to_user_id,
                        post.in_reply_to_username, post.referenced_post_id,
                        post.reference_type, _json(post.raw), now,
                    )
                    for post in posts if post.id
                ],
            )
            connection.executemany(
                """
                INSERT INTO post_metric_snapshots (
                    post_id, captured_at, like_count, retweet_count, reply_count,
                    quote_count, view_count, bookmark_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        post.id, now, post.like_count, post.retweet_count,
                        post.reply_count, post.quote_count, post.view_count,
                        post.bookmark_count,
                    )
                    for post in posts if post.id
                ],
            )

    def get_post_metric_history(self, post_id: str, limit: int = 100) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM post_metric_snapshots
                WHERE post_id=?
                ORDER BY captured_at DESC, id DESC
                LIMIT ?
                """,
                (post_id, max(1, min(limit, 1000))),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def upsert_scan_checkpoint(
        self,
        source_key: str,
        *,
        since_id: str | None = None,
        cursor: str | None = None,
        metadata: dict[str, Any] | None = None,
        succeeded_at: str | None = None,
    ) -> None:
        now = succeeded_at or utc_now()
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO scan_checkpoints (
                    source_key, since_id, cursor, last_success_at, metadata_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_key) DO UPDATE SET
                    since_id=excluded.since_id, cursor=excluded.cursor,
                    last_success_at=excluded.last_success_at,
                    metadata_json=excluded.metadata_json, updated_at=excluded.updated_at
                """,
                (source_key, since_id, cursor, now, _json(metadata or {}), now),
            )

    def get_scan_checkpoint(self, source_key: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM scan_checkpoints WHERE source_key=?",
                (source_key,),
            ).fetchone()
        if not row:
            return None
        output = dict(row)
        try:
            output["metadata"] = json.loads(output.pop("metadata_json") or "{}")
        except json.JSONDecodeError:
            output["metadata"] = {}
        return output

    def save_candidates(self, run_id: int, candidates: list[Candidate]) -> None:
        with self.connection() as connection:
            connection.execute("DELETE FROM candidate_scores WHERE run_id=?", (run_id,))
            connection.execute("DELETE FROM discovery_edges WHERE run_id=?", (run_id,))
        for candidate in candidates:
            candidate.account.id = self.upsert_account(
                candidate.account.model_dump(), candidate.enrichment.model_dump()
            )
        with self.connection() as connection:
            for candidate in candidates:
                connection.execute(
                    """
                    INSERT INTO candidate_scores (
                        run_id, account_id, username, rank, score_total, domain_score, is_seed, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id, candidate.account.id, candidate.account.username, candidate.rank,
                        candidate.score.total, candidate.domain_score, int(candidate.is_seed),
                        candidate.model_dump_json(),
                    ),
                )
                for edge in candidate.edges:
                    self._insert_edge(connection, run_id, edge)

    @staticmethod
    def _insert_edge(connection: sqlite3.Connection, run_id: int, edge: DiscoveryEdge) -> None:
        connection.execute(
            """
            INSERT INTO discovery_edges (
                run_id, target_username, source_type, from_username, evidence, query, weight
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (run_id, edge.target_username, edge.source_type, edge.from_username, edge.evidence, edge.query, edge.weight),
        )

    def save_contacts(self, contacts: list[ContactPoint]) -> None:
        now = utc_now()
        with self.connection() as connection:
            for contact in contacts:
                connection.execute(
                    """
                    INSERT INTO contacts (
                        account_id, contact_type, value, url, source_url, source_kind, evidence,
                        status, first_seen_at, last_seen_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(account_id, contact_type, value, source_url) DO UPDATE SET
                        url=excluded.url, evidence=excluded.evidence, last_seen_at=excluded.last_seen_at
                    """,
                    (
                        contact.account_id, contact.contact_type, contact.value, contact.url,
                        contact.source_url, contact.source_kind, contact.evidence,
                        contact.status, contact.first_seen_at or now, now,
                    ),
                )

    def update_contact_status(self, contact_id: int, status: str) -> None:
        if status not in {"pending", "verified", "rejected"}:
            raise ValueError("Invalid contact status")
        with self.connection() as connection:
            cursor = connection.execute("UPDATE contacts SET status=? WHERE id=?", (status, contact_id))
            if cursor.rowcount == 0:
                raise KeyError(contact_id)

    def cache_get(self, key: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute("SELECT payload_json FROM ai_cache WHERE cache_key=?", (key,)).fetchone()
        return json.loads(row["payload_json"]) if row else None

    def cache_set(self, key: str, payload: dict[str, Any]) -> None:
        with self.connection() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO ai_cache(cache_key, payload_json, created_at) VALUES(?, ?, ?)",
                (key, _json(payload), utc_now()),
            )

    def get_api_cache(self, key: str, max_age_seconds: int) -> Any | None:
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT payload_json FROM api_cache
                WHERE cache_key=?
                  AND (julianday('now') - julianday(fetched_at)) * 86400 <= ?
                """,
                (key, max_age_seconds),
            ).fetchone()
        if not row:
            return None
        try:
            return json.loads(row["payload_json"])
        except json.JSONDecodeError:
            return None

    def set_api_cache(self, key: str, payload: Any) -> None:
        with self.connection() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO api_cache(cache_key, payload_json, fetched_at)
                VALUES(?, ?, ?)
                """,
                (key, _json(payload), utc_now()),
            )

    def get_crawl_cache(self, url: str, max_age_seconds: int = 604800) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM crawl_cache
                WHERE url=? AND (julianday('now') - julianday(fetched_at)) * 86400 <= ?
                """,
                (url, max_age_seconds),
            ).fetchone()
        return dict(row) if row else None

    def set_crawl_cache(self, url: str, status_code: int, content_type: str, body: str) -> None:
        with self.connection() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO crawl_cache(url, status_code, content_type, body, fetched_at)
                VALUES(?, ?, ?, ?, ?)
                """,
                (url, status_code, content_type, body, utc_now()),
            )

    @staticmethod
    def _decode_run(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        value = dict(row)
        for key, output in (("warnings_json", "warnings"), ("config_json", "config"), ("stats_json", "stats"), ("meta_json", "meta")):
            try:
                value[output] = json.loads(value.get(key) or ("[]" if "warnings" in key else "{}"))
            except json.JSONDecodeError:
                value[output] = [] if "warnings" in key else {}
        return value

    def get_run(self, run_id: int) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        return self._decode_run(row)

    def list_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._decode_run(row) or {} for row in rows]

    def has_active_run(self, kind: str) -> bool:
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM runs
                WHERE kind=? AND status IN ('queued', 'running')
                LIMIT 1
                """,
                (kind,),
            ).fetchone()
        return row is not None

    def get_run_results(self, run_id: int, account_type: str = "all") -> list[dict[str, Any]]:
        sql = """
            SELECT cs.*, a.name, a.description, a.followers_count, a.verified,
                   a.account_type, a.languages_json, a.topics_json,
                   (SELECT COUNT(*) FROM contacts c WHERE c.account_id=a.id AND c.status!='rejected') AS contact_count
            FROM candidate_scores cs JOIN accounts a ON a.id=cs.account_id
            WHERE cs.run_id=?
        """
        params: list[Any] = [run_id]
        if account_type != "all":
            if account_type == "organization":
                sql += " AND a.account_type!='person'"
            else:
                sql += " AND a.account_type=?"
                params.append(account_type)
        sql += " ORDER BY cs.rank IS NULL, cs.rank, cs.score_total DESC"
        with self.connection() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def list_library(
        self,
        *,
        account_type: str = "person",
        language: str = "all",
        min_followers: int = 0,
        contact_status: str = "all",
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        sql = """
            SELECT a.*,
                (SELECT MAX(cs.score_total) FROM candidate_scores cs WHERE cs.account_id=a.id) AS best_score,
                (SELECT COUNT(*) FROM contacts c WHERE c.account_id=a.id AND c.status!='rejected') AS contact_count
            FROM accounts a WHERE a.followers_count>=?
        """
        params: list[Any] = [min_followers]
        if account_type != "all":
            if account_type == "organization":
                sql += " AND a.account_type!='person'"
            else:
                sql += " AND a.account_type=?"
                params.append(account_type)
        if language != "all":
            sql += " AND a.languages_json LIKE ?"
            params.append(f'%"{language}"%')
        if contact_status != "all":
            sql += " AND EXISTS (SELECT 1 FROM contacts c WHERE c.account_id=a.id AND c.status=?)"
            params.append(contact_status)
        sql += " ORDER BY best_score DESC, a.followers_count DESC LIMIT ?"
        params.append(limit)
        with self.connection() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def get_account(self, account_id: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
            if not row:
                return None
            value = dict(row)
            value["contacts"] = [dict(item) for item in connection.execute(
                """
                SELECT *, CASE WHEN (julianday('now') - julianday(last_seen_at)) > 30 THEN 1 ELSE 0 END AS stale
                FROM contacts WHERE account_id=? ORDER BY status, contact_type
                """,
                (account_id,),
            ).fetchall()]
            value["posts"] = [dict(item) for item in connection.execute(
                "SELECT * FROM posts WHERE author_id=? ORDER BY created_at DESC LIMIT 20", (account_id,)
            ).fetchall()]
            value["scores"] = [dict(item) for item in connection.execute(
                "SELECT * FROM candidate_scores WHERE account_id=? ORDER BY run_id DESC LIMIT 20", (account_id,)
            ).fetchall()]
            value["handles"] = [dict(item) for item in connection.execute(
                "SELECT * FROM account_handles WHERE account_id=? ORDER BY last_seen_at DESC", (account_id,)
            ).fetchall()]
            value["outreach"] = self.account_outreach_history(account_id)
        return value

    def contacts_for_accounts(self, account_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
        if not account_ids:
            return {}
        placeholders = ",".join("?" for _ in account_ids)
        with self.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT *, CASE WHEN (julianday('now') - julianday(last_seen_at)) > 30 THEN 1 ELSE 0 END AS stale
                FROM contacts WHERE account_id IN ({placeholders}) ORDER BY id
                """,
                account_ids,
            ).fetchall()
        result: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            result.setdefault(row["account_id"], []).append(dict(row))
        return result

    def create_seed_set(
        self,
        *,
        name: str,
        version: str,
        target_size: int,
        quotas: dict[str, Any],
        source_seed_set_id: int | None = None,
        status: str = "draft",
        config: dict[str, Any] | None = None,
    ) -> int:
        if status not in {"draft", "incomplete", "ready", "archived"}:
            raise ValueError("Invalid seed set status")
        now = utc_now()
        with self.connection() as connection:
            cursor = connection.execute(
                """
                INSERT INTO seed_sets(
                    name, version, target_size, source_seed_set_id, status,
                    quotas_json, config_json, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    name,
                    version,
                    target_size,
                    source_seed_set_id,
                    status,
                    _json(quotas),
                    _json(config or {}),
                    now,
                    now,
                ),
            )
            return int(cursor.lastrowid)

    def update_seed_set_status(self, seed_set_id: int, status: str) -> None:
        if status not in {"draft", "incomplete", "ready", "archived"}:
            raise ValueError("Invalid seed set status")
        with self.connection() as connection:
            cursor = connection.execute(
                "UPDATE seed_sets SET status=?, updated_at=? WHERE id=?",
                (status, utc_now(), seed_set_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(seed_set_id)

    def upsert_seed_member(
        self,
        seed_set_id: int,
        account_id: str,
        *,
        role: str,
        review_status: str,
        primary_topic: str,
        language_bucket: str,
        account_type_bucket: str,
        institution_kind: str | None = None,
        rank: int | None = None,
        source_run_id: int | None = None,
        network_seed_count: int = 0,
        score: float = 0,
    ) -> None:
        if role not in {"base", "expanded"}:
            raise ValueError("Invalid seed role")
        if review_status not in {"approved", "pending", "rejected"}:
            raise ValueError("Invalid seed review status")
        now = utc_now()
        reviewed_at = now if review_status in {"approved", "rejected"} else None
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO seed_members(
                    seed_set_id, account_id, role, review_status, primary_topic,
                    language_bucket, account_type_bucket, institution_kind, rank,
                    source_run_id, network_seed_count, score, created_at, reviewed_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(seed_set_id, account_id) DO UPDATE SET
                    role=excluded.role, review_status=excluded.review_status,
                    primary_topic=excluded.primary_topic,
                    language_bucket=excluded.language_bucket,
                    account_type_bucket=excluded.account_type_bucket,
                    institution_kind=excluded.institution_kind, rank=excluded.rank,
                    source_run_id=excluded.source_run_id,
                    network_seed_count=excluded.network_seed_count,
                    score=excluded.score,
                    reviewed_at=COALESCE(excluded.reviewed_at, seed_members.reviewed_at)
                """,
                (
                    seed_set_id,
                    account_id,
                    role,
                    review_status,
                    primary_topic,
                    language_bucket,
                    account_type_bucket,
                    institution_kind,
                    rank,
                    source_run_id,
                    network_seed_count,
                    score,
                    now,
                    reviewed_at,
                ),
            )

    def update_seed_member_status(
        self, seed_set_id: int, account_id: str, status: str
    ) -> None:
        if status not in {"approved", "pending", "rejected"}:
            raise ValueError("Invalid seed review status")
        with self.connection() as connection:
            cursor = connection.execute(
                """
                UPDATE seed_members SET review_status=?, reviewed_at=?
                WHERE seed_set_id=? AND account_id=?
                """,
                (status, utc_now(), seed_set_id, account_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(account_id)

    def save_seed_edge(
        self,
        *,
        run_id: int,
        source_account_id: str | None,
        target_account_id: str,
        target_username: str,
        edge_type: str,
        weight: float,
        evidence: str,
        evidence_url: str | None = None,
        from_username: str | None = None,
        query: str | None = None,
    ) -> None:
        now = utc_now()
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO discovery_edges(
                    run_id, target_username, source_type, from_username, evidence,
                    query, weight, source_account_id, target_account_id, edge_type,
                    evidence_url, first_seen_at, last_seen_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    target_username,
                    edge_type,
                    from_username,
                    evidence,
                    query,
                    weight,
                    source_account_id,
                    target_account_id,
                    edge_type,
                    evidence_url,
                    now,
                    now,
                ),
            )

    def list_seed_sets(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT s.*,
                    COUNT(m.account_id) AS member_count,
                    SUM(CASE WHEN m.review_status='approved' THEN 1 ELSE 0 END) AS approved_count,
                    SUM(CASE WHEN m.review_status='pending' THEN 1 ELSE 0 END) AS pending_count,
                    SUM(CASE WHEN m.review_status='rejected' THEN 1 ELSE 0 END) AS rejected_count
                FROM seed_sets s LEFT JOIN seed_members m ON m.seed_set_id=s.id
                GROUP BY s.id ORDER BY s.id DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._decode_seed_set(dict(row)) for row in rows]

    @staticmethod
    def _decode_seed_set(value: dict[str, Any]) -> dict[str, Any]:
        for source, target in (("quotas_json", "quotas"), ("config_json", "config")):
            try:
                value[target] = json.loads(value.get(source) or "{}")
            except json.JSONDecodeError:
                value[target] = {}
        for key in ("member_count", "approved_count", "pending_count", "rejected_count"):
            value[key] = int(value.get(key) or 0)
        return value

    def get_seed_set(self, seed_set_id: int) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT s.*,
                    COUNT(m.account_id) AS member_count,
                    SUM(CASE WHEN m.review_status='approved' THEN 1 ELSE 0 END) AS approved_count,
                    SUM(CASE WHEN m.review_status='pending' THEN 1 ELSE 0 END) AS pending_count,
                    SUM(CASE WHEN m.review_status='rejected' THEN 1 ELSE 0 END) AS rejected_count
                FROM seed_sets s LEFT JOIN seed_members m ON m.seed_set_id=s.id
                WHERE s.id=? GROUP BY s.id
                """,
                (seed_set_id,),
            ).fetchone()
        return self._decode_seed_set(dict(row)) if row else None

    def list_seed_members(self, seed_set_id: int) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT m.*, a.username, a.name, a.description, a.followers_count,
                       a.verified, a.protected, a.account_type, a.languages_json,
                       a.topics_json,
                       (SELECT COUNT(DISTINCT e.source_account_id)
                        FROM discovery_edges e
                        WHERE e.target_account_id=m.account_id
                          AND e.run_id=m.source_run_id) AS common_seed_count,
                       (SELECT GROUP_CONCAT(DISTINCT e.edge_type)
                        FROM discovery_edges e
                        WHERE e.target_account_id=m.account_id
                          AND e.run_id=m.source_run_id) AS discovery_paths
                FROM seed_members m JOIN accounts a ON a.id=m.account_id
                WHERE m.seed_set_id=?
                ORDER BY m.rank IS NULL, m.rank, m.score DESC
                """,
                (seed_set_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def save_x_sender(
        self,
        *,
        label: str,
        x_handle: str,
        sender_type: str,
        send_method: str,
        opencli_profile: str | None = None,
        enabled: bool = True,
        comment_auto_publish: bool = False,
        daily_comment_limit: int = 10,
        daily_dm_limit: int = 10,
        sender_id: int | None = None,
    ) -> int:
        allowed_types = {"brand", "founder", "employee", "creator_partner", "community"}
        allowed_methods = {"opencli_reply", "official_x_dm", "mock", "disabled"}
        handle = x_handle.strip().lstrip("@")
        if not label.strip() or not handle:
            raise ValueError("Sender label and X handle are required")
        if sender_type not in allowed_types or send_method not in allowed_methods:
            raise ValueError("Invalid sender type or send method")
        if send_method == "opencli_reply" and not (opencli_profile or "").strip():
            raise ValueError("OpenCLI profile is required for OpenCLI senders")
        now = utc_now()
        with self.connection() as connection:
            if sender_id is None:
                cursor = connection.execute(
                    """
                    INSERT INTO x_sender_accounts(
                        label, x_handle, sender_type, send_method, opencli_profile,
                        enabled, comment_auto_publish, daily_comment_limit,
                        daily_dm_limit, created_at, updated_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        label.strip(), handle, sender_type, send_method,
                        (opencli_profile or "").strip() or None, int(enabled),
                        int(comment_auto_publish), max(0, daily_comment_limit),
                        max(0, daily_dm_limit), now, now,
                    ),
                )
                return int(cursor.lastrowid)
            cursor = connection.execute(
                """
                UPDATE x_sender_accounts SET label=?, x_handle=?, sender_type=?,
                    send_method=?, opencli_profile=?, enabled=?, comment_auto_publish=?,
                    daily_comment_limit=?, daily_dm_limit=?, updated_at=?
                WHERE id=?
                """,
                (
                    label.strip(), handle, sender_type, send_method,
                    (opencli_profile or "").strip() or None, int(enabled),
                    int(comment_auto_publish), max(0, daily_comment_limit),
                    max(0, daily_dm_limit), now, sender_id,
                ),
            )
            if cursor.rowcount == 0:
                raise KeyError(sender_id)
            return sender_id

    def list_x_senders(self, *, enabled_only: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM x_sender_accounts"
        if enabled_only:
            sql += " WHERE enabled=1"
        sql += " ORDER BY enabled DESC, label COLLATE NOCASE"
        with self.connection() as connection:
            rows = connection.execute(sql).fetchall()
        return [dict(row) for row in rows]

    def get_x_sender(self, sender_id: int) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM x_sender_accounts WHERE id=?", (sender_id,)
            ).fetchone()
        return dict(row) if row else None

    @staticmethod
    def _refresh_dm_batch_counts(connection: sqlite3.Connection, batch_id: int) -> None:
        counts = connection.execute(
            """
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN status='sent'
                             AND COALESCE(provider, '') NOT IN ('dry_run', 'mock')
                            THEN 1 ELSE 0 END) AS sent,
                   SUM(CASE WHEN status IN ('failed', 'confirmation_required') THEN 1 ELSE 0 END) AS failed,
                   SUM(CASE WHEN status='skipped'
                                  OR (status='sent' AND provider IN ('dry_run', 'mock'))
                            THEN 1 ELSE 0 END) AS skipped,
                   SUM(CASE WHEN status IN ('pending', 'sending') THEN 1 ELSE 0 END) AS active
            FROM dm_messages WHERE batch_id=?
            """,
            (batch_id,),
        ).fetchone()
        completed_at = utc_now() if counts["active"] == 0 else None
        connection.execute(
            """
            UPDATE dm_batches SET total_count=?, sent_count=?, failed_count=?, skipped_count=?,
                status=CASE WHEN ? IS NOT NULL AND status='running' THEN 'completed' ELSE status END,
                completed_at=CASE WHEN ? IS NOT NULL AND status='running' THEN ? ELSE completed_at END
            WHERE id=?
            """,
            (
                counts["total"] or 0, counts["sent"] or 0, counts["failed"] or 0,
                counts["skipped"] or 0, completed_at, completed_at, completed_at, batch_id,
            ),
        )

    def create_dm_batch(
        self,
        *,
        sender_account_id: int,
        template_key: str,
        messages: list[dict[str, str]],
    ) -> int:
        sender = self.get_x_sender(sender_account_id)
        if not sender or not sender["enabled"]:
            raise ValueError("An enabled sender account is required")
        unique: dict[str, dict[str, str]] = {}
        for message in messages:
            unique.setdefault(str(message["account_id"]), message)
        if not unique:
            raise ValueError("At least one recipient is required")
        now = utc_now()
        with self.connection() as connection:
            cursor = connection.execute(
                """
                INSERT INTO dm_batches(sender_account_id, template_key, status, total_count, created_at)
                VALUES(?, ?, 'ready', ?, ?)
                """,
                (sender_account_id, template_key, len(unique), now),
            )
            batch_id = int(cursor.lastrowid)
            connection.executemany(
                """
                INSERT INTO dm_messages(
                    batch_id, account_id, recipient_x_user_id, recipient_handle,
                    rendered_text, status, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                [
                    (
                        batch_id, value["account_id"], value["recipient_x_user_id"],
                        value["recipient_handle"], value["rendered_text"], now, now,
                    )
                    for value in unique.values()
                ],
            )
        return batch_id

    def list_dm_batches(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT b.*, s.label AS sender_label, s.x_handle AS sender_handle,
                       s.send_method
                FROM dm_batches b JOIN x_sender_accounts s ON s.id=b.sender_account_id
                ORDER BY b.id DESC LIMIT ?
                """,
                (max(1, min(limit, 500)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_dm_batch(self, batch_id: int) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT b.*, s.label AS sender_label, s.x_handle AS sender_handle,
                       s.send_method, s.daily_dm_limit, s.enabled AS sender_enabled
                FROM dm_batches b JOIN x_sender_accounts s ON s.id=b.sender_account_id
                WHERE b.id=?
                """,
                (batch_id,),
            ).fetchone()
        return dict(row) if row else None

    def list_dm_messages(self, batch_id: int) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT m.*, a.name, a.description
                FROM dm_messages m JOIN accounts a ON a.id=m.account_id
                WHERE m.batch_id=? ORDER BY m.id
                """,
                (batch_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def remove_dm_message(self, batch_id: int, message_id: int) -> None:
        with self.connection() as connection:
            cursor = connection.execute(
                "DELETE FROM dm_messages WHERE id=? AND batch_id=? AND status='pending'",
                (message_id, batch_id),
            )
            if cursor.rowcount == 0:
                raise ValueError("Only pending recipients can be removed")
            self._refresh_dm_batch_counts(connection, batch_id)

    def set_dm_batch_status(self, batch_id: int, action: str) -> None:
        transitions = {
            "start": ({"ready", "paused"}, "running"),
            "resume": ({"paused"}, "running"),
            "pause": ({"running"}, "paused"),
            "cancel": ({"ready", "running", "paused"}, "cancelled"),
        }
        if action not in transitions:
            raise ValueError("Invalid batch action")
        allowed, target = transitions[action]
        now = utc_now()
        with self.connection() as connection:
            row = connection.execute("SELECT status FROM dm_batches WHERE id=?", (batch_id,)).fetchone()
            if not row:
                raise KeyError(batch_id)
            if row["status"] not in allowed:
                raise ValueError(f"Cannot {action} batch in {row['status']} state")
            connection.execute(
                """
                UPDATE dm_batches SET status=?,
                    started_at=CASE WHEN ?='running' THEN COALESCE(started_at, ?) ELSE started_at END,
                    cancelled_at=CASE WHEN ?='cancelled' THEN ? ELSE cancelled_at END
                WHERE id=?
                """,
                (target, target, now, target, now, batch_id),
            )
            if target == "cancelled":
                connection.execute(
                    "UPDATE dm_messages SET status='skipped', updated_at=? WHERE batch_id=? AND status='pending'",
                    (now, batch_id),
                )
            self._refresh_dm_batch_counts(connection, batch_id)

    def claim_next_dm_message(self, *, dm_window_days: int = 30) -> dict[str, Any] | None:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT m.*, b.sender_account_id, b.template_key,
                       s.x_handle AS sender_handle, s.send_method,
                       s.daily_dm_limit, s.enabled AS sender_enabled
                FROM dm_messages m
                JOIN dm_batches b ON b.id=m.batch_id
                JOIN x_sender_accounts s ON s.id=b.sender_account_id
                WHERE b.status='running' AND m.status='pending'
                ORDER BY b.id, m.id LIMIT 1
                """
            ).fetchone()
            if not row:
                connection.commit()
                return None
            now = utc_now()
            if row["send_method"] != "mock":
                daily = connection.execute(
                    """
                    SELECT COUNT(*) AS count FROM dm_messages m
                    JOIN dm_batches b ON b.id=m.batch_id
                    WHERE b.sender_account_id=?
                      AND m.status IN ('sending', 'sent', 'confirmation_required')
                      AND COALESCE(m.provider, '') NOT IN ('dry_run', 'mock')
                      AND date(COALESCE(m.sent_at, m.attempted_at))=date('now')
                    """,
                    (row["sender_account_id"],),
                ).fetchone()
                block_reason = self._dm_block_reason_in_connection(
                    connection,
                    str(row["account_id"]),
                    str(row["template_key"]),
                    window_days=dm_window_days,
                    exclude_dm_message_id=int(row["id"]),
                )
                if int(daily["count"] or 0) >= int(row["daily_dm_limit"]):
                    block_reason = "Sender daily DM limit reached"
                if block_reason:
                    connection.execute(
                        """
                        UPDATE dm_messages SET status='skipped', provider='guard',
                            attempted_at=?, sanitized_error=?, updated_at=? WHERE id=?
                        """,
                        (now, block_reason, now, row["id"]),
                    )
                    self._refresh_dm_batch_counts(connection, int(row["batch_id"]))
                    connection.commit()
                    result = dict(row)
                    result.update(status="skipped", attempted_at=now, provider="guard")
                    return result
            connection.execute(
                "UPDATE dm_messages SET status='sending', attempted_at=?, updated_at=? WHERE id=?",
                (now, now, row["id"]),
            )
            connection.commit()
            result = dict(row)
            result["status"] = "sending"
            result["attempted_at"] = now
            return result
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def interrupt_sending_dm_messages(self) -> int:
        now = utc_now()
        with self.connection() as connection:
            cursor = connection.execute(
                """
                UPDATE dm_messages SET status='confirmation_required',
                    sanitized_error='Worker stopped during send; verify manually and do not retry',
                    updated_at=? WHERE status='sending'
                """,
                (now,),
            )
            batch_ids = [
                row["batch_id"]
                for row in connection.execute(
                    "SELECT DISTINCT batch_id FROM dm_messages WHERE status='confirmation_required'"
                ).fetchall()
            ]
            for batch_id in batch_ids:
                self._refresh_dm_batch_counts(connection, int(batch_id))
        return cursor.rowcount

    def finish_dm_message(
        self,
        message_id: int,
        *,
        status: str,
        provider: str,
        provider_receipt: str | None = None,
        sanitized_error: str | None = None,
    ) -> None:
        if status not in {"sent", "failed", "skipped", "confirmation_required"}:
            raise ValueError("Invalid DM result status")
        now = utc_now()
        with self.connection() as connection:
            row = connection.execute(
                "SELECT batch_id FROM dm_messages WHERE id=?", (message_id,)
            ).fetchone()
            if not row:
                raise KeyError(message_id)
            connection.execute(
                """
                UPDATE dm_messages SET status=?, provider=?, provider_receipt=?,
                    sanitized_error=?, sent_at=CASE WHEN ?='sent' THEN ? ELSE sent_at END,
                    updated_at=? WHERE id=?
                """,
                (
                    status, provider, provider_receipt, sanitized_error,
                    status, now, now, message_id,
                ),
            )
            self._refresh_dm_batch_counts(connection, int(row["batch_id"]))

    @staticmethod
    def _dm_block_reason_in_connection(
        connection: sqlite3.Connection,
        account_id: str,
        template_key: str,
        *,
        window_days: int,
        exclude_dm_message_id: int | None,
    ) -> str | None:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max(1, window_days))).isoformat()
        active = connection.execute(
            """
            SELECT id FROM dm_messages
            WHERE account_id=? AND id!=COALESCE(?, -1)
              AND status IN ('sending', 'confirmation_required')
              AND COALESCE(provider, '') NOT IN ('dry_run', 'mock')
            LIMIT 1
            """,
            (account_id, exclude_dm_message_id),
        ).fetchone()
        if active:
            return "KOL already has active DM outreach"
        active_comment = connection.execute(
            """
            SELECT id FROM reply_opportunities
            WHERE account_id=? AND status IN ('publishing', 'confirmation_required')
              AND COALESCE(send_backend, '')!='mock'
            LIMIT 1
            """,
            (account_id,),
        ).fetchone()
        if active_comment:
            return "KOL already has active comment outreach"
        recent = connection.execute(
            """
            SELECT m.id FROM dm_messages m
            JOIN dm_batches b ON b.id=m.batch_id
            WHERE m.account_id=? AND m.id!=COALESCE(?, -1)
              AND b.template_key=?
              AND m.status IN ('sent', 'confirmation_required')
              AND COALESCE(m.provider, '') NOT IN ('dry_run', 'mock')
              AND COALESCE(m.sent_at, m.attempted_at)>=?
            LIMIT 1
            """,
            (account_id, exclude_dm_message_id, template_key, cutoff),
        ).fetchone()
        return "Same DM template was already sent to this KOL recently" if recent else None

    def dm_block_reason(
        self,
        account_id: str,
        *,
        template_key: str,
        window_days: int = 30,
        exclude_dm_message_id: int | None = None,
    ) -> str | None:
        with self.connection() as connection:
            return self._dm_block_reason_in_connection(
                connection,
                account_id,
                template_key,
                window_days=window_days,
                exclude_dm_message_id=exclude_dm_message_id,
            )

    @staticmethod
    def _comment_block_reason_in_connection(
        connection: sqlite3.Connection,
        account_id: str,
        *,
        window_days: int,
        exclude_reply_id: int | None,
    ) -> str | None:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max(1, window_days))).isoformat()
        active_dm = connection.execute(
            """
            SELECT id FROM dm_messages
            WHERE account_id=? AND status IN ('sending', 'confirmation_required')
              AND COALESCE(provider, '') NOT IN ('dry_run', 'mock')
            LIMIT 1
            """,
            (account_id,),
        ).fetchone()
        if active_dm:
            return "KOL already has active DM outreach"
        recent_comment = connection.execute(
            """
            SELECT id FROM reply_opportunities
            WHERE account_id=? AND id!=COALESCE(?, -1)
              AND status IN ('publishing', 'replied', 'responded', 'confirmation_required')
              AND COALESCE(send_backend, '')!='mock'
              AND COALESCE(published_at, attempted_at, replied_at)>=?
            LIMIT 1
            """,
            (account_id, exclude_reply_id, cutoff),
        ).fetchone()
        return "KOL already has recent promotional comment outreach" if recent_comment else None

    def comment_block_reason(
        self,
        account_id: str,
        *,
        window_days: int = 7,
        exclude_reply_id: int | None = None,
    ) -> str | None:
        with self.connection() as connection:
            return self._comment_block_reason_in_connection(
                connection,
                account_id,
                window_days=window_days,
                exclude_reply_id=exclude_reply_id,
            )

    def cross_channel_outreach_warning(
        self, account_id: str, *, target_channel: str, window_days: int = 30
    ) -> str | None:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max(1, window_days))).isoformat()
        with self.connection() as connection:
            if target_channel == "dm":
                row = connection.execute(
                    """
                    SELECT id FROM reply_opportunities
                    WHERE account_id=? AND status IN ('replied', 'responded', 'confirmation_required')
                      AND COALESCE(send_backend, '')!='mock'
                      AND COALESCE(published_at, attempted_at, replied_at)>=? LIMIT 1
                    """,
                    (account_id, cutoff),
                ).fetchone()
                return "Warning: this KOL has recent Comment outreach" if row else None
            row = connection.execute(
                """
                SELECT id FROM dm_messages
                WHERE account_id=? AND status IN ('sent', 'confirmation_required')
                  AND COALESCE(provider, '') NOT IN ('dry_run', 'mock')
                  AND COALESCE(sent_at, attempted_at)>=? LIMIT 1
                """,
                (account_id, cutoff),
            ).fetchone()
        return "Warning: this KOL has recent DM outreach" if row else None

    def claim_reply_for_publish(
        self,
        opportunity_id: int,
        *,
        sender_id: int,
        backend: str,
        daily_limit: int,
        comment_window_days: int = 7,
    ) -> dict[str, Any]:
        """Atomically reserve one validated reply immediately before the external write."""
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT ro.*, p.text, p.url AS post_url, s.enabled,
                       s.x_handle AS sender_handle, s.sender_type,
                       s.send_method, s.opencli_profile
                FROM reply_opportunities ro
                JOIN posts p ON p.id=ro.post_id
                JOIN x_sender_accounts s ON s.id=ro.sender_account_id
                WHERE ro.id=? AND s.id=?
                """,
                (opportunity_id, sender_id),
            ).fetchone()
            if not row:
                raise KeyError(opportunity_id)
            if row["status"] != "validated":
                raise ValueError("Reply is not validated or is already being published")
            if not row["enabled"]:
                raise ValueError("Selected sender is disabled")
            block = self._comment_block_reason_in_connection(
                connection,
                str(row["account_id"]),
                window_days=comment_window_days,
                exclude_reply_id=opportunity_id,
            )
            if block:
                raise ValueError(block)
            if backend != "mock":
                daily = connection.execute(
                    """
                    SELECT COUNT(*) AS count FROM reply_opportunities
                    WHERE sender_account_id=?
                      AND status IN ('publishing', 'replied', 'responded', 'confirmation_required')
                      AND COALESCE(send_backend, '')!='mock'
                      AND date(COALESCE(published_at, attempted_at, replied_at))=date('now')
                    """,
                    (sender_id,),
                ).fetchone()
                if int(daily["count"] or 0) >= max(0, daily_limit):
                    raise ValueError("Sender daily comment limit reached")
            now = utc_now()
            cursor = connection.execute(
                """
                UPDATE reply_opportunities
                SET status='publishing', attempted_at=?, send_backend=?
                WHERE id=? AND status='validated'
                """,
                (now, backend, opportunity_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("Reply could not be reserved for publishing")
            connection.commit()
            value = dict(row)
            value.update(status="publishing", attempted_at=now, send_backend=backend)
            return value
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def interrupt_publishing_replies(self) -> int:
        with self.connection() as connection:
            cursor = connection.execute(
                """
                UPDATE reply_opportunities
                SET status='confirmation_required',
                    sanitized_error='Application stopped during publish; verify on X and do not retry'
                WHERE status='publishing'
                """
            )
        return cursor.rowcount

    def account_outreach_history(self, account_id: str, limit: int = 20) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT 'dm' AS channel, m.id, m.status, m.rendered_text AS text,
                       m.attempted_at, m.sent_at AS completed_at, m.provider_receipt AS result,
                       s.label AS sender_label, s.x_handle AS sender_handle,
                       b.template_key AS style, m.provider, m.sanitized_error,
                       COALESCE(m.sent_at, m.attempted_at, m.created_at) AS sort_at
                FROM dm_messages m JOIN dm_batches b ON b.id=m.batch_id
                JOIN x_sender_accounts s ON s.id=b.sender_account_id
                WHERE m.account_id=?
                UNION ALL
                SELECT 'comment', ro.id, ro.status, ro.draft,
                       ro.attempted_at, COALESCE(ro.published_at, ro.replied_at), ro.reply_url,
                       s.label, s.x_handle, ro.comment_style, ro.send_backend,
                       ro.sanitized_error,
                       COALESCE(ro.published_at, ro.attempted_at, ro.replied_at, ro.first_seen_at)
                FROM reply_opportunities ro
                LEFT JOIN x_sender_accounts s ON s.id=ro.sender_account_id
                WHERE ro.account_id=?
                ORDER BY sort_at DESC LIMIT ?
                """,
                (account_id, account_id, max(1, min(limit, 100))),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_brand_profile(self) -> dict[str, Any]:
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM brand_profile WHERE id=1").fetchone()
        if not row:
            return {
                "id": 1,
                "brand_name": "",
                "x_handle": "",
                "description": "",
                "audience": "",
                "tone": "professional, concise, conversational",
                "allowed_claims": [],
                "forbidden_terms": [],
                "updated_at": None,
            }
        value = dict(row)
        for source, target in (
            ("allowed_claims_json", "allowed_claims"),
            ("forbidden_terms_json", "forbidden_terms"),
        ):
            try:
                value[target] = json.loads(value.pop(source) or "[]")
            except json.JSONDecodeError:
                value[target] = []
        return value

    def save_brand_profile(
        self,
        *,
        brand_name: str,
        x_handle: str,
        description: str,
        audience: str,
        tone: str,
        allowed_claims: list[str],
        forbidden_terms: list[str],
    ) -> None:
        now = utc_now()
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO brand_profile(
                    id, brand_name, x_handle, description, audience, tone,
                    allowed_claims_json, forbidden_terms_json, updated_at
                ) VALUES(1, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    brand_name=excluded.brand_name, x_handle=excluded.x_handle,
                    description=excluded.description, audience=excluded.audience,
                    tone=excluded.tone, allowed_claims_json=excluded.allowed_claims_json,
                    forbidden_terms_json=excluded.forbidden_terms_json,
                    updated_at=excluded.updated_at
                """,
                (
                    brand_name.strip(), x_handle.strip().lstrip("@"), description.strip(),
                    audience.strip(), tone.strip(), _json(allowed_claims),
                    _json(forbidden_terms), now,
                ),
            )

    def latest_approved_watch_accounts(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT id FROM seed_sets WHERE status='ready' ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if not row:
                return []
            rows = connection.execute(
                """
                SELECT a.*, m.rank AS seed_rank, m.primary_topic,
                       m.language_bucket, m.score AS seed_score
                FROM seed_members m JOIN accounts a ON a.id=m.account_id
                WHERE m.seed_set_id=? AND m.review_status='approved'
                  AND (m.account_type_bucket='person' OR a.account_type='person')
                ORDER BY m.rank IS NULL, m.rank, m.score DESC
                LIMIT ?
                """,
                (row["id"], max(1, min(limit, 500))),
            ).fetchall()
        return [dict(item) for item in rows]

    def save_post_observations(
        self,
        run_id: int,
        observations: list[tuple[str, str, str]],
        *,
        observed_at: str | None = None,
    ) -> None:
        if not observations:
            return
        now = observed_at or utc_now()
        with self.connection() as connection:
            connection.executemany(
                """
                INSERT OR IGNORE INTO post_observations(
                    run_id, post_id, source_type, source_key, observed_at
                ) VALUES(?, ?, ?, ?, ?)
                """,
                [
                    (run_id, post_id, source_type, source_key, now)
                    for post_id, source_type, source_key in observations
                ],
            )

    def list_recent_signal_posts(self, hours: int = 48) -> list[dict[str, Any]]:
        modifier = f"-{max(1, min(hours, 168))} hours"
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT p.*, a.name AS author_name, a.followers_count, a.account_type,
                       a.languages_json, a.topics_json,
                       GROUP_CONCAT(DISTINCT o.source_type) AS source_types
                FROM posts p
                LEFT JOIN accounts a ON a.id=p.author_id
                LEFT JOIN post_observations o ON o.post_id=p.id
                WHERE julianday(p.created_at) >= julianday('now', ?)
                GROUP BY p.id
                ORDER BY p.created_at DESC
                """,
                (modifier,),
            ).fetchall()
        return [dict(row) for row in rows]

    def upsert_reply_opportunity(
        self,
        *,
        post_id: str,
        account_id: str,
        score: float,
        language: str,
        score_payload: dict[str, Any],
        reasons: list[str],
        draft: str,
        draft_source: str,
        expires_at: str,
        observed_at: str | None = None,
        sender_account_id: int | None = None,
        comment_style: str = "brand",
        publish_mode: str = "review",
        campaign_goal: str = "",
        suitable: bool | None = None,
        suitability_reason: str | None = None,
        validation_status: str = "pending",
        validation_reason: str | None = None,
        initial_status: str = "pending",
    ) -> int:
        now = observed_at or utc_now()
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO reply_opportunities(
                    post_id, account_id, score, status, language, score_json,
                    reasons_json, draft, draft_source, expires_at, first_seen_at,
                    last_scored_at, sender_account_id, comment_style, publish_mode,
                    campaign_goal, suitable, suitability_reason, validation_status,
                    validation_reason
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(post_id) DO UPDATE SET
                    account_id=excluded.account_id, score=excluded.score,
                    language=excluded.language, score_json=excluded.score_json,
                    reasons_json=excluded.reasons_json,
                    draft=CASE WHEN reply_opportunities.manually_edited=1
                                    OR reply_opportunities.status IN (
                                        'publishing', 'replied', 'responded', 'confirmation_required'
                                    )
                               THEN reply_opportunities.draft ELSE excluded.draft END,
                    draft_source=CASE WHEN reply_opportunities.manually_edited=1
                                           OR reply_opportunities.status IN (
                                               'publishing', 'replied', 'responded', 'confirmation_required'
                                           )
                                      THEN reply_opportunities.draft_source
                                      ELSE excluded.draft_source END,
                    expires_at=excluded.expires_at,
                    last_scored_at=excluded.last_scored_at,
                    sender_account_id=CASE WHEN reply_opportunities.status IN (
                                                'publishing', 'replied', 'responded', 'confirmation_required'
                                            )
                                           THEN reply_opportunities.sender_account_id
                                           ELSE COALESCE(reply_opportunities.sender_account_id, excluded.sender_account_id)
                                      END,
                    comment_style=CASE WHEN reply_opportunities.status IN (
                                           'publishing', 'replied', 'responded', 'confirmation_required'
                                       ) THEN reply_opportunities.comment_style ELSE excluded.comment_style END,
                    publish_mode=CASE WHEN reply_opportunities.status IN (
                                          'publishing', 'replied', 'responded', 'confirmation_required'
                                      ) THEN reply_opportunities.publish_mode ELSE excluded.publish_mode END,
                    campaign_goal=CASE WHEN reply_opportunities.status IN (
                                           'publishing', 'replied', 'responded', 'confirmation_required'
                                       ) THEN reply_opportunities.campaign_goal ELSE excluded.campaign_goal END,
                    suitable=CASE WHEN reply_opportunities.status IN (
                                      'publishing', 'replied', 'responded', 'confirmation_required'
                                  ) THEN reply_opportunities.suitable ELSE excluded.suitable END,
                    suitability_reason=CASE WHEN reply_opportunities.status IN (
                                                'publishing', 'replied', 'responded', 'confirmation_required'
                                            ) THEN reply_opportunities.suitability_reason ELSE excluded.suitability_reason END,
                    validation_status=CASE WHEN reply_opportunities.status IN (
                                               'publishing', 'replied', 'responded', 'confirmation_required'
                                           ) THEN reply_opportunities.validation_status ELSE excluded.validation_status END,
                    validation_reason=CASE WHEN reply_opportunities.status IN (
                                               'publishing', 'replied', 'responded', 'confirmation_required'
                                           ) THEN reply_opportunities.validation_reason ELSE excluded.validation_reason END
                """,
                (
                    post_id, account_id, score, initial_status, language, _json(score_payload),
                    _json(reasons), draft, draft_source, expires_at, now, now,
                    sender_account_id, comment_style, publish_mode, campaign_goal,
                    None if suitable is None else int(suitable), suitability_reason,
                    validation_status, validation_reason,
                ),
            )
            row = connection.execute(
                "SELECT id FROM reply_opportunities WHERE post_id=?", (post_id,)
            ).fetchone()
        return int(row["id"])

    @staticmethod
    def _decode_reply_opportunity(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        value = dict(row)
        for source, target, fallback in (
            ("score_json", "score_payload", {}),
            ("reasons_json", "reasons", []),
            ("outcome_json", "outcome", {}),
        ):
            try:
                value[target] = json.loads(value.pop(source) or _json(fallback))
            except json.JSONDecodeError:
                value[target] = fallback
        return value

    def expire_reply_opportunities(self, now: str | None = None) -> int:
        with self.connection() as connection:
            cursor = connection.execute(
                """
                UPDATE reply_opportunities SET status='expired'
                WHERE status='pending' AND expires_at<=?
                """,
                (now or utc_now(),),
            )
        return cursor.rowcount

    def list_reply_opportunities(
        self,
        *,
        status: str = "pending",
        language: str = "all",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        sql = """
            SELECT ro.*, p.text, p.created_at, p.url AS post_url, p.like_count,
                   p.retweet_count, p.reply_count, p.quote_count, p.view_count,
                   a.username, a.name, a.followers_count, a.profile_image_url,
                   s.label AS sender_label, s.x_handle AS sender_handle
            FROM reply_opportunities ro
            JOIN posts p ON p.id=ro.post_id
            LEFT JOIN accounts a ON a.id=ro.account_id
            LEFT JOIN x_sender_accounts s ON s.id=ro.sender_account_id
            WHERE 1=1
        """
        params: list[Any] = []
        if status != "all":
            sql += " AND ro.status=?"
            params.append(status)
        if language != "all":
            sql += " AND ro.language=?"
            params.append(language)
        sql += " ORDER BY ro.score DESC, p.created_at DESC LIMIT ?"
        params.append(max(1, min(limit, 500)))
        with self.connection() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [self._decode_reply_opportunity(row) for row in rows]

    def get_reply_opportunity(self, opportunity_id: int) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT ro.*, p.text, p.created_at, p.url AS post_url,
                       p.conversation_id, p.author_username,
                       a.username, a.name, a.followers_count
                FROM reply_opportunities ro
                JOIN posts p ON p.id=ro.post_id
                LEFT JOIN accounts a ON a.id=ro.account_id
                WHERE ro.id=?
                """,
                (opportunity_id,),
            ).fetchone()
        return self._decode_reply_opportunity(row) if row else None

    def update_reply_opportunity(
        self,
        opportunity_id: int,
        *,
        status: str | None = None,
        draft: str | None = None,
        reply_url: str | None = None,
        sender_account_id: int | None = None,
        comment_style: str | None = None,
        publish_mode: str | None = None,
        campaign_goal: str | None = None,
        suitable: bool | None = None,
        suitability_reason: str | None = None,
        validation_status: str | None = None,
        validation_reason: str | None = None,
        send_backend: str | None = None,
        sanitized_error: str | None = None,
    ) -> None:
        allowed = {
            "pending", "draft", "validated", "publishing", "review_required",
            "unsuitable", "replied", "confirmation_required", "failed", "skipped",
            "expired", "responded"
        }
        if status is not None and status not in allowed:
            raise ValueError("Invalid reply opportunity status")
        now = datetime.now(timezone.utc)
        with self.connection() as connection:
            existing = connection.execute(
                "SELECT * FROM reply_opportunities WHERE id=?", (opportunity_id,)
            ).fetchone()
            if not existing:
                raise KeyError(opportunity_id)
            if status == "replied" and not (reply_url or existing["reply_url"]):
                raise ValueError("Reply URL is required when marking replied")
            values = {
                "status": status or existing["status"],
                "draft": draft if draft is not None else existing["draft"],
                "manually_edited": 1 if draft is not None else existing["manually_edited"],
                "reply_url": reply_url if reply_url is not None else existing["reply_url"],
                "replied_at": existing["replied_at"],
                "next_check_at": existing["next_check_at"],
            }
            if draft is not None and existing["status"] == "validated" and status is None:
                values["status"] = "draft"
            if status == "replied" and not values["replied_at"]:
                values["replied_at"] = now.isoformat()
                values["next_check_at"] = (now + timedelta(hours=1)).isoformat()
            if status in {"pending", "confirmation_required", "skipped", "expired"}:
                values["next_check_at"] = None
            connection.execute(
                """
                UPDATE reply_opportunities SET
                    status=?, draft=?, manually_edited=?, reply_url=?,
                    replied_at=?, next_check_at=?, sender_account_id=COALESCE(?, sender_account_id),
                    comment_style=COALESCE(?, comment_style), publish_mode=COALESCE(?, publish_mode),
                    campaign_goal=COALESCE(?, campaign_goal), suitable=COALESCE(?, suitable),
                    suitability_reason=COALESCE(?, suitability_reason),
                    validation_status=COALESCE(?, validation_status),
                    validation_reason=COALESCE(?, validation_reason),
                    attempted_at=CASE WHEN ?='publishing' THEN ? ELSE attempted_at END,
                    published_at=CASE WHEN ?='replied' THEN ? ELSE published_at END,
                    send_backend=COALESCE(?, send_backend),
                    sanitized_error=COALESCE(?, sanitized_error)
                WHERE id=?
                """,
                (
                    values["status"], values["draft"], values["manually_edited"],
                    values["reply_url"], values["replied_at"],
                    values["next_check_at"], sender_account_id, comment_style,
                    publish_mode, campaign_goal,
                    None if suitable is None else int(suitable), suitability_reason,
                    validation_status, validation_reason,
                    values["status"], now.isoformat(), values["status"], now.isoformat(),
                    send_backend, sanitized_error, opportunity_id,
                ),
            )

    def due_reply_outcomes(self, now: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT ro.*, p.conversation_id, p.author_username,
                       bp.x_handle AS brand_handle
                FROM reply_opportunities ro
                JOIN posts p ON p.id=ro.post_id
                LEFT JOIN brand_profile bp ON bp.id=1
                WHERE ro.status IN ('replied', 'responded')
                  AND ro.next_check_at IS NOT NULL AND ro.next_check_at<=?
                ORDER BY ro.next_check_at LIMIT ?
                """,
                (now or utc_now(), max(1, min(limit, 100))),
            ).fetchall()
        return [dict(row) for row in rows]

    def save_reply_outcome(
        self,
        opportunity_id: int,
        *,
        stage_hours: int,
        metrics: dict[str, int],
        author_responded: bool,
        payload: dict[str, Any],
        captured_at: str | None = None,
    ) -> None:
        if stage_hours not in {1, 6, 24}:
            raise ValueError("Invalid reply outcome stage")
        now = captured_at or utc_now()
        with self.connection() as connection:
            row = connection.execute(
                "SELECT replied_at, outcome_json FROM reply_opportunities WHERE id=?",
                (opportunity_id,),
            ).fetchone()
            if not row:
                raise KeyError(opportunity_id)
            connection.execute(
                """
                INSERT INTO reply_outcome_snapshots(
                    opportunity_id, stage_hours, captured_at, like_count,
                    retweet_count, reply_count, quote_count, view_count,
                    author_responded, payload_json
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(opportunity_id, stage_hours) DO UPDATE SET
                    captured_at=excluded.captured_at, like_count=excluded.like_count,
                    retweet_count=excluded.retweet_count, reply_count=excluded.reply_count,
                    quote_count=excluded.quote_count, view_count=excluded.view_count,
                    author_responded=excluded.author_responded,
                    payload_json=excluded.payload_json
                """,
                (
                    opportunity_id, stage_hours, now, metrics.get("like_count", 0),
                    metrics.get("retweet_count", 0), metrics.get("reply_count", 0),
                    metrics.get("quote_count", 0), metrics.get("view_count", 0),
                    int(author_responded), _json(payload),
                ),
            )
            replied_at = datetime.fromisoformat(row["replied_at"])
            next_stage = {1: 6, 6: 24, 24: None}[stage_hours]
            next_check = (
                (replied_at + timedelta(hours=next_stage)).isoformat()
                if next_stage is not None
                else None
            )
            try:
                outcome = json.loads(row["outcome_json"] or "{}")
            except json.JSONDecodeError:
                outcome = {}
            outcome[str(stage_hours)] = {"metrics": metrics, "author_responded": author_responded}
            connection.execute(
                """
                UPDATE reply_opportunities SET outcome_json=?, next_check_at=?,
                    status=CASE WHEN ? THEN 'responded' ELSE status END,
                    responded_at=CASE WHEN ? THEN COALESCE(responded_at, ?) ELSE responded_at END
                WHERE id=?
                """,
                (
                    _json(outcome), next_check, int(author_responded),
                    int(author_responded), now, opportunity_id,
                ),
            )

    def upsert_topic_cluster(
        self,
        *,
        fingerprint: str,
        title: str,
        summary: str,
        language: str,
        lifecycle: str,
        heat_score: float,
        metrics: dict[str, Any],
        outline: str,
        draft: str,
        draft_source: str,
        native_trend: bool,
        post_ids: list[tuple[str, float]],
        observed_at: str | None = None,
    ) -> int:
        if lifecycle not in {"emerging", "rising", "breakout", "declining"}:
            raise ValueError("Invalid topic lifecycle")
        now = observed_at or utc_now()
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO topic_clusters(
                    fingerprint, title, summary, language, lifecycle, heat_score,
                    metrics_json, outline, draft, draft_source, native_trend,
                    first_seen_at, last_seen_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(fingerprint) DO UPDATE SET
                    title=excluded.title, summary=excluded.summary,
                    language=excluded.language, lifecycle=excluded.lifecycle,
                    heat_score=excluded.heat_score, metrics_json=excluded.metrics_json,
                    outline=CASE WHEN topic_clusters.manually_edited=1
                                 THEN topic_clusters.outline ELSE excluded.outline END,
                    draft=CASE WHEN topic_clusters.manually_edited=1
                               THEN topic_clusters.draft ELSE excluded.draft END,
                    draft_source=CASE WHEN topic_clusters.manually_edited=1
                                      THEN topic_clusters.draft_source
                                      ELSE excluded.draft_source END,
                    native_trend=excluded.native_trend, last_seen_at=excluded.last_seen_at,
                    updated_at=excluded.updated_at
                """,
                (
                    fingerprint, title, summary, language, lifecycle, heat_score,
                    _json(metrics), outline, draft, draft_source, int(native_trend),
                    now, now, now,
                ),
            )
            row = connection.execute(
                "SELECT id FROM topic_clusters WHERE fingerprint=?", (fingerprint,)
            ).fetchone()
            cluster_id = int(row["id"])
            connection.executemany(
                """
                INSERT INTO topic_cluster_posts(cluster_id, post_id, relevance, observed_at)
                VALUES(?, ?, ?, ?)
                ON CONFLICT(cluster_id, post_id) DO UPDATE SET
                    relevance=excluded.relevance, observed_at=excluded.observed_at
                """,
                [(cluster_id, post_id, relevance, now) for post_id, relevance in post_ids],
            )
            connection.execute(
                """
                INSERT INTO topic_metric_snapshots(
                    cluster_id, captured_at, post_count, unique_authors,
                    engagement_total, kol_count, heat_score, payload_json
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    cluster_id, now, metrics.get("post_count", 0),
                    metrics.get("unique_authors", 0), metrics.get("engagement_total", 0),
                    metrics.get("kol_count", 0), heat_score, _json(metrics),
                ),
            )
        return cluster_id

    @staticmethod
    def _decode_topic_cluster(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        value = dict(row)
        try:
            value["metrics"] = json.loads(value.pop("metrics_json") or "{}")
        except json.JSONDecodeError:
            value["metrics"] = {}
        return value

    def list_topic_clusters(
        self,
        *,
        status: str = "pending",
        lifecycle: str = "all",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        sql = """
            SELECT tc.*,
                   (SELECT p.url FROM topic_cluster_posts tcp JOIN posts p ON p.id=tcp.post_id
                    WHERE tcp.cluster_id=tc.id
                    ORDER BY (p.like_count+p.retweet_count+p.reply_count+p.quote_count) DESC
                    LIMIT 1) AS representative_url,
                   (SELECT p.author_username FROM topic_cluster_posts tcp JOIN posts p ON p.id=tcp.post_id
                    WHERE tcp.cluster_id=tc.id
                    ORDER BY (p.like_count+p.retweet_count+p.reply_count+p.quote_count) DESC
                    LIMIT 1) AS representative_author
            FROM topic_clusters tc WHERE 1=1
        """
        params: list[Any] = []
        if status != "all":
            sql += " AND tc.editorial_status=?"
            params.append(status)
        if lifecycle != "all":
            sql += " AND tc.lifecycle=?"
            params.append(lifecycle)
        sql += " ORDER BY tc.heat_score DESC, tc.last_seen_at DESC LIMIT ?"
        params.append(max(1, min(limit, 500)))
        with self.connection() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [self._decode_topic_cluster(row) for row in rows]

    def get_topic_cluster(self, cluster_id: int) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM topic_clusters WHERE id=?", (cluster_id,)
            ).fetchone()
            if not row:
                return None
            posts = connection.execute(
                """
                SELECT p.*, tcp.relevance, a.followers_count
                FROM topic_cluster_posts tcp JOIN posts p ON p.id=tcp.post_id
                LEFT JOIN accounts a ON a.id=p.author_id
                WHERE tcp.cluster_id=?
                ORDER BY (p.like_count+p.retweet_count+p.reply_count+p.quote_count) DESC
                LIMIT 20
                """,
                (cluster_id,),
            ).fetchall()
        value = self._decode_topic_cluster(row)
        value["posts"] = [dict(item) for item in posts]
        return value

    def update_topic_cluster(
        self,
        cluster_id: int,
        *,
        status: str | None = None,
        outline: str | None = None,
        draft: str | None = None,
        published_url: str | None = None,
    ) -> None:
        allowed = {"pending", "adopted", "published", "skipped"}
        if status is not None and status not in allowed:
            raise ValueError("Invalid editorial status")
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM topic_clusters WHERE id=?", (cluster_id,)
            ).fetchone()
            if not row:
                raise KeyError(cluster_id)
            next_status = status or row["editorial_status"]
            connection.execute(
                """
                UPDATE topic_clusters SET editorial_status=?, outline=?, draft=?,
                    manually_edited=?, published_url=?, published_at=?, updated_at=?
                WHERE id=?
                """,
                (
                    next_status,
                    outline if outline is not None else row["outline"],
                    draft if draft is not None else row["draft"],
                    1 if outline is not None or draft is not None else row["manually_edited"],
                    published_url if published_url is not None else row["published_url"],
                    utc_now() if next_status == "published" and not row["published_at"] else row["published_at"],
                    utc_now(), cluster_id,
                ),
            )
