from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from kol_search.discovery.promotion import KolStatus
from kol_search.database import DatabaseTarget
from kol_search.platform_modules.native_db import NativePlatformDatabase
from kol_search.platform_modules.adapter_utils import stable_native_id
from kol_search.platforms.x import XAccount, XTrend, XTrendTweet, XTweet


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


X_SCHEMA = """
CREATE TABLE IF NOT EXISTS x_accounts (
    id TEXT PRIMARY KEY,
    handle TEXT NOT NULL,
    display_name TEXT,
    bio TEXT,
    followers_count INTEGER NOT NULL DEFAULT 0,
    following_count INTEGER NOT NULL DEFAULT 0,
    tweet_count INTEGER NOT NULL DEFAULT 0,
    listed_count INTEGER NOT NULL DEFAULT 0,
    verified INTEGER NOT NULL DEFAULT 0,
    protected INTEGER NOT NULL DEFAULT 0,
    disabled INTEGER NOT NULL DEFAULT 0,
    spam_risk REAL NOT NULL DEFAULT 0,
    anomaly_signals_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT,
    profile_url TEXT,
    avatar_url TEXT,
    source_provider TEXT NOT NULL,
    captured_at TEXT,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_x_accounts_handle
ON x_accounts(handle COLLATE NOCASE);

CREATE TABLE IF NOT EXISTS x_kols (
    account_id TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'candidate',
    score REAL NOT NULL DEFAULT 0,
    reasons_json TEXT NOT NULL DEFAULT '[]',
    last_qualified_at TEXT,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(account_id) REFERENCES x_accounts(id)
);
CREATE INDEX IF NOT EXISTS idx_x_kols_status_score ON x_kols(status, score DESC);

CREATE TABLE IF NOT EXISTS x_tweets (
    id TEXT PRIMARY KEY,
    author_id TEXT NOT NULL,
    author_handle TEXT,
    text TEXT NOT NULL,
    language TEXT,
    created_at TEXT,
    conversation_id TEXT,
    in_reply_to_user_id TEXT,
    reference_type TEXT,
    referenced_tweet_id TEXT,
    mentioned_usernames_json TEXT NOT NULL DEFAULT '[]',
    relevance_score REAL NOT NULL DEFAULT 0,
    like_count INTEGER NOT NULL DEFAULT 0,
    repost_count INTEGER NOT NULL DEFAULT 0,
    reply_count INTEGER NOT NULL DEFAULT 0,
    quote_count INTEGER NOT NULL DEFAULT 0,
    bookmark_count INTEGER NOT NULL DEFAULT 0,
    view_count INTEGER NOT NULL DEFAULT 0,
    url TEXT,
    source_provider TEXT NOT NULL,
    captured_at TEXT,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(author_id) REFERENCES x_accounts(id)
);
CREATE INDEX IF NOT EXISTS idx_x_tweets_author_time ON x_tweets(author_id, created_at);

CREATE TABLE IF NOT EXISTS x_tweet_metrics (
    tweet_id TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    like_count INTEGER NOT NULL,
    repost_count INTEGER NOT NULL,
    reply_count INTEGER NOT NULL,
    quote_count INTEGER NOT NULL,
    bookmark_count INTEGER NOT NULL DEFAULT 0,
    view_count INTEGER NOT NULL,
    PRIMARY KEY(tweet_id, captured_at),
    FOREIGN KEY(tweet_id) REFERENCES x_tweets(id)
);

CREATE TABLE IF NOT EXISTS x_discovery_evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_account_id TEXT,
    target_account_id TEXT NOT NULL,
    relation_type TEXT NOT NULL,
    evidence TEXT NOT NULL,
    evidence_url TEXT,
    weight REAL NOT NULL DEFAULT 0,
    observed_at TEXT NOT NULL,
    UNIQUE(source_account_id, target_account_id, relation_type, evidence)
);

CREATE TABLE IF NOT EXISTS x_trends (
    id TEXT NOT NULL,
    name TEXT NOT NULL,
    rank INTEGER NOT NULL DEFAULT 0,
    post_count INTEGER NOT NULL DEFAULT 0,
    url TEXT,
    captured_at TEXT NOT NULL,
    PRIMARY KEY(id, captured_at)
);
CREATE INDEX IF NOT EXISTS idx_x_trends_latest ON x_trends(id, captured_at DESC);

CREATE TABLE IF NOT EXISTS x_trend_tweets (
    trend_id TEXT NOT NULL,
    trend_name TEXT NOT NULL,
    trend_rank INTEGER NOT NULL DEFAULT 0,
    tweet_id TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    PRIMARY KEY(trend_id, tweet_id),
    FOREIGN KEY(tweet_id) REFERENCES x_tweets(id)
);
CREATE INDEX IF NOT EXISTS idx_x_trend_tweets_recent
ON x_trend_tweets(last_seen_at DESC, trend_rank ASC);
"""


