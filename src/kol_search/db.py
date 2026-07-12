from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
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
    lang TEXT,
    raw_json TEXT NOT NULL DEFAULT '{}',
    fetched_at TEXT NOT NULL
);

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
}

EDGE_COLUMNS: dict[str, str] = {
    "source_account_id": "TEXT",
    "target_account_id": "TEXT",
    "edge_type": "TEXT",
    "evidence_url": "TEXT",
    "first_seen_at": "TEXT",
    "last_seen_at": "TEXT",
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
            edge_existing = {
                row[1] for row in connection.execute("PRAGMA table_info(discovery_edges)")
            }
            for name, definition in EDGE_COLUMNS.items():
                if name not in edge_existing:
                    connection.execute(
                        f"ALTER TABLE discovery_edges ADD COLUMN {name} {definition}"
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

    def save_posts(self, posts: list[Post]) -> None:
        if not posts:
            return
        now = utc_now()
        for post in posts:
            if post.author_username:
                post.author_id = self.resolve_account_id(post.author_username, post.author_id)
        with self.connection() as connection:
            connection.executemany(
                """
                INSERT INTO posts (
                    id, author_id, author_username, text, created_at, like_count, retweet_count,
                    reply_count, quote_count, lang, raw_json, fetched_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    text=excluded.text, like_count=excluded.like_count,
                    retweet_count=excluded.retweet_count, reply_count=excluded.reply_count,
                    quote_count=excluded.quote_count, raw_json=excluded.raw_json,
                    fetched_at=excluded.fetched_at
                """,
                [
                    (
                        post.id, post.author_id, post.author_username, post.text, post.created_at,
                        post.like_count, post.retweet_count, post.reply_count, post.quote_count,
                        post.lang, _json(post.raw), now,
                    )
                    for post in posts if post.id
                ],
            )

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
