from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from threading import RLock
from typing import Any

from kol_search.database import DatabaseRuntime, DatabaseTarget


PROVIDER_STATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS discover_response_snapshots (
    cache_key TEXT PRIMARY KEY,
    platform TEXT NOT NULL,
    operation TEXT NOT NULL,
    provider TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_discover_snapshots_expiry
ON discover_response_snapshots(expires_at);

CREATE TABLE IF NOT EXISTS provider_usage_daily (
    provider TEXT NOT NULL,
    usage_date TEXT NOT NULL,
    request_count INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(provider, usage_date)
);

CREATE TABLE IF NOT EXISTS provider_account_status (
    provider TEXT PRIMARY KEY,
    credits_remaining REAL,
    credits_used REAL,
    upstream_total_requests INTEGER,
    observed_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS llm_usage_daily (
    provider TEXT NOT NULL,
    usage_date TEXT NOT NULL,
    request_count INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(provider, usage_date)
);

CREATE TABLE IF NOT EXISTS llm_reply_drafts (
    cache_key TEXT PRIMARY KEY,
    platform TEXT NOT NULL,
    content_id TEXT NOT NULL,
    model TEXT NOT NULL,
    draft TEXT NOT NULL,
    generated_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_llm_reply_drafts_expiry
ON llm_reply_drafts(expires_at);
"""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class ProviderStateStore:
    """Persistent response cache and provider-budget ledger shared by connectors."""

    def __init__(self, target: DatabaseTarget) -> None:
        self.database = DatabaseRuntime(target)
        self._usage_lock = RLock()
        # Local SQLite remains self-contained for development. Production
        # PostgreSQL tables are created only by the reviewed migration.
        if not self.database.is_postgres:
            self._install_schema()

    def _install_schema(self) -> None:
        with self.database.transaction() as connection:
            for statement in PROVIDER_STATE_SCHEMA.split(";"):
                sql = statement.strip()
                if sql:
                    connection.execute(sql)

    def close(self) -> None:
        self.database.close()

    def reserve_calls(
        self,
        provider: str,
        *,
        units: int = 1,
        daily_limit: int,
        usage_date: date | None = None,
    ) -> bool:
        """Atomically reserve billable calls before contacting an upstream."""

        units = max(1, int(units))
        daily_limit = max(0, int(daily_limit))
        day = (usage_date or _utc_now().date()).isoformat()
        now = _utc_now().isoformat()
        with self._usage_lock, self.database.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO provider_usage_daily(
                    provider, usage_date, request_count, updated_at
                ) VALUES(?, ?, 0, ?)
                ON CONFLICT(provider, usage_date) DO NOTHING
                """,
                (provider, day, now),
            )
            cursor = connection.execute(
                """
                UPDATE provider_usage_daily
                SET request_count=request_count+?, updated_at=?
                WHERE provider=? AND usage_date=?
                  AND request_count+?<=?
                """,
                (units, now, provider, day, units, daily_limit),
            )
            return bool(cursor.rowcount)

    def usage_today(self, provider: str) -> int:
        day = _utc_now().date().isoformat()
        with self.database.transaction() as connection:
            row = connection.execute(
                """
                SELECT request_count FROM provider_usage_daily
                WHERE provider=? AND usage_date=?
                """,
                (provider, day),
            ).fetchone()
        return int(row["request_count"]) if row is not None else 0

    def reserve_llm_request(
        self,
        provider: str,
        *,
        daily_limit: int,
        usage_date: date | None = None,
    ) -> bool:
        day = (usage_date or _utc_now().date()).isoformat()
        now = _utc_now().isoformat()
        with self._usage_lock, self.database.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO llm_usage_daily(provider, usage_date, request_count, updated_at)
                VALUES(?, ?, 0, ?)
                ON CONFLICT(provider, usage_date) DO NOTHING
                """,
                (provider, day, now),
            )
            cursor = connection.execute(
                """
                UPDATE llm_usage_daily
                SET request_count=request_count+1, updated_at=?
                WHERE provider=? AND usage_date=? AND request_count+1<=?
                """,
                (now, provider, day, max(0, daily_limit)),
            )
        return bool(cursor.rowcount)

    def get_reply_draft(self, cache_key: str) -> dict[str, Any] | None:
        with self.database.transaction() as connection:
            row = connection.execute(
                """
                SELECT platform, content_id, model, draft, generated_at, expires_at
                FROM llm_reply_drafts WHERE cache_key=?
                """,
                (cache_key,),
            ).fetchone()
        if row is None:
            return None
        output = dict(row)
        try:
            if _utc_now() > _parse_datetime(str(output["expires_at"])):
                return None
        except (TypeError, ValueError):
            return None
        return output

    def put_reply_draft(
        self,
        cache_key: str,
        *,
        platform: str,
        content_id: str,
        model: str,
        draft: str,
        ttl_seconds: int,
    ) -> dict[str, Any]:
        generated_at = _utc_now()
        expires_at = generated_at + timedelta(seconds=max(1, ttl_seconds))
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO llm_reply_drafts(
                    cache_key, platform, content_id, model, draft,
                    generated_at, expires_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    model=excluded.model,
                    draft=excluded.draft,
                    generated_at=excluded.generated_at,
                    expires_at=excluded.expires_at,
                    updated_at=excluded.updated_at
                """,
                (
                    cache_key,
                    platform,
                    content_id,
                    model,
                    draft,
                    generated_at.isoformat(),
                    expires_at.isoformat(),
                    generated_at.isoformat(),
                ),
            )
        return {
            "platform": platform,
            "content_id": content_id,
            "model": model,
            "draft": draft,
            "generated_at": generated_at.isoformat(),
            "expires_at": expires_at.isoformat(),
        }

    def debit_estimated_credits(self, provider: str, *, amount: float) -> None:
        """Conservatively project balance between upstream account checks."""

        amount = max(0.0, float(amount))
        if amount == 0:
            return
        now = _utc_now().isoformat()
        with self._usage_lock, self.database.transaction(immediate=True) as connection:
            connection.execute(
                """
                UPDATE provider_account_status
                SET credits_remaining=CASE
                        WHEN credits_remaining-? < 0 THEN 0
                        ELSE credits_remaining-?
                    END,
                    updated_at=?
                WHERE provider=? AND credits_remaining IS NOT NULL
                """,
                (amount, amount, now, provider),
            )

    def record_account_status(
        self,
        provider: str,
        *,
        credits_remaining: float | None,
        credits_used: float | None,
        upstream_total_requests: int | None,
        observed_at: datetime | None = None,
    ) -> None:
        captured = (observed_at or _utc_now()).isoformat()
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO provider_account_status(
                    provider, credits_remaining, credits_used,
                    upstream_total_requests, observed_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?)
                ON CONFLICT(provider) DO UPDATE SET
                    credits_remaining=excluded.credits_remaining,
                    credits_used=excluded.credits_used,
                    upstream_total_requests=excluded.upstream_total_requests,
                    observed_at=excluded.observed_at,
                    updated_at=excluded.updated_at
                """,
                (
                    provider,
                    credits_remaining,
                    credits_used,
                    upstream_total_requests,
                    captured,
                    captured,
                ),
            )

    def account_status(self, provider: str) -> dict[str, Any] | None:
        with self.database.transaction() as connection:
            row = connection.execute(
                """
                SELECT provider, credits_remaining, credits_used,
                       upstream_total_requests, observed_at
                FROM provider_account_status WHERE provider=?
                """,
                (provider,),
            ).fetchone()
        return dict(row) if row is not None else None

    def account_status_is_fresh(self, provider: str, *, max_age_seconds: int) -> bool:
        status = self.account_status(provider)
        if status is None:
            return False
        try:
            observed_at = _parse_datetime(str(status["observed_at"]))
        except (TypeError, ValueError):
            return False
        return _utc_now() - observed_at <= timedelta(seconds=max(0, max_age_seconds))

    def put_snapshot(
        self,
        cache_key: str,
        *,
        platform: str,
        operation: str,
        provider: str,
        payload: dict[str, Any],
        observed_at: datetime,
        ttl_seconds: int,
    ) -> None:
        now = _utc_now()
        expires_at = now + timedelta(seconds=max(1, ttl_seconds))
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO discover_response_snapshots(
                    cache_key, platform, operation, provider, payload_json,
                    observed_at, expires_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    provider=excluded.provider,
                    payload_json=excluded.payload_json,
                    observed_at=excluded.observed_at,
                    expires_at=excluded.expires_at,
                    updated_at=excluded.updated_at
                """,
                (
                    cache_key,
                    platform,
                    operation,
                    provider,
                    json.dumps(payload, ensure_ascii=False),
                    observed_at.isoformat(),
                    expires_at.isoformat(),
                    now.isoformat(),
                ),
            )

    def get_snapshot(
        self,
        cache_key: str,
        *,
        stale_seconds: int = 0,
    ) -> tuple[dict[str, Any], bool] | None:
        with self.database.transaction() as connection:
            row = connection.execute(
                """
                SELECT payload_json, expires_at
                FROM discover_response_snapshots WHERE cache_key=?
                """,
                (cache_key,),
            ).fetchone()
        if row is None:
            return None
        try:
            expires_at = _parse_datetime(str(row["expires_at"]))
            payload = json.loads(str(row["payload_json"]))
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        now = _utc_now()
        fresh = now <= expires_at
        if not fresh and now > expires_at + timedelta(seconds=max(0, stale_seconds)):
            return None
        return payload, fresh


__all__ = ["PROVIDER_STATE_SCHEMA", "ProviderStateStore"]