def install_x_schema(connection: sqlite3.Connection) -> None:
    """Install the X-owned schema on the exact connection supplied by the core."""

    connection.executescript(X_SCHEMA)
    _ensure_columns(
        connection,
        "x_accounts",
        {
            "listed_count": "INTEGER NOT NULL DEFAULT 0",
            "created_at": "TEXT",
            "disabled": "INTEGER NOT NULL DEFAULT 0",
            "spam_risk": "REAL NOT NULL DEFAULT 0",
            "anomaly_signals_json": "TEXT NOT NULL DEFAULT '[]'",
        },
    )
    _ensure_columns(
        connection,
        "x_tweets",
        {
            "language": "TEXT",
            "in_reply_to_user_id": "TEXT",
            "mentioned_usernames_json": "TEXT NOT NULL DEFAULT '[]'",
            "bookmark_count": "INTEGER NOT NULL DEFAULT 0",
            "relevance_score": "REAL NOT NULL DEFAULT 0",
        },
    )
    _ensure_columns(
        connection,
        "x_tweet_metrics",
        {"bookmark_count": "INTEGER NOT NULL DEFAULT 0"},
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS x_schema_migrations(
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        "INSERT OR IGNORE INTO x_schema_migrations VALUES(2, ?, ?)",
        ("native_accounts_content_relations_and_trends", _now()),
    )
    connection.execute(
        "INSERT OR IGNORE INTO x_schema_migrations VALUES(3, ?, ?)",
        ("content_relevance_score", _now()),
    )
    connection.execute(
        "INSERT OR IGNORE INTO x_schema_migrations VALUES(4, ?, ?)",
        ("account_safety_signals", _now()),
    )
    connection.execute(
        "INSERT OR IGNORE INTO x_schema_migrations VALUES(5, ?, ?)",
        ("native_trend_tweet_links", _now()),
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


class XRepository:
    """Persistence owned by the X platform module."""

    platform_id = "x"

    def __init__(self, path: DatabaseTarget) -> None:
        self.database = NativePlatformDatabase(path)
        if not self.database.is_postgres:
            self.migrate()

    def migrate(self) -> None:
        with self.database.transaction() as connection:
            install_x_schema(connection)

    @contextmanager
    def unit_of_work(self) -> Iterator[None]:
        """Let the platform service own one atomic projection transaction."""

        with self.database.transaction():
            yield

    def upsert_account(self, account: XAccount, *, track_as_kol: bool = True) -> str:
        now = _now()
        handle = account.username.lstrip("@")
        with self.database.transaction() as connection:
            existing = connection.execute(
                "SELECT id FROM x_accounts WHERE lower(handle)=lower(?) LIMIT 1",
                (handle,),
            ).fetchone()
            account_id = str(existing["id"]) if existing is not None else account.external_id
            connection.execute(
                """
                INSERT INTO x_accounts(
                    id, handle, display_name, bio, followers_count, following_count,
                    tweet_count, listed_count, verified, protected, disabled, spam_risk,
                    anomaly_signals_json, created_at,
                    profile_url, avatar_url, source_provider, captured_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    handle=excluded.handle, display_name=excluded.display_name,
                    bio=excluded.bio, followers_count=excluded.followers_count,
                    following_count=excluded.following_count, tweet_count=excluded.tweet_count,
                    listed_count=excluded.listed_count,
                    verified=excluded.verified, protected=excluded.protected,
                    disabled=excluded.disabled, spam_risk=excluded.spam_risk,
                    anomaly_signals_json=excluded.anomaly_signals_json,
                    created_at=excluded.created_at,
                    profile_url=excluded.profile_url, avatar_url=excluded.avatar_url,
                    source_provider=excluded.source_provider, captured_at=excluded.captured_at,
                    updated_at=excluded.updated_at
                """,
                (
                    account_id, handle, account.display_name,
                    account.description,
                    account.followers_count, account.following_count, account.tweet_count,
                    account.listed_count, int(account.verified), int(account.protected),
                    int(account.disabled), account.spam_risk,
                    json.dumps(account.anomaly_signals, ensure_ascii=False),
                    account.created_at, account.url,
                    account.profile_image_url, account.source_provider,
                    account.captured_at or now, now,
                ),
            )
            if track_as_kol:
                connection.execute(
                    """INSERT INTO x_kols(account_id, updated_at) VALUES(?, ?)
                    ON CONFLICT(account_id) DO NOTHING""",
                    (account_id, now),
                )
        return account_id

    def upsert_tweets(self, tweets: list[XTweet]) -> None:
        if not tweets:
            return
        now = _now()
        with self.database.transaction() as connection:
            for tweet in tweets:
                connection.execute(
                    """
                    INSERT INTO x_tweets(
                        id, author_id, author_handle, text, language, created_at,
                        conversation_id, in_reply_to_user_id, reference_type,
                        referenced_tweet_id, mentioned_usernames_json, relevance_score, like_count,
                        repost_count, reply_count, quote_count, bookmark_count,
                        view_count, url, source_provider, captured_at, updated_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        author_id=excluded.author_id, author_handle=excluded.author_handle,
                        text=excluded.text, language=excluded.language,
                        created_at=excluded.created_at,
                        conversation_id=excluded.conversation_id,
                        in_reply_to_user_id=excluded.in_reply_to_user_id,
                        reference_type=excluded.reference_type,
                        referenced_tweet_id=excluded.referenced_tweet_id,
                        mentioned_usernames_json=excluded.mentioned_usernames_json,
                        relevance_score=excluded.relevance_score,
                        like_count=excluded.like_count, repost_count=excluded.repost_count,
                        reply_count=excluded.reply_count, quote_count=excluded.quote_count,
                        bookmark_count=excluded.bookmark_count,
                        view_count=excluded.view_count, url=excluded.url,
                        source_provider=excluded.source_provider,
                        captured_at=excluded.captured_at, updated_at=excluded.updated_at
                    """,
                    (
                        tweet.external_id, tweet.author_external_id, tweet.author_username,
                        tweet.text, tweet.language, tweet.created_at,
                        tweet.conversation_id, tweet.in_reply_to_user_id,
                        tweet.reference_type, tweet.referenced_tweet_id,
                        json.dumps(tweet.mentioned_usernames, ensure_ascii=False),
                        tweet.relevance_score,
                        tweet.metrics.likes,
                        tweet.metrics.reposts, tweet.metrics.replies,
                        tweet.metrics.quotes, tweet.metrics.bookmarks,
                        tweet.metrics.views, tweet.url,
                        tweet.source_provider, tweet.captured_at or now, now,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO x_tweet_metrics(
                        tweet_id, captured_at, like_count, repost_count, reply_count,
                        quote_count, bookmark_count, view_count
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(tweet_id, captured_at) DO NOTHING
                    """,
                    (
                        tweet.external_id, tweet.captured_at or now, tweet.metrics.likes,
                        tweet.metrics.reposts, tweet.metrics.replies,
                        tweet.metrics.quotes, tweet.metrics.bookmarks,
                        tweet.metrics.views,
                    ),
                )

    def upsert_trends(self, trends: list[XTrend]) -> None:
        if not trends:
            return
        captured_at = _now()
        with self.database.transaction() as connection:
            connection.executemany(
                """
                INSERT INTO x_trends(
                    id, name, rank, post_count, url, captured_at
                ) VALUES(?, ?, ?, ?, ?, ?)
                ON CONFLICT(id, captured_at) DO UPDATE SET
                    name=excluded.name, rank=excluded.rank,
                    post_count=excluded.post_count, url=excluded.url
                """,
                [
                    (
                        stable_native_id("x", item.name.casefold()),
                        item.name,
                        item.rank,
                        item.post_count,
                        item.url,
                        captured_at,
                    )
                    for item in trends
                ],
            )

    def upsert_trend_tweets(self, links: list[XTrendTweet]) -> None:
        if not links:
            return
        observed_at = _now()
        with self.database.transaction() as connection:
            connection.executemany(
                """
                INSERT INTO x_trend_tweets(
                    trend_id, trend_name, trend_rank, tweet_id,
                    first_seen_at, last_seen_at
                ) VALUES(?, ?, ?, ?, ?, ?)
                ON CONFLICT(trend_id, tweet_id) DO UPDATE SET
                    trend_name=excluded.trend_name,
                    trend_rank=excluded.trend_rank,
                    last_seen_at=excluded.last_seen_at
                """,
                [
                    (
                        stable_native_id("x", item.trend_name.casefold()),
                        item.trend_name,
                        item.trend_rank,
                        item.tweet.external_id,
                        observed_at,
                        observed_at,
                    )
                    for item in links
                ],
            )

    def record_evidence(self, values: list[dict[str, Any]]) -> None:
        with self.database.transaction() as connection:
            for value in values:
                connection.execute(
                    """
                    INSERT INTO x_discovery_evidence(
                        source_account_id, target_account_id, relation_type, evidence,
                        evidence_url, weight, observed_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(source_account_id, target_account_id, relation_type, evidence)
                    DO NOTHING
                    """,
                    (
                        value.get("source_account_id"), value["target_account_id"],
                        value["relation_type"], value["evidence"],
                        value.get("evidence_url"), float(value.get("weight") or 0),
                        value.get("observed_at") or _now(),
                    ),
                )

    def relationship_evidence_count(self, account_id: str) -> int:
        """Return durable seed/active relationship evidence for one X account."""

        with self.database.transaction() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS count FROM x_discovery_evidence
                WHERE target_account_id=? AND relation_type='seed_relation'
                """,
                (account_id,),
            ).fetchone()
        return int(row["count"])

    def recent_relevant_content_count(self, account_id: str, *, days: int = 30) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max(1, days))).isoformat()
        with self.database.transaction() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS count FROM x_tweets
                WHERE author_id=? AND created_at>=? AND relevance_score>=0.5
                """,
                (account_id, cutoff),
            ).fetchone()
        return int(row["count"])

    def set_kol_status(
        self,
        account_id: str,
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
                UPDATE x_kols SET status=?, score=?, reasons_json=?,
                    last_qualified_at=CASE WHEN ? THEN ? ELSE last_qualified_at END,
                    updated_at=? WHERE account_id=?
                """,
                (
                    value,
                    score,
                    json.dumps(reasons),
                    refresh_qualified_at,
                    now,
                    now,
                    account_id,
                ),
            )
            if not cursor.rowcount:
                raise KeyError(account_id)

    def get_kol(self, account_id: str) -> dict[str, Any] | None:
        with self.database.transaction() as connection:
            row = connection.execute(
                """
                SELECT a.*, k.status, k.score, k.reasons_json, k.last_qualified_at
                FROM x_kols k JOIN x_accounts a ON a.id=k.account_id
                WHERE k.account_id=?
                """,
                (account_id,),
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
                SELECT a.*, k.status, k.score, k.reasons_json, k.last_qualified_at
                FROM x_kols k JOIN x_accounts a ON a.id=k.account_id
                {where} ORDER BY k.score DESC, a.followers_count DESC LIMIT ?
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
        clauses = [f"k.status IN ({','.join('?' for _ in values)})"]
        params: list[Any] = list(values)
        if after:
            clauses.append("a.id>?")
            params.append(after)
        params.append(batch_size + 1)
        with self.database.transaction() as connection:
            rows = connection.execute(
                f"""
                SELECT a.*, k.status, k.score, k.reasons_json, k.last_qualified_at
                FROM x_kols k JOIN x_accounts a ON a.id=k.account_id
                WHERE {' AND '.join(clauses)}
                ORDER BY a.id ASC LIMIT ?
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

    def list_recent_trends(self, *, limit: int = 30) -> list[dict[str, Any]]:
        """Return the latest snapshot of each native trend, newest capture first."""

        with self.database.transaction() as connection:
            rows = connection.execute(
                """
                WITH latest AS (
                    SELECT id, MAX(captured_at) AS captured_at
                    FROM x_trends GROUP BY id
                )
                SELECT t.id, t.name, t.rank, t.post_count, t.url, t.captured_at
                FROM x_trends t
                JOIN latest l ON l.id=t.id AND l.captured_at=t.captured_at
                ORDER BY t.captured_at DESC, t.rank ASC, t.name ASC
                LIMIT ?
                """,
                (max(1, min(limit, 100)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_recent_trend_tweets(
        self, *, limit: int = 30, offset: int = 0
    ) -> list[dict[str, Any]]:
        """Return concrete tweets discovered from recent native trends."""

        with self.database.transaction() as connection:
            rows = connection.execute(
                """
                SELECT t.id, t.author_id, t.author_handle, t.text, t.created_at,
                       t.captured_at, t.url, t.like_count, t.repost_count,
                       t.reply_count, t.quote_count, t.view_count,
                       a.display_name, l.trend_name, l.trend_rank, l.last_seen_at
                FROM x_trend_tweets l
                JOIN x_tweets t ON t.id=l.tweet_id
                LEFT JOIN x_accounts a ON a.id=t.author_id
                ORDER BY COALESCE(t.created_at, l.last_seen_at) DESC,
                         l.trend_rank ASC, t.id DESC
                LIMIT ? OFFSET ?
                """,
                (max(1, min(limit, 100)), max(0, offset)),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_managed_kol_tweets(
        self, *, limit: int = 30, offset: int = 0
    ) -> list[dict[str, Any]]:
        """Return newest tweets from KOLs still managed by the automation loop."""

        with self.database.transaction() as connection:
            rows = connection.execute(
                """
                SELECT t.id, t.author_id, t.author_handle, t.text, t.created_at,
                       t.captured_at, t.url, t.relevance_score, t.like_count,
                       t.repost_count, t.reply_count, t.quote_count, t.view_count,
                       a.display_name, k.status AS kol_status, k.score AS kol_score
                FROM x_tweets t
                JOIN x_kols k ON k.account_id=t.author_id
                JOIN x_accounts a ON a.id=t.author_id
                WHERE k.status IN ('seed', 'candidate', 'active', 'review')
                ORDER BY COALESCE(t.created_at, t.captured_at) DESC, t.id DESC
                LIMIT ? OFFSET ?
                """,
                (max(1, min(limit, 100)), max(0, offset)),
            ).fetchall()
        return [dict(row) for row in rows]

    def summary(self) -> dict[str, int]:
        with self.database.transaction() as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) AS count FROM x_kols GROUP BY status"
            ).fetchall()
            tweets = connection.execute(
                "SELECT COUNT(*) AS count FROM x_tweets"
            ).fetchone()["count"]
            trends = connection.execute(
                "SELECT COUNT(DISTINCT id) AS count FROM x_trends"
            ).fetchone()["count"]
        result = {str(row["status"]): int(row["count"]) for row in rows}
        result["content"] = int(tweets)
        result["trends"] = int(trends)
        return result

    def pause_inactive(self, *, days: int) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max(1, days))).isoformat()
        now = _now()
        reasons = json.dumps((f"no qualifying activity for at least {max(1, days)} days",))
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE x_kols SET status='paused', reasons_json=?, updated_at=?
                WHERE status='active' AND COALESCE(last_qualified_at, updated_at)<?
                """,
                (reasons, now, cutoff),
            )
        return int(cursor.rowcount)
