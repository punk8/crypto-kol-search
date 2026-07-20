from __future__ import annotations

import json
import hashlib
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

CREATE TABLE IF NOT EXISTS managed_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,
    external_account_id TEXT NOT NULL,
    username TEXT NOT NULL,
    display_name TEXT,
    browser_profile TEXT NOT NULL,
    roles_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'active',
    comment_daily_limit INTEGER NOT NULL DEFAULT 10,
    comment_hourly_limit INTEGER NOT NULL DEFAULT 3,
    dm_daily_limit INTEGER NOT NULL DEFAULT 5,
    dm_hourly_limit INTEGER NOT NULL DEFAULT 2,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_health_at TEXT,
    last_health_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(platform, external_account_id)
);

CREATE TABLE IF NOT EXISTS outbound_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    platform TEXT NOT NULL,
    managed_account_id INTEGER NOT NULL,
    target_external_id TEXT NOT NULL,
    target_url TEXT,
    target_author_id TEXT,
    conversation_id TEXT,
    draft TEXT NOT NULL,
    final_text TEXT,
    status TEXT NOT NULL DEFAULT 'draft',
    idempotency_key TEXT NOT NULL UNIQUE,
    approved_by TEXT,
    approved_at TEXT,
    started_at TEXT,
    sent_at TEXT,
    receipt_url TEXT,
    receipt_external_id TEXT,
    receipt_json TEXT NOT NULL DEFAULT '{}',
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (managed_account_id) REFERENCES managed_accounts(id)
);
CREATE INDEX IF NOT EXISTS idx_outbound_actions_queue
ON outbound_actions(status, platform, kind, created_at);
CREATE INDEX IF NOT EXISTS idx_outbound_actions_limits
ON outbound_actions(managed_account_id, kind, sent_at);

CREATE TABLE IF NOT EXISTS conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,
    managed_account_id INTEGER NOT NULL,
    external_conversation_id TEXT,
    participant_external_id TEXT NOT NULL,
    participant_name TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    last_message_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(platform, managed_account_id, participant_external_id),
    FOREIGN KEY (managed_account_id) REFERENCES managed_accounts(id)
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL,
    external_message_id TEXT,
    direction TEXT NOT NULL,
    text TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'observed',
    sent_at TEXT,
    raw_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE(conversation_id, external_message_id),
    FOREIGN KEY (conversation_id) REFERENCES conversations(id)
);

CREATE TABLE IF NOT EXISTS do_not_contact (
    platform TEXT NOT NULL,
    target_external_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(platform, target_external_id)
);

CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    object_type TEXT NOT NULL,
    object_id TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_events_object
ON audit_events(object_type, object_id, created_at);

