from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from kol_search.discovery.promotion import KolStatus
from kol_search.database import DatabaseTarget
from kol_search.platform_modules.adapter_utils import stable_native_id
from kol_search.platform_modules.native_db import NativePlatformDatabase
from kol_search.platforms.xiaohongshu import (
    XiaohongshuComment,
    XiaohongshuNote,
    XiaohongshuTrend,
    XiaohongshuUser,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


XHS_SCHEMA = """
CREATE TABLE IF NOT EXISTS xhs_users (
    id TEXT PRIMARY KEY,
    nickname TEXT NOT NULL,
    native_id_resolved INTEGER NOT NULL DEFAULT 1,
    bio TEXT,
    followers_count INTEGER NOT NULL DEFAULT 0,
    following_count INTEGER NOT NULL DEFAULT 0,
    verified INTEGER NOT NULL DEFAULT 0,
    protected INTEGER NOT NULL DEFAULT 0,
    disabled INTEGER NOT NULL DEFAULT 0,
    spam_risk REAL NOT NULL DEFAULT 0,
    anomaly_signals_json TEXT NOT NULL DEFAULT '[]',
    profile_url TEXT,
    avatar_url TEXT,
    source_provider TEXT NOT NULL,
    captured_at TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS xhs_kols (
    user_id TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'candidate',
    score REAL NOT NULL DEFAULT 0,
    reasons_json TEXT NOT NULL DEFAULT '[]',
    last_qualified_at TEXT,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(user_id) REFERENCES xhs_users(id)
);
CREATE INDEX IF NOT EXISTS idx_xhs_kols_status_score ON xhs_kols(status, score DESC);
CREATE TABLE IF NOT EXISTS xhs_notes (
    id TEXT PRIMARY KEY,
    author_id TEXT NOT NULL,
    note_type TEXT NOT NULL DEFAULT 'unknown',
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    published_at TEXT,
    like_count INTEGER NOT NULL DEFAULT 0,
    comment_count INTEGER NOT NULL DEFAULT 0,
    collect_count INTEGER NOT NULL DEFAULT 0,
    view_count INTEGER NOT NULL DEFAULT 0,
    relevance_score REAL NOT NULL DEFAULT 0,
    note_url TEXT,
    source_provider TEXT NOT NULL,
    captured_at TEXT,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(author_id) REFERENCES xhs_users(id)
);
CREATE INDEX IF NOT EXISTS idx_xhs_notes_author_time ON xhs_notes(author_id, published_at);
CREATE TABLE IF NOT EXISTS xhs_note_metrics (
    note_id TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    like_count INTEGER NOT NULL,
    comment_count INTEGER NOT NULL,
    collect_count INTEGER NOT NULL,
    view_count INTEGER NOT NULL,
    PRIMARY KEY(note_id, captured_at),
    FOREIGN KEY(note_id) REFERENCES xhs_notes(id)
);
CREATE TABLE IF NOT EXISTS xhs_comments (
    id TEXT PRIMARY KEY,
    note_id TEXT NOT NULL,
    author_id TEXT NOT NULL,
    author_nickname TEXT,
    body TEXT NOT NULL,
    created_at TEXT,
    like_count INTEGER NOT NULL DEFAULT 0,
    reply_count INTEGER NOT NULL DEFAULT 0,
    source_provider TEXT NOT NULL,
    captured_at TEXT,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(note_id) REFERENCES xhs_notes(id)
);
CREATE INDEX IF NOT EXISTS idx_xhs_comments_note_time
ON xhs_comments(note_id, created_at);
CREATE TABLE IF NOT EXISTS xhs_discovery_evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_user_id TEXT,
    target_user_id TEXT NOT NULL,
    relation_type TEXT NOT NULL,
    evidence TEXT NOT NULL,
    evidence_url TEXT,
    weight REAL NOT NULL DEFAULT 0,
    observed_at TEXT NOT NULL,
    UNIQUE(source_user_id, target_user_id, relation_type, evidence)
);

CREATE TABLE IF NOT EXISTS xhs_trends (
    id TEXT NOT NULL,
    name TEXT NOT NULL,
    rank INTEGER NOT NULL DEFAULT 0,
    note_count INTEGER NOT NULL DEFAULT 0,
    url TEXT,
    captured_at TEXT NOT NULL,
    PRIMARY KEY(id, captured_at)
);
CREATE INDEX IF NOT EXISTS idx_xhs_trends_latest
ON xhs_trends(id, captured_at DESC);
"""


def install_xiaohongshu_schema(connection: sqlite3.Connection) -> None:
    """Install the Xiaohongshu-owned schema on the supplied connection."""

    connection.executescript(XHS_SCHEMA)
    _ensure_columns(
        connection,
        "xhs_users",
        {
            "native_id_resolved": "INTEGER NOT NULL DEFAULT 1",
            "protected": "INTEGER NOT NULL DEFAULT 0",
            "disabled": "INTEGER NOT NULL DEFAULT 0",
            "spam_risk": "REAL NOT NULL DEFAULT 0",
            "anomaly_signals_json": "TEXT NOT NULL DEFAULT '[]'",
        },
    )
    _ensure_columns(
        connection,
        "xhs_notes",
        {"relevance_score": "REAL NOT NULL DEFAULT 0"},
    )
    # Before identity resolution was explicit, OpenCLI author-name fallbacks
    # were stored as 20-character local hashes. Mark only that legacy shape as
    # unresolved so existing databases stop sending it to native user feeds.
    connection.execute(
        """
        UPDATE xhs_users SET native_id_resolved=0
        WHERE source_provider='opencli'
          AND profile_url IS NULL
          AND length(id)=20
          AND id NOT GLOB '*[^0-9a-f]*'
        """
    )
    columns = {
        str(row[1])
        for row in connection.execute("PRAGMA table_info(xhs_notes)").fetchall()
    }
    if "note_type" not in columns:
        connection.execute(
            "ALTER TABLE xhs_notes ADD COLUMN note_type TEXT NOT NULL DEFAULT 'unknown'"
        )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS xhs_schema_migrations(
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        "INSERT OR IGNORE INTO xhs_schema_migrations VALUES(2, ?, ?)",
        ("native_users_notes_comments_and_trends", _now()),
    )
    connection.execute(
        "INSERT OR IGNORE INTO xhs_schema_migrations VALUES(3, ?, ?)",
        ("content_relevance_score", _now()),
    )
    connection.execute(
        "INSERT OR IGNORE INTO xhs_schema_migrations VALUES(4, ?, ?)",
        ("account_safety_signals", _now()),
    )
    connection.execute(
        "INSERT OR IGNORE INTO xhs_schema_migrations VALUES(5, ?, ?)",
        ("native_account_identity_resolution", _now()),
    )


def _ensure_columns(
    connection: sqlite3.Connection, table: str, columns: dict[str, str]
) -> None:
    existing = {
        str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
    }
    for name, declaration in columns.items():
        if name not in existing:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")


class XiaohongshuRepository:
    platform_id = "xiaohongshu"

    def __init__(self, path: DatabaseTarget) -> None:
        self.database = NativePlatformDatabase(path)
        if not self.database.is_postgres:
            self.migrate()

    def migrate(self) -> None:
        with self.database.transaction() as connection:
            install_xiaohongshu_schema(connection)

    @contextmanager
    def unit_of_work(self) -> Iterator[None]:
        """Let the platform service own one atomic projection transaction."""

        with self.database.transaction():
            yield

    def upsert_user(self, user: XiaohongshuUser) -> None:
        now = _now()
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO xhs_users(
                    id, nickname, native_id_resolved, bio,
                    followers_count, following_count, verified,
                    protected, disabled, spam_risk, anomaly_signals_json,
                    profile_url, avatar_url, source_provider, captured_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    nickname=excluded.nickname,
                    native_id_resolved=excluded.native_id_resolved,
                    bio=excluded.bio,
                    followers_count=excluded.followers_count,
                    following_count=excluded.following_count, verified=excluded.verified,
                    protected=excluded.protected, disabled=excluded.disabled,
                    spam_risk=excluded.spam_risk,
                    anomaly_signals_json=excluded.anomaly_signals_json,
                    profile_url=excluded.profile_url, avatar_url=excluded.avatar_url,
                    source_provider=excluded.source_provider,
                    captured_at=excluded.captured_at, updated_at=excluded.updated_at
                """,
                (
                    user.external_id, user.nickname, int(user.native_id_resolved),
                    user.bio, user.followers_count, user.following_count,
                    int(user.verified), int(user.protected),
                    int(user.disabled), user.spam_risk,
                    json.dumps(user.anomaly_signals, ensure_ascii=False), user.profile_url,
                    user.avatar_url, user.source_provider, user.captured_at or now, now,
                ),
            )
            connection.execute(
                """INSERT INTO xhs_kols(user_id, updated_at) VALUES(?, ?)
                ON CONFLICT(user_id) DO NOTHING""",
                (user.external_id, now),
            )

    def upsert_notes(self, notes: list[XiaohongshuNote]) -> None:
        if not notes:
            return
        now = _now()
        with self.database.transaction() as connection:
            for note in notes:
                connection.execute(
                    """
                    INSERT INTO xhs_notes(
                        id, author_id, note_type, title, body, published_at,
                        like_count, comment_count, collect_count, view_count,
                        relevance_score, note_url, source_provider, captured_at, updated_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        author_id=excluded.author_id, note_type=excluded.note_type,
                        title=excluded.title, body=excluded.body,
                        published_at=excluded.published_at, like_count=excluded.like_count,
                        comment_count=excluded.comment_count,
                        collect_count=excluded.collect_count, view_count=excluded.view_count,
                        relevance_score=excluded.relevance_score,
                        note_url=excluded.note_url, source_provider=excluded.source_provider,
                        captured_at=excluded.captured_at, updated_at=excluded.updated_at
                    """,
                    (
                        note.external_id, note.author_external_id, note.note_type,
                        note.title, note.body,
                        note.published_at, note.metrics.likes, note.metrics.comments,
                        note.metrics.collects, note.metrics.views, note.relevance_score, note.url,
                        note.source_provider, note.captured_at or now, now,
                    ),
                )
                connection.execute(
                    """INSERT INTO xhs_note_metrics VALUES(?, ?, ?, ?, ?, ?)
                    ON CONFLICT(note_id, captured_at) DO NOTHING""",
                    (
                        note.external_id, note.captured_at or now, note.metrics.likes,
                        note.metrics.comments, note.metrics.collects, note.metrics.views,
                    ),
                )

    def upsert_comments(self, comments: list[XiaohongshuComment]) -> None:
        if not comments:
            return
        now = _now()
        with self.database.transaction() as connection:
            connection.executemany(
                """
                INSERT INTO xhs_comments(
                    id, note_id, author_id, author_nickname, body, created_at,
                    like_count, reply_count, source_provider, captured_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    note_id=excluded.note_id, author_id=excluded.author_id,
                    author_nickname=excluded.author_nickname, body=excluded.body,
                    created_at=excluded.created_at, like_count=excluded.like_count,
                    reply_count=excluded.reply_count,
                    source_provider=excluded.source_provider,
                    captured_at=excluded.captured_at, updated_at=excluded.updated_at
                """,
                [
                    (
                        item.external_id,
                        item.note_external_id,
                        item.author_external_id,
                        item.author_nickname,
                        item.body,
                        item.created_at,
                        item.likes,
                        item.replies,
                        item.source_provider,
                        item.captured_at or now,
                        now,
                    )
                    for item in comments
                ],
            )

    def upsert_trends(self, trends: list[XiaohongshuTrend]) -> None:
        if not trends:
            return
        captured_at = _now()
        with self.database.transaction() as connection:
            connection.executemany(
                """
                INSERT INTO xhs_trends(
                    id, name, rank, note_count, url, captured_at
                ) VALUES(?, ?, ?, ?, ?, ?)
                ON CONFLICT(id, captured_at) DO UPDATE SET
                    name=excluded.name, rank=excluded.rank,
                    note_count=excluded.note_count, url=excluded.url
                """,
                [
                    (
                        stable_native_id("xiaohongshu", item.name.casefold()),
                        item.name,
                        item.rank,
                        item.note_count,
                        item.url,
                        captured_at,
                    )
                    for item in trends
                ],
            )

    def record_evidence(
        self,
        *,
        target_user_id: str,
        relation_type: str,
        evidence: str,
        source_user_id: str | None = None,
        evidence_url: str | None = None,
        weight: float = 0.0,
        observed_at: str | None = None,
    ) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO xhs_discovery_evidence(
                    source_user_id, target_user_id, relation_type, evidence,
                    evidence_url, weight, observed_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_user_id, target_user_id, relation_type, evidence)
                DO NOTHING
                """,
                (
                    source_user_id, target_user_id, relation_type, evidence,
                    evidence_url, weight, observed_at or _now(),
                ),
            )

    def record_comment_relationship_evidence(
        self, comments: list[XiaohongshuComment]
    ) -> int:
        """Persist commenters on seed/active notes as trusted discovery edges."""

        recorded = 0
        with self.database.transaction() as connection:
            for comment in comments:
                if not comment.author_external_id or comment.author_external_id == "unknown":
                    continue
                source = connection.execute(
                    """
                    SELECT n.author_id, k.status
                    FROM xhs_notes n
                    JOIN xhs_kols k ON k.user_id=n.author_id
                    WHERE n.id=?
                    """,
                    (comment.note_external_id,),
                ).fetchone()
                if (
                    source is None
                    or source["status"] not in {
                        KolStatus.SEED.value,
                        KolStatus.ACTIVE.value,
                    }
                    or source["author_id"] == comment.author_external_id
                ):
                    continue
                cursor = connection.execute(
                    """
                    INSERT INTO xhs_discovery_evidence(
                        source_user_id, target_user_id, relation_type, evidence,
                        evidence_url, weight, observed_at
                    ) VALUES(?, ?, 'seed_relation', ?, NULL, 0.20, ?)
                    ON CONFLICT(source_user_id, target_user_id, relation_type, evidence)
                    DO NOTHING
                    """,
                    (
                        source["author_id"],
                        comment.author_external_id,
                        f"commented_on:{comment.note_external_id}",
                        comment.captured_at or comment.created_at or _now(),
                    ),
                )
                recorded += int(cursor.rowcount)
        return recorded

    def relationship_evidence_count(self, user_id: str) -> int:
        """Return durable seed/active relationship evidence for one RED user."""

        with self.database.transaction() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS count FROM xhs_discovery_evidence
                WHERE target_user_id=? AND relation_type='seed_relation'
                """,
                (user_id,),
            ).fetchone()
        return int(row["count"])

    def recent_relevant_content_count(self, user_id: str, *, days: int = 30) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max(1, days))).isoformat()
        with self.database.transaction() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS count FROM xhs_notes
                WHERE author_id=? AND published_at>=? AND relevance_score>=0.5
                """,
                (user_id, cutoff),
            ).fetchone()
        return int(row["count"])

    def set_kol_status(
        self,
        user_id: str,
        status: KolStatus | str,
        *,
        score: float,
        reasons: tuple[str, ...] = (),
        qualified: bool | None = None,
    ) -> None:
        now = _now()
        value = status.value if isinstance(status, KolStatus) else status
        refresh_qualified_at = (
            value in {KolStatus.SEED.value, KolStatus.ACTIVE.value}
            if qualified is None
            else qualified
        )
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE xhs_kols SET status=?, score=?, reasons_json=?,
                    last_qualified_at=CASE WHEN ? THEN ? ELSE last_qualified_at END,
                    updated_at=? WHERE user_id=?
                """,
                (
                    value,
                    score,
                    json.dumps(reasons),
                    int(refresh_qualified_at),
                    now,
                    now,
                    user_id,
                ),
            )
            if not cursor.rowcount:
                raise KeyError(user_id)

    def get_kol(self, user_id: str) -> dict[str, Any] | None:
        with self.database.transaction() as connection:
            row = connection.execute(
                """
                SELECT u.*, k.status, k.score, k.reasons_json, k.last_qualified_at
                FROM xhs_kols k JOIN xhs_users u ON u.id=k.user_id
                WHERE k.user_id=?
                """,
                (user_id,),
            ).fetchone()
        if row is None:
            return None
        output = dict(row)
        output["reasons"] = json.loads(output.pop("reasons_json") or "[]")
        output["anomaly_signals"] = json.loads(
            output.pop("anomaly_signals_json") or "[]"
        )
        return output

    def list_kols(self, status: str = "all", limit: int = 100) -> list[dict[str, Any]]:
        where = "" if status == "all" else "WHERE k.status=?"
        params: list[Any] = [] if status == "all" else [status]
        params.append(max(1, min(limit, 500)))
        with self.database.transaction() as connection:
            rows = connection.execute(
                f"""
                SELECT u.*, k.status, k.score, k.reasons_json, k.last_qualified_at
                FROM xhs_kols k JOIN xhs_users u ON u.id=k.user_id
                {where} ORDER BY k.score DESC, u.followers_count DESC LIMIT ?
                """,
                params,
            ).fetchall()
        output = [dict(row) for row in rows]
        for row in output:
            row["reasons"] = json.loads(row.pop("reasons_json") or "[]")
            row["anomaly_signals"] = json.loads(
                row.pop("anomaly_signals_json") or "[]"
            )
        return output

    def list_scan_targets(
        self,
        *,
        statuses: tuple[str, ...],
        after: str | None,
        limit: int,
    ) -> tuple[list[dict[str, Any]], str | None]:
        values = tuple(dict.fromkeys(statuses))
        if not values:
            return [], None
        batch_size = max(1, limit)
        clauses = [
            f"k.status IN ({','.join('?' for _ in values)})",
            "u.native_id_resolved=1",
        ]
        params: list[Any] = list(values)
        if after:
            clauses.append("u.id>?")
            params.append(after)
        params.append(batch_size + 1)
        with self.database.transaction() as connection:
            rows = connection.execute(
                f"""
                SELECT u.*, k.status, k.score, k.reasons_json, k.last_qualified_at
                FROM xhs_kols k JOIN xhs_users u ON u.id=k.user_id
                WHERE {' AND '.join(clauses)}
                ORDER BY u.id ASC LIMIT ?
                """,
                params,
            ).fetchall()
        output = [dict(row) for row in rows[:batch_size]]
        for row in output:
            row["reasons"] = json.loads(row.pop("reasons_json") or "[]")
            row["anomaly_signals"] = json.loads(
                row.pop("anomaly_signals_json") or "[]"
            )
        next_cursor = str(output[-1]["id"]) if len(rows) > batch_size else None
        return output, next_cursor

    def summary(self) -> dict[str, int]:
        with self.database.transaction() as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) AS count FROM xhs_kols GROUP BY status"
            ).fetchall()
            notes = connection.execute(
                "SELECT COUNT(*) AS count FROM xhs_notes"
            ).fetchone()["count"]
            comments = connection.execute(
                "SELECT COUNT(*) AS count FROM xhs_comments"
            ).fetchone()["count"]
            trends = connection.execute(
                "SELECT COUNT(DISTINCT id) AS count FROM xhs_trends"
            ).fetchone()["count"]
        result = {str(row["status"]): int(row["count"]) for row in rows}
        result["content"] = int(notes)
        result["comments"] = int(comments)
        result["trends"] = int(trends)
        return result

    def pause_inactive(self, *, days: int) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max(1, days))).isoformat()
        now = _now()
        reasons = json.dumps((f"no qualifying activity for at least {max(1, days)} days",))
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE xhs_kols SET status='paused', reasons_json=?, updated_at=?
                WHERE status='active' AND COALESCE(last_qualified_at, updated_at)<?
                """,
                (reasons, now, cutoff),
            )
        return int(cursor.rowcount)