CREATE TABLE IF NOT EXISTS trend_categories (
    slug TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    keywords_json TEXT NOT NULL DEFAULT '[]',
    enabled INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS system_controls (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_by TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS postiz_integrations (
    external_id TEXT PRIMARY KEY,
    identifier TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    profile TEXT NOT NULL DEFAULT '',
    picture TEXT,
    disabled INTEGER NOT NULL DEFAULT 0,
    is_default INTEGER NOT NULL DEFAULT 0,
    raw_json TEXT NOT NULL DEFAULT '{}',
    captured_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_postiz_integrations_provider
ON postiz_integrations(identifier, disabled, profile);

CREATE TABLE IF NOT EXISTS owned_posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_type TEXT NOT NULL DEFAULT 'manual',
    source_cluster_id INTEGER,
    integration_id TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT 'now',
    timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai',
    scheduled_at_local TEXT,
    scheduled_at_utc TEXT,
    who_can_reply TEXT NOT NULL DEFAULT 'everyone',
    made_with_ai INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'draft',
    idempotency_key TEXT UNIQUE,
    postiz_post_id TEXT,
    release_url TEXT,
    approved_by TEXT,
    approved_at TEXT,
    submitted_at TEXT,
    published_at TEXT,
    receipt_json TEXT NOT NULL DEFAULT '{}',
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(source_cluster_id) REFERENCES topic_clusters(id),
    FOREIGN KEY(integration_id) REFERENCES postiz_integrations(external_id)
);
CREATE INDEX IF NOT EXISTS idx_owned_posts_status
ON owned_posts(status, scheduled_at_utc, created_at);

CREATE TABLE IF NOT EXISTS owned_post_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owned_post_id INTEGER NOT NULL,
    position INTEGER NOT NULL,
    content TEXT NOT NULL,
    UNIQUE(owned_post_id, position),
    FOREIGN KEY(owned_post_id) REFERENCES owned_posts(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS owned_post_media (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owned_post_id INTEGER NOT NULL,
    item_position INTEGER NOT NULL,
    source_type TEXT NOT NULL,
    source_value TEXT NOT NULL,
    filename TEXT,
    content_type TEXT,
    storage_path TEXT,
    postiz_asset_id TEXT,
    postiz_asset_path TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(owned_post_id) REFERENCES owned_posts(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_owned_post_media_post
ON owned_post_media(owned_post_id, item_position, id);
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
    "platform": "TEXT NOT NULL DEFAULT 'x'",
    "external_id": "TEXT",
    "source_provider": "TEXT NOT NULL DEFAULT 'legacy'",
    "captured_at": "TEXT",
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
    "platform": "TEXT NOT NULL DEFAULT 'x'",
    "external_id": "TEXT",
    "source_provider": "TEXT NOT NULL DEFAULT 'legacy'",
    "captured_at": "TEXT",
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

TOPIC_CLUSTER_COLUMNS: dict[str, str] = {
    "direction": "TEXT NOT NULL DEFAULT 'other'",
}

BRAND_COLUMNS: dict[str, str] = {
    "xiaohongshu_handle": "TEXT NOT NULL DEFAULT ''",
    "product_url": "TEXT NOT NULL DEFAULT ''",
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
            connection.execute("DROP INDEX IF EXISTS idx_accounts_username_nocase")
            connection.execute(
                "UPDATE accounts SET platform=COALESCE(NULLIF(platform, ''), 'x'), "
                "external_id=COALESCE(NULLIF(external_id, ''), id), "
                "captured_at=COALESCE(captured_at, updated_at)"
            )
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_accounts_platform_external "
                "ON accounts(platform, external_id)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_accounts_platform_username "
                "ON accounts(platform, username COLLATE NOCASE)"
            )
            post_existing = {row[1] for row in connection.execute("PRAGMA table_info(posts)")}
            for name, definition in POST_COLUMNS.items():
                if name not in post_existing:
                    connection.execute(f"ALTER TABLE posts ADD COLUMN {name} {definition}")
            connection.execute(
                "UPDATE posts SET platform=COALESCE(NULLIF(platform, ''), 'x'), "
                "external_id=COALESCE(NULLIF(external_id, ''), id), "
                "captured_at=COALESCE(captured_at, fetched_at)"
            )
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_posts_platform_external "
                "ON posts(platform, external_id)"
            )
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
            topic_existing = {
                row[1] for row in connection.execute("PRAGMA table_info(topic_clusters)")
            }
            for name, definition in TOPIC_CLUSTER_COLUMNS.items():
                if name not in topic_existing:
                    connection.execute(
                        f"ALTER TABLE topic_clusters ADD COLUMN {name} {definition}"
                    )
            brand_existing = {
                row[1] for row in connection.execute("PRAGMA table_info(brand_profile)")
            }
            for name, definition in BRAND_COLUMNS.items():
                if name not in brand_existing:
                    connection.execute(
                        f"ALTER TABLE brand_profile ADD COLUMN {name} {definition}"
                    )
            now = utc_now()
            for slug, display_name, keywords in (
                ("finance", "金融", ["finance", "金融", "market", "宏观", "投资"]),
                ("technology", "科技", ["technology", "tech", "科技", "芯片", "软件"]),
                ("ai", "AI", ["ai", "人工智能", "大模型", "llm", "agent"]),
                ("crypto-rwa", "加密/RWA", ["crypto", "加密货币", "bitcoin", "RWA", "代币化"]),
            ):
                connection.execute(
                    """
                    INSERT OR IGNORE INTO trend_categories(
                        slug, display_name, keywords_json, enabled, updated_at
                    ) VALUES(?, ?, ?, 1, ?)
                    """,
                    (slug, display_name, _json(keywords), now),
                )
            connection.execute(
                """
                INSERT OR IGNORE INTO system_controls(key, value, updated_by, updated_at)
                VALUES('global_kill_switch', 'false', 'migration', ?)
                """,
                (now,),
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
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(6, ?)",
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

    def resolve_account_id(
        self, username: str, proposed_id: str, platform: str = "x"
    ) -> str:
        """Reuse the canonical id associated with a platform-scoped handle."""
        with self.connection() as connection:
            row = connection.execute(
                "SELECT id FROM accounts WHERE platform=? AND username=? COLLATE NOCASE",
                (platform, username.lstrip("@")),
            ).fetchone()
        return str(row["id"]) if row else str(proposed_id)

    def upsert_account(
        self, account_dict: dict[str, Any], enrichment: dict[str, Any] | None = None
    ) -> str:
        enrichment = enrichment or {}
        now = utc_now()
        platform = str(account_dict.get("platform") or "x")
        effective_id = self.resolve_account_id(
            account_dict["username"], account_dict["id"], platform
        )
        account_dict = dict(account_dict)
        account_dict["id"] = effective_id
        external_id = str(account_dict.get("external_id") or effective_id)
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO accounts (
                    id, platform, external_id, source_provider, captured_at,
                    username, name, description, followers_count, following_count, tweet_count,
                    listed_count, verified, protected, created_at, profile_image_url, profile_url,
                    location, entities_json, account_type, languages_json, topics_json, summary,
                    raw_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    platform=excluded.platform, external_id=excluded.external_id,
                    source_provider=excluded.source_provider, captured_at=excluded.captured_at,
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
                    account_dict["id"], platform, external_id,
                    account_dict.get("source_provider", "unknown"),
                    account_dict.get("captured_at") or now,
                    account_dict["username"], account_dict.get("name"),
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
                post.author_id = self.resolve_account_id(
                    post.author_username, post.author_id, post.platform
                )
        with self.connection() as connection:
            connection.executemany(
                """
                INSERT INTO posts (
                    id, platform, external_id, source_provider, captured_at,
                    author_id, author_username, text, created_at, like_count, retweet_count,
                    reply_count, quote_count, view_count, bookmark_count, lang, url,
                    conversation_id, in_reply_to_user_id, in_reply_to_username,
                    referenced_post_id, reference_type, raw_json, fetched_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    platform=excluded.platform, external_id=excluded.external_id,
                    source_provider=excluded.source_provider, captured_at=excluded.captured_at,
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
                        post.id, post.platform, post.external_id or post.id,
                        post.source_provider, post.captured_at or now,
                        post.author_id, post.author_username, post.text, post.created_at,
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
            SELECT cs.*, a.platform, a.external_id, a.source_provider, a.captured_at,
                   a.profile_url, a.name, a.description, a.followers_count, a.verified,
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
        sql += " ORDER BY a.platform, cs.rank IS NULL, cs.rank, cs.score_total DESC"
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

    def get_brand_profile(self) -> dict[str, Any]:
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM brand_profile WHERE id=1").fetchone()
        if not row:
            return {
                "id": 1,
                "brand_name": "",
                "x_handle": "",
                "xiaohongshu_handle": "",
                "product_url": "",
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
        xiaohongshu_handle: str = "",
        product_url: str = "",
    ) -> None:
        now = utc_now()
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO brand_profile(
                    id, brand_name, x_handle, xiaohongshu_handle, product_url,
                    description, audience, tone,
                    allowed_claims_json, forbidden_terms_json, updated_at
                ) VALUES(1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    brand_name=excluded.brand_name, x_handle=excluded.x_handle,
                    xiaohongshu_handle=excluded.xiaohongshu_handle,
                    product_url=excluded.product_url,
                    description=excluded.description, audience=excluded.audience,
                    tone=excluded.tone, allowed_claims_json=excluded.allowed_claims_json,
                    forbidden_terms_json=excluded.forbidden_terms_json,
                    updated_at=excluded.updated_at
                """,
                (
                    brand_name.strip(), x_handle.strip().lstrip("@"),
                    xiaohongshu_handle.strip().lstrip("@"), product_url.strip(),
                    description.strip(),
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
    ) -> int:
        now = observed_at or utc_now()
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO reply_opportunities(
                    post_id, account_id, score, status, language, score_json,
                    reasons_json, draft, draft_source, expires_at, first_seen_at,
                    last_scored_at
                ) VALUES(?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(post_id) DO UPDATE SET
                    account_id=excluded.account_id, score=excluded.score,
                    language=excluded.language, score_json=excluded.score_json,
                    reasons_json=excluded.reasons_json,
                    draft=CASE WHEN reply_opportunities.manually_edited=1
                               THEN reply_opportunities.draft ELSE excluded.draft END,
                    draft_source=CASE WHEN reply_opportunities.manually_edited=1
                                      THEN reply_opportunities.draft_source
                                      ELSE excluded.draft_source END,
                    expires_at=excluded.expires_at,
                    last_scored_at=excluded.last_scored_at
                """,
                (
                    post_id, account_id, score, language, _json(score_payload),
                    _json(reasons), draft, draft_source, expires_at, now, now,
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
            SELECT ro.*, p.platform, p.external_id AS post_external_id,
                   p.text, p.created_at, p.url AS post_url, p.like_count,
                   p.retweet_count, p.reply_count, p.quote_count, p.view_count,
                   a.username, a.name, a.followers_count, a.profile_image_url
            FROM reply_opportunities ro
            JOIN posts p ON p.id=ro.post_id
            LEFT JOIN accounts a ON a.id=ro.account_id
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
                       p.platform, p.external_id AS post_external_id,
                       p.conversation_id, p.author_username, p.author_id,
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
    ) -> None:
        allowed = {
            "pending", "replied", "confirmation_required", "skipped", "expired", "responded"
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
            if status == "replied" and not values["replied_at"]:
                values["replied_at"] = now.isoformat()
                values["next_check_at"] = (now + timedelta(hours=1)).isoformat()
            if status in {"pending", "confirmation_required", "skipped", "expired"}:
                values["next_check_at"] = None
            connection.execute(
                """
                UPDATE reply_opportunities SET
                    status=?, draft=?, manually_edited=?, reply_url=?,
                    replied_at=?, next_check_at=?
                WHERE id=?
                """,
                (
                    values["status"], values["draft"], values["manually_edited"],
                    values["reply_url"], values["replied_at"],
                    values["next_check_at"], opportunity_id,
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
        direction: str = "other",
        replace_posts: bool = False,
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
                    direction, first_seen_at, last_seen_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    native_trend=excluded.native_trend, direction=excluded.direction,
                    last_seen_at=excluded.last_seen_at,
                    updated_at=excluded.updated_at
                """,
                (
                    fingerprint, title, summary, language, lifecycle, heat_score,
                    _json(metrics), outline, draft, draft_source, int(native_trend),
                    direction, now, now, now,
                ),
            )
            row = connection.execute(
                "SELECT id FROM topic_clusters WHERE fingerprint=?", (fingerprint,)
            ).fetchone()
            cluster_id = int(row["id"])
            if replace_posts:
                connection.execute(
                    "DELETE FROM topic_cluster_posts WHERE cluster_id=?", (cluster_id,)
                )
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
        direction: str = "all",
        platform: str = "all",
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
        if direction != "all":
            sql += " AND tc.direction=?"
            params.append(direction)
        if platform != "all":
            sql += (
                " AND EXISTS (SELECT 1 FROM topic_cluster_posts tcp2 "
                "JOIN posts p2 ON p2.id=tcp2.post_id "
                "WHERE tcp2.cluster_id=tc.id AND p2.platform=?)"
            )
            params.append(platform)
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

    # Multi-platform account pool and outbound workflow.
    def upsert_managed_account(
        self,
        *,
        platform: str,
        external_account_id: str,
        username: str,
        browser_profile: str,
        roles: list[str],
        display_name: str | None = None,
        comment_daily_limit: int = 10,
        comment_hourly_limit: int = 3,
        dm_daily_limit: int = 5,
        dm_hourly_limit: int = 2,
    ) -> int:
        if platform not in {"x", "xiaohongshu"}:
            raise ValueError("Unsupported managed-account platform")
        clean_roles = sorted(set(roles))
        if not clean_roles or not set(clean_roles) <= {"engagement", "official_dm"}:
            raise ValueError("Managed account requires engagement and/or official_dm role")
        now = utc_now()
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO managed_accounts(
                    platform, external_account_id, username, display_name,
                    browser_profile, roles_json, status, comment_daily_limit,
                    comment_hourly_limit, dm_daily_limit, dm_hourly_limit,
                    created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?)
                ON CONFLICT(platform, external_account_id) DO UPDATE SET
                    username=excluded.username, display_name=excluded.display_name,
                    browser_profile=excluded.browser_profile, roles_json=excluded.roles_json,
                    comment_daily_limit=excluded.comment_daily_limit,
                    comment_hourly_limit=excluded.comment_hourly_limit,
                    dm_daily_limit=excluded.dm_daily_limit,
                    dm_hourly_limit=excluded.dm_hourly_limit,
                    updated_at=excluded.updated_at
                """,
                (
                    platform,
                    external_account_id,
                    username.lstrip("@"),
                    display_name,
                    browser_profile,
                    _json(clean_roles),
                    max(1, comment_daily_limit),
                    max(1, comment_hourly_limit),
                    max(1, dm_daily_limit),
                    max(1, dm_hourly_limit),
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT id FROM managed_accounts WHERE platform=? AND external_account_id=?",
                (platform, external_account_id),
            ).fetchone()
        return int(row["id"])

    @staticmethod
    def _decode_managed_account(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        value = dict(row)
        try:
            value["roles"] = json.loads(value.pop("roles_json") or "[]")
        except json.JSONDecodeError:
            value["roles"] = []
        return value

    def list_managed_accounts(self, platform: str = "all") -> list[dict[str, Any]]:
        sql = "SELECT * FROM managed_accounts"
        params: list[Any] = []
        if platform != "all":
            sql += " WHERE platform=?"
            params.append(platform)
        sql += " ORDER BY platform, username"
        with self.connection() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [self._decode_managed_account(row) for row in rows]

    def get_managed_account(self, account_id: int) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM managed_accounts WHERE id=?", (account_id,)
            ).fetchone()
        return self._decode_managed_account(row) if row else None

    def set_managed_account_status(
        self, account_id: int, status: str, *, actor: str, reason: str | None = None
    ) -> None:
        if status not in {"active", "paused", "disabled"}:
            raise ValueError("Invalid managed-account status")
        with self.connection() as connection:
            cursor = connection.execute(
                "UPDATE managed_accounts SET status=?, last_health_error=?, updated_at=? WHERE id=?",
                (status, reason, utc_now(), account_id),
            )
            if not cursor.rowcount:
                raise KeyError(account_id)
        self.audit(actor, "managed_account.status", "managed_account", str(account_id), {
            "status": status,
            "reason": reason,
        })

    def save_managed_account_health(
        self, account_id: int, *, ready: bool, error: str | None = None
    ) -> None:
        now = utc_now()
        with self.connection() as connection:
            row = connection.execute(
                "SELECT consecutive_failures FROM managed_accounts WHERE id=?", (account_id,)
            ).fetchone()
            if not row:
                raise KeyError(account_id)
            failures = 0 if ready else int(row["consecutive_failures"] or 0) + 1
            status = "disabled" if failures >= 3 else None
            connection.execute(
                """
                UPDATE managed_accounts SET consecutive_failures=?, last_health_at=?,
                    last_health_error=?, status=COALESCE(?, status), updated_at=?
                WHERE id=?
                """,
                (failures, now, error, status, now, account_id),
            )

    def create_outbound_action(
        self,
        *,
        kind: str,
        platform: str,
        managed_account_id: int,
        target_external_id: str,
        draft: str,
        idempotency_key: str,
        target_url: str | None = None,
        target_author_id: str | None = None,
        conversation_id: str | None = None,
        actor: str = "admin",
    ) -> int:
        if kind not in {"comment", "dm"}:
            raise ValueError("Invalid outbound action kind")
        account = self.get_managed_account(managed_account_id)
        if not account or account["platform"] != platform:
            raise ValueError("Managed account does not match target platform")
        required_role = "engagement" if kind == "comment" else "official_dm"
        if required_role not in account["roles"]:
            raise ValueError(f"Managed account lacks {required_role} role")
        if self.is_do_not_contact(platform, target_author_id or target_external_id):
            raise ValueError("Target is on the do-not-contact list")
        now = utc_now()
        with self.connection() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO outbound_actions(
                    kind, platform, managed_account_id, target_external_id,
                    target_url, target_author_id, conversation_id, draft, status,
                    idempotency_key, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?, ?)
                """,
                (
                    kind,
                    platform,
                    managed_account_id,
                    target_external_id,
                    target_url,
                    target_author_id,
                    conversation_id,
                    draft.strip(),
                    idempotency_key,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT id FROM outbound_actions WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
        action_id = int(row["id"])
        self.audit(actor, "outbound.create", "outbound_action", str(action_id), {
            "kind": kind,
            "platform": platform,
            "target_external_id": target_external_id,
        })
        return action_id

    @staticmethod
    def _decode_outbound_action(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        value = dict(row)
        try:
            value["receipt"] = json.loads(value.pop("receipt_json") or "{}")
        except json.JSONDecodeError:
            value["receipt"] = {}
        if "roles_json" in value:
            try:
                value["managed_roles"] = json.loads(value.pop("roles_json") or "[]")
            except json.JSONDecodeError:
                value["managed_roles"] = []
        return value

    def get_outbound_action(self, action_id: int) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT oa.*, ma.username AS managed_username,
                       ma.external_account_id AS managed_external_account_id,
                       ma.browser_profile, ma.roles_json, ma.status AS managed_account_status,
                       ma.comment_daily_limit, ma.comment_hourly_limit,
                       ma.dm_daily_limit, ma.dm_hourly_limit
                FROM outbound_actions oa
                JOIN managed_accounts ma ON ma.id=oa.managed_account_id
                WHERE oa.id=?
                """,
                (action_id,),
            ).fetchone()
        return self._decode_outbound_action(row) if row else None

    def list_outbound_actions(
        self, *, status: str = "all", kind: str = "all", limit: int = 100
    ) -> list[dict[str, Any]]:
        sql = """
            SELECT oa.*, ma.username AS managed_username, ma.browser_profile,
                   ma.roles_json, ma.status AS managed_account_status
            FROM outbound_actions oa JOIN managed_accounts ma ON ma.id=oa.managed_account_id
            WHERE 1=1
        """
        params: list[Any] = []
        if status != "all":
            sql += " AND oa.status=?"
            params.append(status)
        if kind != "all":
            sql += " AND oa.kind=?"
            params.append(kind)
        sql += " ORDER BY oa.id DESC LIMIT ?"
        params.append(max(1, min(limit, 500)))
        with self.connection() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [self._decode_outbound_action(row) for row in rows]

    def approve_outbound_action(
        self, action_id: int, *, final_text: str, actor: str
    ) -> None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT status FROM outbound_actions WHERE id=?", (action_id,)
            ).fetchone()
            if not row:
                raise KeyError(action_id)
            if row["status"] not in {"draft", "failed"}:
                raise ValueError("Only draft or failed actions can be approved")
            connection.execute(
                """
                UPDATE outbound_actions SET status='approved', final_text=?,
                    approved_by=?, approved_at=?, error=NULL, updated_at=? WHERE id=?
                """,
                (final_text.strip(), actor, utc_now(), utc_now(), action_id),
            )
        self.audit(actor, "outbound.approve", "outbound_action", str(action_id), {
            "text": final_text.strip(),
        })

    def claim_outbound_action(self, action_id: int) -> dict[str, Any]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status FROM outbound_actions WHERE id=?", (action_id,)
            ).fetchone()
            if not row:
                raise KeyError(action_id)
            if row["status"] != "approved":
                raise ValueError("Outbound action is not approved")
            connection.execute(
                "UPDATE outbound_actions SET status='sending', started_at=?, updated_at=? WHERE id=?",
                (utc_now(), utc_now(), action_id),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        action = self.get_outbound_action(action_id)
        if not action:
            raise KeyError(action_id)
        return action

    def finish_outbound_action(
        self,
        action_id: int,
        *,
        status: str,
        receipt_url: str | None = None,
        receipt_external_id: str | None = None,
        receipt: dict[str, Any] | None = None,
        error: str | None = None,
        actor: str = "system",
    ) -> None:
        if status not in {
            "sent",
            "confirmation_required",
            "failed",
            "cancelled",
            "target_not_messageable",
        }:
            raise ValueError("Invalid outbound completion status")
        now = utc_now()
        with self.connection() as connection:
            cursor = connection.execute(
                """
                UPDATE outbound_actions SET status=?, sent_at=?, receipt_url=?,
                    receipt_external_id=?, receipt_json=?, error=?, updated_at=? WHERE id=?
                """,
                (
                    status,
                    now if status == "sent" else None,
                    receipt_url,
                    receipt_external_id,
                    _json(receipt or {}),
                    error,
                    now,
                    action_id,
                ),
            )
            if not cursor.rowcount:
                raise KeyError(action_id)
        self.audit(actor, f"outbound.{status}", "outbound_action", str(action_id), {
            "receipt_url": receipt_url,
            "error": error,
        })

    def outbound_usage(self, managed_account_id: int, kind: str) -> dict[str, int]:
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT
                    SUM(CASE WHEN sent_at >= datetime('now', '-1 hour') THEN 1 ELSE 0 END) AS hourly,
                    SUM(CASE WHEN sent_at >= datetime('now', '-1 day') THEN 1 ELSE 0 END) AS daily
                FROM outbound_actions
                WHERE managed_account_id=? AND kind=? AND status='sent'
                """,
                (managed_account_id, kind),
            ).fetchone()
        return {"hourly": int(row["hourly"] or 0), "daily": int(row["daily"] or 0)}

    def has_recent_author_contact(
        self, *, platform: str, target_author_id: str, days: int, kind: str | None = None
    ) -> bool:
        sql = """
            SELECT 1 FROM outbound_actions
            WHERE platform=? AND target_author_id=? AND status='sent'
              AND sent_at >= datetime('now', ?)
        """
        params: list[Any] = [platform, target_author_id, f"-{max(1, days)} days"]
        if kind:
            sql += " AND kind=?"
            params.append(kind)
        sql += " LIMIT 1"
        with self.connection() as connection:
            return connection.execute(sql, params).fetchone() is not None

    def has_sent_target(self, *, platform: str, kind: str, target_external_id: str) -> bool:
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM outbound_actions
                WHERE platform=? AND kind=? AND target_external_id=? AND status='sent'
                LIMIT 1
                """,
                (platform, kind, target_external_id),
            ).fetchone()
        return row is not None

    def add_do_not_contact(
        self, *, platform: str, target_external_id: str, reason: str, actor: str
    ) -> None:
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO do_not_contact(
                    platform, target_external_id, reason, created_by, created_at
                ) VALUES(?, ?, ?, ?, ?)
                ON CONFLICT(platform, target_external_id) DO UPDATE SET
                    reason=excluded.reason, created_by=excluded.created_by,
                    created_at=excluded.created_at
                """,
                (platform, target_external_id, reason, actor, utc_now()),
            )
        self.audit(actor, "do_not_contact.add", "target", f"{platform}:{target_external_id}", {
            "reason": reason,
        })

    def is_do_not_contact(self, platform: str, target_external_id: str) -> bool:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT 1 FROM do_not_contact WHERE platform=? AND target_external_id=?",
                (platform, target_external_id),
            ).fetchone()
        return row is not None

    def audit(
        self,
        actor: str,
        action: str,
        object_type: str,
        object_id: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO audit_events(
                    actor, action, object_type, object_id, payload_json, created_at
                ) VALUES(?, ?, ?, ?, ?, ?)
                """,
                (actor, action, object_type, object_id, _json(payload or {}), utc_now()),
            )

    def list_audit_events(self, limit: int = 200) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM audit_events ORDER BY id DESC LIMIT ?",
                (max(1, min(limit, 1000)),),
            ).fetchall()
        output: list[dict[str, Any]] = []
        for row in rows:
            value = dict(row)
            try:
                value["payload"] = json.loads(value.pop("payload_json") or "{}")
            except json.JSONDecodeError:
                value["payload"] = {}
            output.append(value)
        return output

    def get_control(self, key: str, default: str = "") -> str:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT value FROM system_controls WHERE key=?", (key,)
            ).fetchone()
        return str(row["value"]) if row else default

    def set_control(self, key: str, value: str, *, actor: str) -> None:
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO system_controls(key, value, updated_by, updated_at)
                VALUES(?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value,
                    updated_by=excluded.updated_by, updated_at=excluded.updated_at
                """,
                (key, value, actor, utc_now()),
            )
        self.audit(actor, "control.set", "system_control", key, {"value": value})

    def list_trend_categories(self, enabled_only: bool = True) -> list[dict[str, Any]]:
        sql = "SELECT * FROM trend_categories"
        if enabled_only:
            sql += " WHERE enabled=1"
        sql += " ORDER BY slug"
        with self.connection() as connection:
            rows = connection.execute(sql).fetchall()
        output: list[dict[str, Any]] = []
        for row in rows:
            value = dict(row)
            try:
                value["keywords"] = json.loads(value.pop("keywords_json") or "[]")
            except json.JSONDecodeError:
                value["keywords"] = []
            output.append(value)
        return output

    def upsert_conversation(
        self,
        *,
        platform: str,
        managed_account_id: int,
        participant_external_id: str,
        participant_name: str | None = None,
        external_conversation_id: str | None = None,
        last_message_at: str | None = None,
    ) -> int:
        now = utc_now()
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO conversations(
                    platform, managed_account_id, external_conversation_id,
                    participant_external_id, participant_name, last_message_at,
                    created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(platform, managed_account_id, participant_external_id)
                DO UPDATE SET
                    external_conversation_id=COALESCE(excluded.external_conversation_id,
                                                      conversations.external_conversation_id),
                    participant_name=COALESCE(excluded.participant_name,
                                              conversations.participant_name),
                    last_message_at=COALESCE(excluded.last_message_at,
                                             conversations.last_message_at),
                    updated_at=excluded.updated_at
                """,
                (
                    platform,
                    managed_account_id,
                    external_conversation_id,
                    participant_external_id,
                    participant_name,
                    last_message_at,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                """
                SELECT id FROM conversations
                WHERE platform=? AND managed_account_id=? AND participant_external_id=?
                """,
                (platform, managed_account_id, participant_external_id),
            ).fetchone()
        return int(row["id"])

    def save_message(
        self,
        *,
        conversation_id: int,
        direction: str,
        text: str,
        external_message_id: str | None = None,
        status: str = "observed",
        sent_at: str | None = None,
        raw: dict[str, Any] | None = None,
    ) -> int:
        if direction not in {"inbound", "outbound"}:
            raise ValueError("Invalid message direction")
        stable_external_id = external_message_id or hashlib.sha256(
            f"{direction}:{text}:{sent_at or ''}".encode("utf-8")
        ).hexdigest()
        with self.connection() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO messages(
                    conversation_id, external_message_id, direction, text,
                    status, sent_at, raw_json, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    conversation_id,
                    stable_external_id,
                    direction,
                    text,
                    status,
                    sent_at,
                    _json(raw or {}),
                    utc_now(),
                ),
            )
            row = connection.execute(
                "SELECT id FROM messages WHERE conversation_id=? AND external_message_id=?",
                (conversation_id, stable_external_id),
            ).fetchone()
        return int(row["id"])

    def list_conversations(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT c.*, ma.username AS managed_username,
                       (SELECT text FROM messages m WHERE m.conversation_id=c.id
                        ORDER BY m.id DESC LIMIT 1) AS last_message
                FROM conversations c JOIN managed_accounts ma ON ma.id=c.managed_account_id
                ORDER BY COALESCE(c.last_message_at, c.updated_at) DESC LIMIT ?
                """,
                (max(1, min(limit, 500)),),
            ).fetchall()
        return [dict(row) for row in rows]

    # Postiz-owned X publishing.
    def sync_postiz_integrations(
        self, integrations: list[dict[str, Any]], *, actor: str = "system"
    ) -> int:
        now = utc_now()
        x_rows = [
            row for row in integrations
            if str(row.get("identifier") or "").lower() == "x" and row.get("id")
        ]
        with self.connection() as connection:
            connection.execute(
                "UPDATE postiz_integrations SET disabled=1, updated_at=? WHERE identifier='x'",
                (now,),
            )
            for row in x_rows:
                connection.execute(
                    """
                    INSERT INTO postiz_integrations(
                        external_id, identifier, name, profile, picture, disabled,
                        raw_json, captured_at, updated_at
                    ) VALUES(?, 'x', ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(external_id) DO UPDATE SET
                        identifier='x', name=excluded.name, profile=excluded.profile,
                        picture=excluded.picture, disabled=excluded.disabled,
                        raw_json=excluded.raw_json, captured_at=excluded.captured_at,
                        updated_at=excluded.updated_at
                    """,
                    (
                        str(row["id"]),
                        str(row.get("name") or ""),
                        str(row.get("profile") or "").lstrip("@"),
                        row.get("picture"),
                        int(bool(row.get("disabled"))),
                        _json(row),
                        now,
                        now,
                    ),
                )
            default = connection.execute(
                """
                SELECT external_id FROM postiz_integrations
                WHERE identifier='x' AND disabled=0 AND is_default=1 LIMIT 1
                """
            ).fetchone()
            if default is None:
                first = connection.execute(
                    """
                    SELECT external_id FROM postiz_integrations
                    WHERE identifier='x' AND disabled=0 ORDER BY profile, external_id LIMIT 1
                    """
                ).fetchone()
                if first:
                    default = first
            if default:
                connection.execute(
                    """
                    UPDATE postiz_integrations SET is_default=CASE WHEN external_id=? THEN 1 ELSE 0 END
                    WHERE identifier='x'
                    """,
                    (default["external_id"],),
                )
            else:
                connection.execute(
                    "UPDATE postiz_integrations SET is_default=0 WHERE identifier='x'"
                )
        self.audit(actor, "postiz.integrations.sync", "postiz", "x", {"count": len(x_rows)})
        return len(x_rows)

    def list_postiz_integrations(self, *, enabled_only: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM postiz_integrations WHERE identifier='x'"
        if enabled_only:
            sql += " AND disabled=0"
        sql += " ORDER BY is_default DESC, profile, external_id"
        with self.connection() as connection:
            rows = connection.execute(sql).fetchall()
        output: list[dict[str, Any]] = []
        for row in rows:
            value = dict(row)
            try:
                value["raw"] = json.loads(value.pop("raw_json") or "{}")
            except json.JSONDecodeError:
                value["raw"] = {}
            output.append(value)
        return output

    def get_postiz_integration(self, external_id: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM postiz_integrations WHERE external_id=?", (external_id,)
            ).fetchone()
        if not row:
            return None
        value = dict(row)
        try:
            value["raw"] = json.loads(value.pop("raw_json") or "{}")
        except json.JSONDecodeError:
            value["raw"] = {}
        return value

    def set_default_postiz_integration(self, external_id: str, *, actor: str) -> None:
        integration = self.get_postiz_integration(external_id)
        if not integration or integration["disabled"]:
            raise ValueError("Postiz X integration 不存在或已禁用")
        with self.connection() as connection:
            connection.execute("UPDATE postiz_integrations SET is_default=0 WHERE identifier='x'")
            connection.execute(
                "UPDATE postiz_integrations SET is_default=1, updated_at=? WHERE external_id=?",
                (utc_now(), external_id),
            )
        self.audit(actor, "postiz.integration.default", "postiz_integration", external_id)

    def create_owned_post(
        self,
        *,
        integration_id: str,
        items: list[str],
        media: list[dict[str, Any]] | None = None,
        source_type: str = "manual",
        source_cluster_id: int | None = None,
        mode: str = "now",
        timezone_name: str = "Asia/Shanghai",
        scheduled_at_local: str | None = None,
        scheduled_at_utc: str | None = None,
        who_can_reply: str = "everyone",
        made_with_ai: bool = False,
        actor: str = "admin",
    ) -> int:
        if source_type not in {"manual", "trend"}:
            raise ValueError("Invalid owned-post source")
        if mode not in {"now", "schedule"}:
            raise ValueError("Invalid owned-post mode")
        if mode == "schedule" and not (scheduled_at_local and scheduled_at_utc):
            raise ValueError("定时发布必须同时保存北京时间和 UTC 时间")
        if who_can_reply not in {"everyone", "following", "mentionedUsers", "subscribers", "verified"}:
            raise ValueError("Invalid X reply setting")
        cleaned = [str(item).strip() for item in items]
        if not cleaned or any(not item for item in cleaned):
            raise ValueError("帖子或线程内容不能为空")
        integration = self.get_postiz_integration(integration_id)
        if not integration or integration["disabled"]:
            raise ValueError("Postiz X integration 不存在或已禁用")
        now = utc_now()
        for row in media or []:
            if str(row.get("source_type") or "") not in {"local", "url"}:
                raise ValueError("Invalid owned-post media source")
            if int(row.get("item_position") or 0) not in range(len(cleaned)):
                raise ValueError("媒体所属线程段无效")
        with self.connection() as connection:
            cursor = connection.execute(
                """
                INSERT INTO owned_posts(
                    source_type, source_cluster_id, integration_id, mode, timezone,
                    scheduled_at_local, scheduled_at_utc, who_can_reply, made_with_ai,
                    status, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?)
                """,
                (
                    source_type, source_cluster_id, integration_id, mode, timezone_name,
                    scheduled_at_local, scheduled_at_utc, who_can_reply,
                    int(made_with_ai), now, now,
                ),
            )
            post_id = int(cursor.lastrowid)
            connection.executemany(
                "INSERT INTO owned_post_items(owned_post_id, position, content) VALUES(?, ?, ?)",
                [(post_id, position, content) for position, content in enumerate(cleaned)],
            )
            for row in media or []:
                connection.execute(
                    """
                    INSERT INTO owned_post_media(
                        owned_post_id, item_position, source_type, source_value,
                        filename, content_type, storage_path, status, created_at, updated_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                    """,
                    (
                        post_id,
                        int(row.get("item_position") or 0),
                        str(row["source_type"]),
                        str(row["source_value"]),
                        row.get("filename"),
                        row.get("content_type"),
                        row.get("storage_path"),
                        now,
                        now,
                    ),
                )
        self.audit(actor, "owned_post.create", "owned_post", str(post_id), {
            "source_type": source_type, "integration_id": integration_id, "mode": mode
        })
        return post_id

    def get_owned_post(self, post_id: int) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT op.*, pi.name AS integration_name, pi.profile AS integration_profile,
                       pi.picture AS integration_picture, pi.disabled AS integration_disabled
                FROM owned_posts op
                JOIN postiz_integrations pi ON pi.external_id=op.integration_id
                WHERE op.id=?
                """,
                (post_id,),
            ).fetchone()
            if not row:
                return None
            items = connection.execute(
                "SELECT * FROM owned_post_items WHERE owned_post_id=? ORDER BY position",
                (post_id,),
            ).fetchall()
            media = connection.execute(
                "SELECT * FROM owned_post_media WHERE owned_post_id=? ORDER BY item_position, id",
                (post_id,),
            ).fetchall()
        value = dict(row)
        try:
            value["receipt"] = json.loads(value.pop("receipt_json") or "{}")
        except json.JSONDecodeError:
            value["receipt"] = {}
        value["items"] = [dict(item) for item in items]
        value["media"] = [dict(item) for item in media]
        return value

    def update_owned_post_draft(
        self,
        post_id: int,
        *,
        integration_id: str,
        items: list[str],
        media: list[dict[str, Any]],
        mode: str,
        timezone_name: str,
        scheduled_at_local: str | None,
        scheduled_at_utc: str | None,
        who_can_reply: str,
        made_with_ai: bool,
        actor: str,
    ) -> None:
        integration = self.get_postiz_integration(integration_id)
        if not integration or integration["disabled"]:
            raise ValueError("Postiz X integration 不存在或已禁用")
        cleaned = [str(item).strip() for item in items]
        if not cleaned or any(not item for item in cleaned):
            raise ValueError("帖子或线程内容不能为空")
        if mode not in {"now", "schedule"}:
            raise ValueError("Invalid owned-post mode")
        if mode == "schedule" and not (scheduled_at_local and scheduled_at_utc):
            raise ValueError("定时发布必须同时保存北京时间和 UTC 时间")
        if who_can_reply not in {"everyone", "following", "mentionedUsers", "subscribers", "verified"}:
            raise ValueError("Invalid X reply setting")
        for media_row in media:
            if str(media_row.get("source_type") or "") not in {"local", "url"}:
                raise ValueError("Invalid owned-post media source")
            if int(media_row.get("item_position") or 0) not in range(len(cleaned)):
                raise ValueError("媒体所属线程段无效")
        now = utc_now()
        with self.connection() as connection:
            row = connection.execute(
                "SELECT status FROM owned_posts WHERE id=?", (post_id,)
            ).fetchone()
            if not row:
                raise KeyError(post_id)
            if row["status"] not in {"draft", "failed"}:
                raise ValueError("只有草稿或明确失败的帖子可编辑")
            connection.execute(
                """
                UPDATE owned_posts SET integration_id=?, mode=?, timezone=?,
                    scheduled_at_local=?, scheduled_at_utc=?, who_can_reply=?,
                    made_with_ai=?, status='draft', idempotency_key=NULL,
                    approved_by=NULL, approved_at=NULL, error=NULL, updated_at=?
                WHERE id=?
                """,
                (
                    integration_id, mode, timezone_name, scheduled_at_local,
                    scheduled_at_utc, who_can_reply, int(made_with_ai), now, post_id,
                ),
            )
            connection.execute("DELETE FROM owned_post_items WHERE owned_post_id=?", (post_id,))
            connection.execute("DELETE FROM owned_post_media WHERE owned_post_id=?", (post_id,))
            connection.executemany(
                "INSERT INTO owned_post_items(owned_post_id, position, content) VALUES(?, ?, ?)",
                [(post_id, position, content) for position, content in enumerate(cleaned)],
            )
            for media_row in media:
                connection.execute(
                    """
                    INSERT INTO owned_post_media(
                        owned_post_id, item_position, source_type, source_value,
                        filename, content_type, storage_path, status, created_at, updated_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                    """,
                    (
                        post_id, int(media_row.get("item_position") or 0),
                        str(media_row["source_type"]), str(media_row["source_value"]),
                        media_row.get("filename"), media_row.get("content_type"),
                        media_row.get("storage_path"), now, now,
                    ),
                )
        self.audit(actor, "owned_post.update", "owned_post", str(post_id))

    def list_owned_posts(self, *, status: str = "all", limit: int = 100) -> list[dict[str, Any]]:
        sql = "SELECT id FROM owned_posts"
        params: list[Any] = []
        if status != "all":
            sql += " WHERE status=?"
            params.append(status)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, min(limit, 500)))
        with self.connection() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [post for row in rows if (post := self.get_owned_post(int(row["id"]))) is not None]

    def approve_owned_post(self, post_id: int, *, idempotency_key: str, actor: str) -> None:
        now = utc_now()
        try:
            with self.connection() as connection:
                row = connection.execute(
                    "SELECT status FROM owned_posts WHERE id=?", (post_id,)
                ).fetchone()
                if not row:
                    raise KeyError(post_id)
                if row["status"] not in {"draft", "failed"}:
                    raise ValueError("只有草稿或明确失败的帖子可审批")
                connection.execute(
                    """
                    UPDATE owned_posts SET status='approved', idempotency_key=?,
                        approved_by=?, approved_at=?, error=NULL, updated_at=? WHERE id=?
                    """,
                    (idempotency_key, actor, now, now, post_id),
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError("相同账号、内容和发布时间的帖子已存在") from exc
        self.audit(actor, "owned_post.approve", "owned_post", str(post_id))

    def claim_owned_post(self, post_id: int) -> dict[str, Any]:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT status FROM owned_posts WHERE id=?", (post_id,)
            ).fetchone()
            if not row:
                raise KeyError(post_id)
            if row["status"] != "approved":
                raise ValueError("帖子尚未逐条审批")
            connection.execute(
                "UPDATE owned_posts SET status='submitting', submitted_at=?, updated_at=? WHERE id=?",
                (utc_now(), utc_now(), post_id),
            )
        claimed = self.get_owned_post(post_id)
        if claimed is None:
            raise KeyError(post_id)
        return claimed

    def update_owned_media_result(
        self,
        media_id: int,
        *,
        status: str,
        asset_id: str | None = None,
        asset_path: str | None = None,
        error: str | None = None,
    ) -> None:
        with self.connection() as connection:
            connection.execute(
                """
                UPDATE owned_post_media SET status=?, postiz_asset_id=?,
                    postiz_asset_path=?, error=?, updated_at=? WHERE id=?
                """,
                (status, asset_id, asset_path, error, utc_now(), media_id),
            )

    def finish_owned_post(
        self,
        post_id: int,
        *,
        status: str,
        postiz_post_id: str | None = None,
        release_url: str | None = None,
        receipt: dict[str, Any] | list[Any] | None = None,
        error: str | None = None,
        actor: str = "system",
    ) -> None:
        if status not in {"scheduled", "published", "confirmation_required", "failed", "paused", "cancelled"}:
            raise ValueError("Invalid owned-post completion status")
        now = utc_now()
        with self.connection() as connection:
            cursor = connection.execute(
                """
                UPDATE owned_posts SET status=?,
                    postiz_post_id=COALESCE(?, postiz_post_id),
                    release_url=COALESCE(?, release_url), receipt_json=?, error=?,
                    published_at=CASE WHEN ?='published' THEN ? ELSE published_at END,
                    updated_at=? WHERE id=?
                """,
                (
                    status, postiz_post_id, release_url, _json(receipt or {}), error,
                    status, now, now, post_id,
                ),
            )
            if not cursor.rowcount:
                raise KeyError(post_id)
        self.audit(actor, f"owned_post.{status}", "owned_post", str(post_id), {
            "postiz_post_id": postiz_post_id, "release_url": release_url, "error": error
        })
