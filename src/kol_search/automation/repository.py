from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .database import AutomationDatabase
from .models import (
    ACTION_TRANSITIONS,
    OPPORTUNITY_TRANSITIONS,
    ActionKind,
    ActionStatus,
    ChannelControlScope,
    ChannelState,
    CoreJobStatus,
    CoreJobType,
    OpportunityStatus,
    PlatformConnectionStatus,
    PolicyOutcome,
    enum_value,
)


class StateTransitionError(RuntimeError):
    """Raised when a workflow record cannot enter the requested state."""


class RecordNotFoundError(KeyError):
    """Raised when a requested automation record does not exist."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))


def _decode_json(value: str | None, fallback: Any) -> Any:
    try:
        return json.loads(value or "")
    except (json.JSONDecodeError, TypeError):
        return fallback


def _record(row: sqlite3.Row | None, json_fields: Mapping[str, tuple[str, Any]]) -> dict[str, Any] | None:
    if row is None:
        return None
    output = dict(row)
    for stored_name, (public_name, fallback) in json_fields.items():
        output[public_name] = _decode_json(output.pop(stored_name, None), fallback)
    return output


CONNECTION_JSON = {
    "capabilities_json": ("capabilities", []),
    "metadata_json": ("metadata", {}),
}
JOB_JSON = {"payload_json": ("payload", {}), "result_json": ("result", {})}
OPPORTUNITY_JSON = {
    "evidence_json": ("evidence", []),
    "payload_json": ("payload", {}),
}
ACTION_JSON = {
    "receipt_json": ("receipt", {}),
    "payload_json": ("payload", {}),
}
POLICY_JSON = {"rules_json": ("rules", [])}
AUDIT_JSON = {"payload_json": ("payload", {})}
BRAND_JSON = {
    "platform_handles_json": ("platform_handles", {}),
    "allowed_claims_json": ("allowed_claims", []),
    "forbidden_terms_json": ("forbidden_terms", []),
    "approved_domains_json": ("approved_domains", []),
}
CONVERSATION_JSON = {"evidence_json": ("evidence", {})}


class AutomationStore:
    """Transaction-safe repository for the platform-independent automation core."""

    def __init__(self, database: AutomationDatabase | str | Path) -> None:
        self.database = (
            database if isinstance(database, AutomationDatabase) else AutomationDatabase(database)
        )

    # Shared brand and deterministic account limits

    def save_brand_config(
        self,
        *,
        brand_name: str = "",
        description: str = "",
        audience: str = "",
        tone: str = "professional, concise, conversational",
        product_url: str = "",
        platform_handles: Mapping[str, str] | None = None,
        allowed_claims: Sequence[str] = (),
        forbidden_terms: Sequence[str] = (),
        approved_domains: Sequence[str] = (),
        now: str | None = None,
    ) -> None:
        timestamp = now or utc_now()
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO automation_brand_config(
                    id, brand_name, description, audience, tone, product_url,
                    platform_handles_json, allowed_claims_json, forbidden_terms_json,
                    approved_domains_json, updated_at
                ) VALUES(1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    brand_name=excluded.brand_name, description=excluded.description,
                    audience=excluded.audience, tone=excluded.tone,
                    product_url=excluded.product_url,
                    platform_handles_json=excluded.platform_handles_json,
                    allowed_claims_json=excluded.allowed_claims_json,
                    forbidden_terms_json=excluded.forbidden_terms_json,
                    approved_domains_json=excluded.approved_domains_json,
                    updated_at=excluded.updated_at
                """,
                (
                    brand_name.strip(),
                    description.strip(),
                    audience.strip(),
                    tone.strip(),
                    product_url.strip(),
                    _json(dict(platform_handles or {})),
                    _json([str(value).strip() for value in allowed_claims if str(value).strip()]),
                    _json([str(value).strip() for value in forbidden_terms if str(value).strip()]),
                    _json(sorted({str(value).strip().lower() for value in approved_domains if str(value).strip()})),
                    timestamp,
                ),
            )

    def get_brand_config(self) -> dict[str, Any]:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM automation_brand_config WHERE id=1"
            ).fetchone()
        value = _record(row, BRAND_JSON)
        return value or {
            "id": 1,
            "brand_name": "",
            "description": "",
            "audience": "",
            "tone": "professional, concise, conversational",
            "product_url": "",
            "platform_handles": {},
            "allowed_claims": [],
            "forbidden_terms": [],
            "approved_domains": [],
            "updated_at": None,
        }

    # Human- or platform-verified DM conversation state

    def set_conversation_state(
        self,
        platform_id: str,
        native_conversation_id: str,
        native_account_id: str,
        *,
        valid: bool,
        evidence: Mapping[str, Any],
        verified_by: str,
        now: str | None = None,
    ) -> None:
        platform_id = _required(platform_id, "platform_id")
        conversation_id = _required(
            native_conversation_id, "native_conversation_id"
        )
        account_id = _required(native_account_id, "native_account_id")
        actor = _required(verified_by, "verified_by")
        if valid and not evidence:
            raise ValueError("A valid conversation requires verification evidence")
        state = "valid" if valid else "revoked"
        timestamp = now or utc_now()
        with self.database.transaction(immediate=True) as connection:
            existing = connection.execute(
                """
                SELECT native_account_id FROM automation_conversations
                WHERE platform_id=? AND native_conversation_id=?
                """,
                (platform_id, conversation_id),
            ).fetchone()
            if existing is not None and existing["native_account_id"] != account_id:
                raise ValueError("Conversation is already bound to another native account")
            connection.execute(
                """
                INSERT INTO automation_conversations(
                    platform_id, native_conversation_id, native_account_id, state,
                    evidence_json, verified_by, verified_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(platform_id, native_conversation_id) DO UPDATE SET
                    native_account_id=excluded.native_account_id,
                    state=excluded.state,
                    evidence_json=excluded.evidence_json,
                    verified_by=excluded.verified_by,
                    verified_at=excluded.verified_at,
                    updated_at=excluded.updated_at
                """,
                (
                    platform_id,
                    conversation_id,
                    account_id,
                    state,
                    _json(dict(evidence)),
                    actor,
                    timestamp,
                    timestamp,
                ),
            )
            self._append_audit(
                connection,
                actor=actor,
                event_type=f"conversation.{state}",
                object_type="conversation",
                object_id=conversation_id,
                platform_id=platform_id,
                payload={"native_account_id": account_id, "evidence": dict(evidence)},
                created_at=timestamp,
            )

    def get_conversation_state(
        self, platform_id: str, native_conversation_id: str
    ) -> dict[str, Any] | None:
        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM automation_conversations
                WHERE platform_id=? AND native_conversation_id=?
                """,
                (platform_id, native_conversation_id),
            ).fetchone()
        return _record(row, CONVERSATION_JSON)

    def is_valid_conversation(
        self,
        platform_id: str,
        native_account_id: str,
        native_conversation_id: str,
    ) -> bool:
        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM automation_conversations
                WHERE platform_id=? AND native_account_id=?
                    AND native_conversation_id=? AND state='valid'
                """,
                (platform_id, native_account_id, native_conversation_id),
            ).fetchone()
        return row is not None

    def upsert_account_quota(
        self,
        connection_id: int,
        action_type: ActionKind | str,
        *,
        hourly_limit: int,
        daily_limit: int,
        cooldown_seconds: int = 0,
        now: str | None = None,
    ) -> None:
        action_value = enum_value(action_type, ActionKind)
        hourly = _bounded_int(hourly_limit, 0, 1_000_000, "hourly_limit")
        daily = _bounded_int(daily_limit, 0, 1_000_000, "daily_limit")
        cooldown = _bounded_int(cooldown_seconds, 0, 10 * 365 * 86400, "cooldown_seconds")
        timestamp = now or utc_now()
        with self.database.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT 1 FROM automation_platform_connections WHERE id=?",
                (connection_id,),
            ).fetchone()
            if row is None:
                raise RecordNotFoundError(f"Platform connection {connection_id} does not exist")
            connection.execute(
                """
                INSERT INTO automation_account_quotas(
                    connection_id, action_type, hourly_limit, daily_limit,
                    cooldown_seconds, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?)
                ON CONFLICT(connection_id, action_type) DO UPDATE SET
                    hourly_limit=excluded.hourly_limit,
                    daily_limit=excluded.daily_limit,
                    cooldown_seconds=excluded.cooldown_seconds,
                    updated_at=excluded.updated_at
                """,
                (connection_id, action_value, hourly, daily, cooldown, timestamp),
            )

    def get_account_quota(
        self, connection_id: int, action_type: ActionKind | str
    ) -> dict[str, Any] | None:
        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM automation_account_quotas
                WHERE connection_id=? AND action_type=?
                """,
                (connection_id, enum_value(action_type, ActionKind)),
            ).fetchone()
        return dict(row) if row is not None else None

    @property
    def path(self) -> Path:
        return self.database.path

    # Platform connections

    def upsert_platform_connection(
        self,
        platform_id: str,
        *,
        connection_key: str = "default",
        display_name: str = "",
        status: PlatformConnectionStatus | str = PlatformConnectionStatus.DISCONNECTED,
        enabled: bool = True,
        capabilities: Iterable[str] = (),
        metadata: Mapping[str, Any] | None = None,
        now: str | None = None,
    ) -> int:
        platform_id = _required(platform_id, "platform_id")
        connection_key = _required(connection_key, "connection_key")
        status_value = enum_value(status, PlatformConnectionStatus)
        timestamp = now or utc_now()
        capability_values = sorted({_required(str(item), "capability") for item in capabilities})
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO automation_platform_connections(
                    platform_id, connection_key, display_name, status, enabled,
                    capabilities_json, metadata_json, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(platform_id, connection_key) DO UPDATE SET
                    display_name=excluded.display_name,
                    status=excluded.status,
                    enabled=excluded.enabled,
                    capabilities_json=excluded.capabilities_json,
                    metadata_json=excluded.metadata_json,
                    updated_at=excluded.updated_at
                """,
                (
                    platform_id,
                    connection_key,
                    display_name,
                    status_value,
                    int(enabled),
                    _json(capability_values),
                    _json(dict(metadata or {})),
                    timestamp,
                    timestamp,
                ),
            )
            row = connection.execute(
                """
                SELECT id FROM automation_platform_connections
                WHERE platform_id=? AND connection_key=?
                """,
                (platform_id, connection_key),
            ).fetchone()
            assert row is not None
            connection_id = int(row["id"])
        return connection_id

    def get_platform_connection(self, connection_id: int) -> dict[str, Any] | None:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM automation_platform_connections WHERE id=?", (connection_id,)
            ).fetchone()
        return _record(row, CONNECTION_JSON)

    def list_platform_connections(
        self, *, platform_id: str | None = None, enabled_only: bool = False
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        if platform_id is not None:
            clauses.append("platform_id=?")
            values.append(platform_id)
        if enabled_only:
            clauses.append("enabled=1")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.database.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM automation_platform_connections {where}
                ORDER BY platform_id, connection_key
                """,
                values,
            ).fetchall()
        return [_record(row, CONNECTION_JSON) for row in rows]  # type: ignore[misc]

    def record_connection_health(
        self,
        connection_id: int,
        status: PlatformConnectionStatus | str,
        *,
        error: str | None = None,
        checked_at: str | None = None,
    ) -> None:
        status_value = enum_value(status, PlatformConnectionStatus)
        timestamp = checked_at or utc_now()
        with self.database.transaction(immediate=True) as connection:
            cursor = connection.execute(
                """
                UPDATE automation_platform_connections SET
                    status=?,
                    consecutive_failures=CASE WHEN ?='connected' THEN 0
                        WHEN ? IN ('degraded', 'disconnected') THEN consecutive_failures + 1
                        ELSE consecutive_failures END,
                    last_health_at=?, last_health_error=?, updated_at=?
                WHERE id=?
                """,
                (
                    status_value,
                    status_value,
                    status_value,
                    timestamp,
                    error,
                    timestamp,
                    connection_id,
                ),
            )
            if cursor.rowcount != 1:
                raise RecordNotFoundError(f"Platform connection {connection_id} does not exist")

    # Core jobs

    def create_job(
        self,
        job_type: CoreJobType | str,
        platform_id: str,
        *,
        connection_id: int | None = None,
        priority: int = 50,
        payload: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
        available_at: str | None = None,
        now: str | None = None,
    ) -> int:
        job_type_value = enum_value(job_type, CoreJobType)
        platform_id = _required(platform_id, "platform_id")
        priority = _bounded_int(priority, 0, 100, "priority")
        timestamp = now or utc_now()
        available = available_at or timestamp
        with self.database.transaction(immediate=True) as connection:
            self._validate_connection(connection, connection_id, platform_id)
            if idempotency_key:
                existing = connection.execute(
                    "SELECT id, job_type, platform_id FROM automation_jobs WHERE idempotency_key=?",
                    (idempotency_key,),
                ).fetchone()
                if existing is not None:
                    if existing["job_type"] != job_type_value or existing["platform_id"] != platform_id:
                        raise ValueError("Job idempotency key is already used by a different job")
                    return int(existing["id"])
            cursor = connection.execute(
                """
                INSERT INTO automation_jobs(
                    job_type, platform_id, connection_id, priority, payload_json,
                    idempotency_key, available_at, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_type_value,
                    platform_id,
                    connection_id,
                    priority,
                    _json(dict(payload or {})),
                    idempotency_key,
                    available,
                    timestamp,
                    timestamp,
                ),
            )
            return int(cursor.lastrowid)

    def claim_next_job(
        self,
        worker_id: str,
        *,
        platform_id: str | None = None,
        job_types: Iterable[CoreJobType | str] | None = None,
        now: str | None = None,
    ) -> dict[str, Any] | None:
        worker_id = _required(worker_id, "worker_id")
        timestamp = now or utc_now()
        clauses = ["status='queued'", "available_at<=?"]
        values: list[Any] = [timestamp]
        if platform_id is not None:
            clauses.append("platform_id=?")
            values.append(platform_id)
        if job_types is not None:
            normalized = [enum_value(item, CoreJobType) for item in job_types]
            if not normalized:
                return None
            clauses.append(f"job_type IN ({','.join('?' for _ in normalized)})")
            values.extend(normalized)
        with self.database.transaction(immediate=True) as connection:
            row = connection.execute(
                f"""
                SELECT id FROM automation_jobs
                WHERE {' AND '.join(clauses)}
                ORDER BY priority DESC, available_at, id
                LIMIT 1
                """,
                values,
            ).fetchone()
            if row is None:
                return None
            job_id = int(row["id"])
            cursor = connection.execute(
                """
                UPDATE automation_jobs SET status='running', progress=1, phase='starting',
                    attempts=attempts+1, locked_by=?, locked_at=?,
                    started_at=COALESCE(started_at, ?), error=NULL, updated_at=?
                WHERE id=? AND status='queued'
                """,
                (worker_id, timestamp, timestamp, timestamp, job_id),
            )
            if cursor.rowcount != 1:
                raise StateTransitionError(f"Job {job_id} was claimed concurrently")
            claimed = connection.execute(
                "SELECT * FROM automation_jobs WHERE id=?", (job_id,)
            ).fetchone()
            return _record(claimed, JOB_JSON)

    def recover_stale_jobs(
        self,
        *,
        before: str,
        now: str | None = None,
    ) -> int:
        """Fail abandoned jobs on restart without retrying their side effects."""

        timestamp = now or utc_now()
        with self.database.transaction(immediate=True) as connection:
            cursor = connection.execute(
                """
                UPDATE automation_jobs
                SET status='failed', phase='failed', progress=100,
                    error='worker interrupted; inspect platform state before retrying',
                    finished_at=?, locked_by=NULL, locked_at=NULL, updated_at=?
                WHERE status='running' AND locked_at IS NOT NULL AND locked_at<?
                """,
                (timestamp, timestamp, before),
            )
        return int(cursor.rowcount)

    def update_job_progress(
        self,
        job_id: int,
        *,
        progress: int,
        phase: str,
        now: str | None = None,
    ) -> None:
        progress = _bounded_int(progress, 0, 99, "progress")
        phase = _required(phase, "phase")
        timestamp = now or utc_now()
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE automation_jobs SET progress=?, phase=?, updated_at=?
                WHERE id=? AND status='running'
                """,
                (progress, phase, timestamp, job_id),
            )
            if cursor.rowcount != 1:
                self._raise_job_transition(connection, job_id, "running progress")

    def complete_job(
        self,
        job_id: int,
        *,
        result: Mapping[str, Any] | None = None,
        now: str | None = None,
    ) -> None:
        timestamp = now or utc_now()
        with self.database.transaction(immediate=True) as connection:
            cursor = connection.execute(
                """
                UPDATE automation_jobs SET status='succeeded', progress=100, phase='done',
                    result_json=?, finished_at=?, locked_by=NULL, locked_at=NULL, updated_at=?
                WHERE id=? AND status='running'
                """,
                (_json(dict(result or {})), timestamp, timestamp, job_id),
            )
            if cursor.rowcount != 1:
                self._raise_job_transition(connection, job_id, CoreJobStatus.SUCCEEDED.value)

    def fail_job(self, job_id: int, error: str, *, now: str | None = None) -> None:
        timestamp = now or utc_now()
        with self.database.transaction(immediate=True) as connection:
            cursor = connection.execute(
                """
                UPDATE automation_jobs SET status='failed', phase='failed', error=?,
                    finished_at=?, locked_by=NULL, locked_at=NULL, updated_at=?
                WHERE id=? AND status='running'
                """,
                (_required(error, "error"), timestamp, timestamp, job_id),
            )
            if cursor.rowcount != 1:
                self._raise_job_transition(connection, job_id, CoreJobStatus.FAILED.value)

    def cancel_job(self, job_id: int, *, reason: str = "", now: str | None = None) -> None:
        timestamp = now or utc_now()
        with self.database.transaction(immediate=True) as connection:
            cursor = connection.execute(
                """
                UPDATE automation_jobs SET status='cancelled', phase='cancelled', error=?,
                    finished_at=?, locked_by=NULL, locked_at=NULL, updated_at=?
                WHERE id=? AND status IN ('queued', 'running')
                """,
                (reason or None, timestamp, timestamp, job_id),
            )
            if cursor.rowcount != 1:
                self._raise_job_transition(connection, job_id, CoreJobStatus.CANCELLED.value)

    def get_job(self, job_id: int) -> dict[str, Any] | None:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM automation_jobs WHERE id=?", (job_id,)
            ).fetchone()
        return _record(row, JOB_JSON)

    def list_jobs(
        self,
        *,
        platform_id: str | None = None,
        status: CoreJobStatus | str | None = None,
        job_types: Iterable[CoreJobType | str] | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        if platform_id is not None:
            clauses.append("platform_id=?")
            values.append(platform_id)
        if status is not None:
            clauses.append("status=?")
            values.append(enum_value(status, CoreJobStatus))
        if job_types is not None:
            normalized = [enum_value(item, CoreJobType) for item in job_types]
            if not normalized:
                return []
            clauses.append(f"job_type IN ({','.join('?' for _ in normalized)})")
            values.extend(normalized)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        values.append(_bounded_int(limit, 1, 1000, "limit"))
        with self.database.connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM automation_jobs {where} ORDER BY created_at DESC, id DESC LIMIT ?",
                values,
            ).fetchall()
        return [_record(row, JOB_JSON) for row in rows]  # type: ignore[misc]

    # Opportunities

    def upsert_opportunity(
        self,
        platform_id: str,
        opportunity_type: str,
        native_object_type: str,
        native_object_id: str,
        *,
        priority: int = 50,
        score: float = 0,
        title: str = "",
        evidence: Sequence[Mapping[str, Any] | str] = (),
        payload: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
        expires_at: str | None = None,
        now: str | None = None,
    ) -> int:
        platform_id = _required(platform_id, "platform_id")
        opportunity_type = _required(opportunity_type, "opportunity_type")
        native_object_type = _required(native_object_type, "native_object_type")
        native_object_id = _required(native_object_id, "native_object_id")
        priority = _bounded_int(priority, 0, 100, "priority")
        score = _bounded_float(score, 0, 100, "score")
        key = idempotency_key or ":".join(
            (platform_id, opportunity_type, native_object_type, native_object_id)
        )
        timestamp = now or utc_now()
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO automation_opportunities(
                    platform_id, opportunity_type, native_object_type, native_object_id,
                    priority, score, title, evidence_json, payload_json, idempotency_key,
                    expires_at, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(idempotency_key) DO UPDATE SET
                    priority=excluded.priority, score=excluded.score, title=excluded.title,
                    evidence_json=excluded.evidence_json, payload_json=excluded.payload_json,
                    expires_at=excluded.expires_at, updated_at=excluded.updated_at,
                    status=CASE
                        WHEN automation_opportunities.status='expired'
                            AND excluded.expires_at IS NOT NULL
                            AND excluded.expires_at>excluded.updated_at
                        THEN 'open'
                        ELSE automation_opportunities.status
                    END
                """,
                (
                    platform_id,
                    opportunity_type,
                    native_object_type,
                    native_object_id,
                    priority,
                    score,
                    title,
                    _json(list(evidence)),
                    _json(dict(payload or {})),
                    key,
                    expires_at,
                    timestamp,
                    timestamp,
                ),
            )
            row = connection.execute(
                """
                SELECT id, platform_id, opportunity_type, native_object_type, native_object_id
                FROM automation_opportunities WHERE idempotency_key=?
                """,
                (key,),
            ).fetchone()
            assert row is not None
            identity = (
                row["platform_id"],
                row["opportunity_type"],
                row["native_object_type"],
                row["native_object_id"],
            )
            if identity != (
                platform_id,
                opportunity_type,
                native_object_type,
                native_object_id,
            ):
                raise ValueError("Opportunity idempotency key is already used by a different object")
            return int(row["id"])

    def get_opportunity(self, opportunity_id: int) -> dict[str, Any] | None:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM automation_opportunities WHERE id=?", (opportunity_id,)
            ).fetchone()
        return _record(row, OPPORTUNITY_JSON)

    def list_opportunities(
        self,
        *,
        platform_id: str | None = None,
        status: OpportunityStatus | str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        if platform_id is not None:
            clauses.append("platform_id=?")
            values.append(platform_id)
        if status is not None:
            clauses.append("status=?")
            values.append(enum_value(status, OpportunityStatus))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        values.append(_bounded_int(limit, 1, 1000, "limit"))
        with self.database.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM automation_opportunities {where}
                ORDER BY priority DESC, score DESC, id LIMIT ?
                """,
                values,
            ).fetchall()
        return [_record(row, OPPORTUNITY_JSON) for row in rows]  # type: ignore[misc]

    def transition_opportunity(
        self,
        opportunity_id: int,
        status: OpportunityStatus | str,
        *,
        expected_status: OpportunityStatus | str | None = None,
        actor: str = "system",
        now: str | None = None,
    ) -> None:
        target = OpportunityStatus(enum_value(status, OpportunityStatus))
        timestamp = now or utc_now()
        with self.database.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT status, platform_id FROM automation_opportunities WHERE id=?",
                (opportunity_id,),
            ).fetchone()
            if row is None:
                raise RecordNotFoundError(f"Opportunity {opportunity_id} does not exist")
            current = OpportunityStatus(row["status"])
            if expected_status is not None and current.value != enum_value(
                expected_status, OpportunityStatus
            ):
                raise StateTransitionError(
                    f"Opportunity {opportunity_id} is {current.value}, not {expected_status}"
                )
            if target == current:
                return
            if target not in OPPORTUNITY_TRANSITIONS[current]:
                raise StateTransitionError(
                    f"Opportunity cannot transition from {current.value} to {target.value}"
                )
            connection.execute(
                "UPDATE automation_opportunities SET status=?, updated_at=? WHERE id=?",
                (target.value, timestamp, opportunity_id),
            )
            if target in {OpportunityStatus.DISMISSED, OpportunityStatus.EXPIRED}:
                self._cancel_pending_opportunity_actions(
                    connection,
                    opportunity_id,
                    reason=f"opportunity {target.value}",
                    timestamp=timestamp,
                )
            self._append_audit(
                connection,
                actor=actor,
                event_type=f"opportunity.{target.value}",
                object_type="opportunity",
                object_id=str(opportunity_id),
                platform_id=row["platform_id"],
                payload={"from": current.value, "to": target.value},
                created_at=timestamp,
            )

    def expire_opportunities(self, *, now: str | None = None, platform_id: str | None = None) -> int:
        timestamp = now or utc_now()
        platform_clause = " AND platform_id=?" if platform_id is not None else ""
        values: list[Any] = [timestamp, timestamp]
        if platform_id is not None:
            values.append(platform_id)
        with self.database.transaction(immediate=True) as connection:
            expiring = connection.execute(
                f"""
                SELECT id, platform_id FROM automation_opportunities
                WHERE status IN ('open', 'planned')
                    AND expires_at IS NOT NULL AND expires_at<=?{platform_clause}
                """,
                values[1:],
            ).fetchall()
            cursor = connection.execute(
                f"""
                UPDATE automation_opportunities SET status='expired', updated_at=?
                WHERE status IN ('open', 'planned')
                    AND expires_at IS NOT NULL AND expires_at<=?{platform_clause}
                """,
                values,
            )
            for row in expiring:
                opportunity_id = int(row["id"])
                self._cancel_pending_opportunity_actions(
                    connection,
                    opportunity_id,
                    reason="opportunity expired",
                    timestamp=timestamp,
                )
                self._append_audit(
                    connection,
                    actor="system",
                    event_type="opportunity.expired",
                    object_type="opportunity",
                    object_id=str(opportunity_id),
                    platform_id=row["platform_id"],
                    payload={"reason": "expires_at reached"},
                    created_at=timestamp,
                )
            return int(cursor.rowcount)

    def _cancel_pending_opportunity_actions(
        self,
        connection: sqlite3.Connection,
        opportunity_id: int,
        *,
        reason: str,
        timestamp: str,
    ) -> None:
        rows = connection.execute(
            """
            SELECT id, platform_id, status FROM automation_actions
            WHERE opportunity_id=? AND status IN ('proposed', 'needs_review', 'scheduled')
            """,
            (opportunity_id,),
        ).fetchall()
        if not rows:
            return
        connection.execute(
            """
            UPDATE automation_actions SET status='cancelled', error=?, finished_at=?,
                locked_by=NULL, locked_at=NULL, updated_at=?
            WHERE opportunity_id=? AND status IN ('proposed', 'needs_review', 'scheduled')
            """,
            (reason, timestamp, timestamp, opportunity_id),
        )
        for row in rows:
            self._append_audit(
                connection,
                actor="system",
                event_type="action.cancelled",
                object_type="action",
                object_id=str(row["id"]),
                platform_id=row["platform_id"],
                payload={"from": row["status"], "to": "cancelled", "reason": reason},
                created_at=timestamp,
            )

    # Actions and policy decisions

    def create_action(
        self,
        platform_id: str,
        action_type: ActionKind | str,
        native_object_type: str,
        native_object_id: str,
        *,
        opportunity_id: int | None = None,
        connection_id: int | None = None,
        draft: str = "",
        payload: Mapping[str, Any] | None = None,
        initial_status: ActionStatus | str = ActionStatus.PROPOSED,
        idempotency_key: str,
        scheduled_at: str | None = None,
        now: str | None = None,
    ) -> int:
        platform_id = _required(platform_id, "platform_id")
        action_type_value = enum_value(action_type, ActionKind)
        native_object_type = _required(native_object_type, "native_object_type")
        native_object_id = _required(native_object_id, "native_object_id")
        key = _required(idempotency_key, "idempotency_key")
        status_value = enum_value(initial_status, ActionStatus)
        if status_value not in {
            ActionStatus.PROPOSED.value,
            ActionStatus.NEEDS_REVIEW.value,
            ActionStatus.SCHEDULED.value,
        }:
            raise ValueError("New actions must be proposed, needs_review, or scheduled")
        timestamp = now or utc_now()
        with self.database.transaction(immediate=True) as connection:
            self._validate_connection(connection, connection_id, platform_id)
            existing = connection.execute(
                """
                SELECT id, platform_id, action_type, native_object_type, native_object_id
                FROM automation_actions WHERE idempotency_key=?
                """,
                (key,),
            ).fetchone()
            if existing is not None:
                identity = (
                    existing["platform_id"],
                    existing["action_type"],
                    existing["native_object_type"],
                    existing["native_object_id"],
                )
                if identity != (
                    platform_id,
                    action_type_value,
                    native_object_type,
                    native_object_id,
                ):
                    raise ValueError("Action idempotency key is already used by a different action")
                return int(existing["id"])
            if opportunity_id is not None:
                opportunity = connection.execute(
                    "SELECT platform_id, status FROM automation_opportunities WHERE id=?",
                    (opportunity_id,),
                ).fetchone()
                if opportunity is None:
                    raise RecordNotFoundError(f"Opportunity {opportunity_id} does not exist")
                if opportunity["platform_id"] != platform_id:
                    raise ValueError("Action and opportunity must belong to the same platform")
                if opportunity["status"] not in (
                    OpportunityStatus.OPEN.value,
                    OpportunityStatus.PLANNED.value,
                ):
                    raise StateTransitionError(
                        f"Cannot create an action for {opportunity['status']} opportunity"
                    )
            cursor = connection.execute(
                """
                INSERT INTO automation_actions(
                    opportunity_id, platform_id, connection_id, action_type,
                    native_object_type, native_object_id, status, draft, payload_json,
                    idempotency_key, scheduled_at, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    opportunity_id,
                    platform_id,
                    connection_id,
                    action_type_value,
                    native_object_type,
                    native_object_id,
                    status_value,
                    draft,
                    _json(dict(payload or {})),
                    key,
                    scheduled_at,
                    timestamp,
                    timestamp,
                ),
            )
            action_id = int(cursor.lastrowid)
            if opportunity_id is not None:
                connection.execute(
                    """
                    UPDATE automation_opportunities SET status='planned', updated_at=?
                    WHERE id=? AND status='open'
                    """,
                    (timestamp, opportunity_id),
                )
            self._append_audit(
                connection,
                actor="system",
                event_type="action.created",
                object_type="action",
                object_id=str(action_id),
                platform_id=platform_id,
                payload={"status": status_value, "action_type": action_type_value},
                created_at=timestamp,
            )
            return action_id

    def get_action(self, action_id: int) -> dict[str, Any] | None:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM automation_actions WHERE id=?", (action_id,)
            ).fetchone()
        return _record(row, ACTION_JSON)

    def list_actions(
        self,
        *,
        platform_id: str | None = None,
        status: ActionStatus | str | None = None,
        connection_id: int | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        if platform_id is not None:
            clauses.append("platform_id=?")
            values.append(platform_id)
        if status is not None:
            clauses.append("status=?")
            values.append(enum_value(status, ActionStatus))
        if connection_id is not None:
            clauses.append("connection_id=?")
            values.append(connection_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        values.append(_bounded_int(limit, 1, 1000, "limit"))
        with self.database.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM automation_actions {where}
                ORDER BY updated_at DESC, id DESC LIMIT ?
                """,
                values,
            ).fetchall()
        return [_record(row, ACTION_JSON) for row in rows]  # type: ignore[misc]

    def claim_next_action(
        self,
        worker_id: str,
        *,
        platform_id: str | None = None,
        now: str | None = None,
    ) -> dict[str, Any] | None:
        worker_id = _required(worker_id, "worker_id")
        timestamp = now or utc_now()
        platform_clause = " AND platform_id=?" if platform_id is not None else ""
        values: list[Any] = [timestamp]
        if platform_id is not None:
            values.append(platform_id)
        with self.database.transaction(immediate=True) as connection:
            candidates = connection.execute(
                f"""
                SELECT * FROM automation_actions
                WHERE status='scheduled' AND (scheduled_at IS NULL OR scheduled_at<=?)
                    {platform_clause}
                ORDER BY COALESCE(scheduled_at, created_at), id
                LIMIT 100
                """,
                values,
            ).fetchall()
            for candidate in candidates:
                effective = self._effective_channel_state(
                    connection,
                    platform_id=candidate["platform_id"],
                    connection_id=candidate["connection_id"],
                    action_type=candidate["action_type"],
                )
                if not effective["allowed"]:
                    continue
                action_id = int(candidate["id"])
                cursor = connection.execute(
                    """
                    UPDATE automation_actions SET status='executing', locked_by=?, locked_at=?,
                        started_at=COALESCE(started_at, ?), error=NULL, updated_at=?
                    WHERE id=? AND status='scheduled'
                    """,
                    (worker_id, timestamp, timestamp, timestamp, action_id),
                )
                if cursor.rowcount != 1:
                    continue
                claimed = connection.execute(
                    "SELECT * FROM automation_actions WHERE id=?", (action_id,)
                ).fetchone()
                self._append_audit(
                    connection,
                    actor=worker_id,
                    event_type="action.executing",
                    object_type="action",
                    object_id=str(action_id),
                    platform_id=candidate["platform_id"],
                    payload={},
                    created_at=timestamp,
                )
                return _record(claimed, ACTION_JSON)
            return None

    def transition_action(
        self,
        action_id: int,
        status: ActionStatus | str,
        *,
        expected_status: ActionStatus | str | None = None,
        actor: str = "system",
        scheduled_at: str | None = None,
        final_text: str | None = None,
        approved_by: str | None = None,
        error: str | None = None,
        now: str | None = None,
    ) -> None:
        target = ActionStatus(enum_value(status, ActionStatus))
        timestamp = now or utc_now()
        with self.database.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT status, platform_id FROM automation_actions WHERE id=?", (action_id,)
            ).fetchone()
            if row is None:
                raise RecordNotFoundError(f"Action {action_id} does not exist")
            current = ActionStatus(row["status"])
            if expected_status is not None and current.value != enum_value(
                expected_status, ActionStatus
            ):
                raise StateTransitionError(
                    f"Action {action_id} is {current.value}, not {expected_status}"
                )
            if target == current:
                return
            if target not in ACTION_TRANSITIONS[current]:
                raise StateTransitionError(
                    f"Action cannot transition from {current.value} to {target.value}"
                )
            fields = ["status=?", "updated_at=?"]
            values: list[Any] = [target.value, timestamp]
            if scheduled_at is not None:
                fields.append("scheduled_at=?")
                values.append(scheduled_at)
            if final_text is not None:
                fields.append("final_text=?")
                values.append(final_text)
            if approved_by is not None:
                fields.extend(("approved_by=?", "approved_at=?"))
                values.extend((approved_by, timestamp))
            if error is not None:
                fields.append("error=?")
                values.append(error)
            if target == ActionStatus.EXECUTING:
                fields.append("started_at=COALESCE(started_at, ?)")
                values.append(timestamp)
            if target in {
                ActionStatus.SUCCEEDED,
                ActionStatus.FAILED,
                ActionStatus.CANCELLED,
            }:
                fields.extend(("finished_at=?", "locked_by=NULL", "locked_at=NULL"))
                values.append(timestamp)
            values.append(action_id)
            connection.execute(
                f"UPDATE automation_actions SET {', '.join(fields)} WHERE id=?", values
            )
            self._append_audit(
                connection,
                actor=actor,
                event_type=f"action.{target.value}",
                object_type="action",
                object_id=str(action_id),
                platform_id=row["platform_id"],
                payload={"from": current.value, "to": target.value},
                created_at=timestamp,
            )

    def record_action_result(
        self,
        action_id: int,
        *,
        success: bool,
        confirmed: bool,
        external_id: str | None = None,
        receipt_url: str | None = None,
        receipt: Mapping[str, Any] | None = None,
        error: str | None = None,
        actor: str = "worker",
        pause_connection: bool = False,
        pause_reason: str | None = None,
        now: str | None = None,
    ) -> ActionStatus:
        if success:
            target = ActionStatus.SUCCEEDED if confirmed else ActionStatus.CONFIRMATION_REQUIRED
        else:
            target = ActionStatus.FAILED
        timestamp = now or utc_now()
        with self.database.transaction(immediate=True) as connection:
            row = connection.execute(
                """
                SELECT status, platform_id, connection_id
                FROM automation_actions WHERE id=?
                """,
                (action_id,),
            ).fetchone()
            if row is None:
                raise RecordNotFoundError(f"Action {action_id} does not exist")
            current = ActionStatus(row["status"])
            if target not in ACTION_TRANSITIONS[current]:
                raise StateTransitionError(
                    f"Action cannot transition from {current.value} to {target.value}"
                )
            terminal = target in {ActionStatus.SUCCEEDED, ActionStatus.FAILED}
            connection.execute(
                """
                UPDATE automation_actions SET status=?,
                    external_id=COALESCE(?, external_id),
                    receipt_url=COALESCE(?, receipt_url),
                    receipt_json=COALESCE(?, receipt_json),
                    error=?, finished_at=?, locked_by=NULL, locked_at=NULL,
                    updated_at=? WHERE id=?
                """,
                (
                    target.value,
                    external_id,
                    receipt_url,
                    _json(dict(receipt)) if receipt is not None else None,
                    error,
                    timestamp if terminal else None,
                    timestamp,
                    action_id,
                ),
            )
            self._append_audit(
                connection,
                actor=actor,
                event_type=f"action.{target.value}",
                object_type="action",
                object_id=str(action_id),
                platform_id=row["platform_id"],
                payload={"confirmed": confirmed, "success": success, "error": error},
                created_at=timestamp,
            )
            if pause_connection and row["connection_id"] is not None:
                connection_id = int(row["connection_id"])
                control_key = self._control_key(
                    ChannelControlScope.ACCOUNT.value,
                    platform_id=row["platform_id"],
                    connection_id=connection_id,
                    action_type=None,
                )
                reason = (pause_reason or error or "platform write requires attention")[:500]
                connection.execute(
                    """
                    INSERT INTO automation_channel_controls(
                        control_key, scope, platform_id, connection_id, action_type,
                        state, reason, updated_by, created_at, updated_at
                    ) VALUES(?, 'account', ?, ?, NULL, 'paused', ?, ?, ?, ?)
                    ON CONFLICT(control_key) DO UPDATE SET
                        state='paused', reason=excluded.reason,
                        updated_by=excluded.updated_by, updated_at=excluded.updated_at
                    """,
                    (
                        control_key,
                        row["platform_id"],
                        connection_id,
                        reason,
                        actor,
                        timestamp,
                        timestamp,
                    ),
                )
                self._append_audit(
                    connection,
                    actor=actor,
                    event_type="channel_control.updated",
                    object_type="channel_control",
                    object_id=control_key,
                    platform_id=row["platform_id"],
                    payload={"scope": "account", "state": "paused", "reason": reason},
                    created_at=timestamp,
                )
        return target

    def recover_stale_actions(
        self,
        *,
        before: str,
        actor: str = "startup-recovery",
        now: str | None = None,
    ) -> int:
        """Move abandoned external writes to manual confirmation without retrying."""

        timestamp = now or utc_now()
        with self.database.transaction(immediate=True) as connection:
            rows = connection.execute(
                """
                SELECT id, platform_id FROM automation_actions
                WHERE status='executing' AND COALESCE(locked_at, started_at, updated_at)<?
                ORDER BY id
                """,
                (before,),
            ).fetchall()
            for row in rows:
                connection.execute(
                    """
                    UPDATE automation_actions SET status='confirmation_required',
                        error=COALESCE(error, ?), locked_by=NULL, locked_at=NULL,
                        updated_at=? WHERE id=? AND status='executing'
                    """,
                    (
                        "worker stopped while the platform write result was unknown; do not retry",
                        timestamp,
                        row["id"],
                    ),
                )
                self._append_audit(
                    connection,
                    actor=actor,
                    event_type="action.confirmation_required",
                    object_type="action",
                    object_id=str(row["id"]),
                    platform_id=row["platform_id"],
                    payload={"reason": "stale executing action recovered without retry"},
                    created_at=timestamp,
                )
            return len(rows)

    def record_policy_decision(
        self,
        outcome: PolicyOutcome | str,
        policy_version: str,
        *,
        opportunity_id: int | None = None,
        action_id: int | None = None,
        score: float | None = None,
        rules: Sequence[Mapping[str, Any] | str] = (),
        explanation: str = "",
        now: str | None = None,
    ) -> int:
        if (opportunity_id is None) == (action_id is None):
            raise ValueError("Exactly one of opportunity_id or action_id is required")
        outcome_value = enum_value(outcome, PolicyOutcome)
        policy_version = _required(policy_version, "policy_version")
        if score is not None:
            score = _bounded_float(score, 0, 100, "score")
        timestamp = now or utc_now()
        with self.database.transaction(immediate=True) as connection:
            if opportunity_id is not None and connection.execute(
                "SELECT 1 FROM automation_opportunities WHERE id=?", (opportunity_id,)
            ).fetchone() is None:
                raise RecordNotFoundError(f"Opportunity {opportunity_id} does not exist")
            if action_id is not None and connection.execute(
                "SELECT 1 FROM automation_actions WHERE id=?", (action_id,)
            ).fetchone() is None:
                raise RecordNotFoundError(f"Action {action_id} does not exist")
            cursor = connection.execute(
                """
                INSERT INTO automation_policy_decisions(
                    opportunity_id, action_id, outcome, policy_version, score,
                    rules_json, explanation, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    opportunity_id,
                    action_id,
                    outcome_value,
                    policy_version,
                    score,
                    _json(list(rules)),
                    explanation,
                    timestamp,
                ),
            )
            decision_id = int(cursor.lastrowid)
            subject_type = "opportunity" if opportunity_id is not None else "action"
            subject_id = opportunity_id if opportunity_id is not None else action_id
            subject_table = (
                "automation_opportunities"
                if opportunity_id is not None
                else "automation_actions"
            )
            platform_row = connection.execute(
                f"SELECT platform_id FROM {subject_table} WHERE id=?",
                (subject_id,),
            ).fetchone()
            assert platform_row is not None
            self._append_audit(
                connection,
                actor="policy",
                event_type="policy.evaluated",
                object_type=subject_type,
                object_id=str(subject_id),
                platform_id=platform_row["platform_id"],
                payload={
                    "decision_id": decision_id,
                    "outcome": outcome_value,
                    "policy_version": policy_version,
                },
                created_at=timestamp,
            )
            return decision_id

    def list_policy_decisions(
        self, *, opportunity_id: int | None = None, action_id: int | None = None
    ) -> list[dict[str, Any]]:
        if (opportunity_id is None) == (action_id is None):
            raise ValueError("Exactly one of opportunity_id or action_id is required")
        column, value = (
            ("opportunity_id", opportunity_id)
            if opportunity_id is not None
            else ("action_id", action_id)
        )
        with self.database.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM automation_policy_decisions
                WHERE {column}=? ORDER BY created_at DESC, id DESC
                """,
                (value,),
            ).fetchall()
        return [_record(row, POLICY_JSON) for row in rows]  # type: ignore[misc]

    def count_action_usage(
        self,
        connection_id: int,
        action_type: ActionKind | str,
        *,
        since: str,
    ) -> int:
        """Count both completed writes and quota reserved by pending writes."""
        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS count FROM automation_actions
                WHERE connection_id=? AND action_type=?
                    AND status IN ('scheduled', 'executing', 'succeeded',
                                   'confirmation_required')
                    AND COALESCE(finished_at, started_at, scheduled_at, created_at)>=?
                """,
                (connection_id, enum_value(action_type, ActionKind), since),
            ).fetchone()
        return int(row["count"])

    def has_existing_action(
        self,
        platform_id: str,
        action_type: ActionKind | str,
        native_object_id: str,
        *,
        draft: str,
        exclude_action_id: int | None = None,
    ) -> bool:
        """Check idempotency without truncating long-running action history."""

        action_value = enum_value(action_type, ActionKind)
        clauses = [
            "platform_id=?",
            "action_type=?",
            "native_object_id=?",
            "status<>'cancelled'",
        ]
        values: list[Any] = [platform_id, action_value, native_object_id]
        if exclude_action_id is not None:
            clauses.append("id<>?")
            values.append(exclude_action_id)
        if action_value == ActionKind.DM.value:
            clauses.append(
                "(status<>'succeeded' OR COALESCE(final_text, draft)=?)"
            )
            values.append(draft)
        with self.database.connection() as connection:
            row = connection.execute(
                f"""
                SELECT 1 FROM automation_actions
                WHERE {' AND '.join(clauses)} LIMIT 1
                """,
                values,
            ).fetchone()
        return row is not None

    def has_recent_author_action(
        self,
        platform_id: str,
        connection_id: int,
        target_author_id: str,
        *,
        since: str,
    ) -> bool:
        """Check author cooldown through the full durable action history."""

        if not target_author_id:
            return False
        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT 1
                FROM automation_actions a
                JOIN automation_opportunities o ON o.id=a.opportunity_id
                WHERE a.platform_id=? AND a.connection_id=?
                    AND a.status IN ('succeeded', 'confirmation_required')
                    AND COALESCE(a.finished_at, a.updated_at)>=?
                    AND json_extract(o.payload_json, '$.target_author_id')=?
                LIMIT 1
                """,
                (platform_id, connection_id, since, target_author_id),
            ).fetchone()
        return row is not None

    def count_succeeded_actions(
        self,
        platform_id: str,
        *,
        since: str,
        automatic_only: bool = False,
    ) -> int:
        automatic = " AND approved_by IS NULL" if automatic_only else ""
        with self.database.connection() as connection:
            row = connection.execute(
                f"""
                SELECT COUNT(*) AS count FROM automation_actions
                WHERE platform_id=? AND status='succeeded'
                    AND COALESCE(finished_at, updated_at)>=?{automatic}
                """,
                (platform_id, since),
            ).fetchone()
        return int(row["count"])

    # Kill switches, channel controls, and audit

    def set_channel_control(
        self,
        scope: ChannelControlScope | str,
        state: ChannelState | str,
        *,
        platform_id: str | None = None,
        connection_id: int | None = None,
        action_type: ActionKind | str | None = None,
        reason: str = "",
        updated_by: str,
        now: str | None = None,
    ) -> str:
        scope_value = enum_value(scope, ChannelControlScope)
        state_value = enum_value(state, ChannelState)
        updated_by = _required(updated_by, "updated_by")
        normalized_action = (
            enum_value(action_type, ActionKind) if action_type is not None else None
        )
        key = self._control_key(
            scope_value,
            platform_id=platform_id,
            connection_id=connection_id,
            action_type=normalized_action,
        )
        timestamp = now or utc_now()
        with self.database.transaction(immediate=True) as connection:
            if connection_id is not None:
                assert platform_id is not None
                self._validate_connection(connection, connection_id, platform_id)
            connection.execute(
                """
                INSERT INTO automation_channel_controls(
                    control_key, scope, platform_id, connection_id, action_type,
                    state, reason, updated_by, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(control_key) DO UPDATE SET
                    state=excluded.state, reason=excluded.reason,
                    updated_by=excluded.updated_by, updated_at=excluded.updated_at
                """,
                (
                    key,
                    scope_value,
                    platform_id,
                    connection_id,
                    normalized_action,
                    state_value,
                    reason,
                    updated_by,
                    timestamp,
                    timestamp,
                ),
            )
            self._append_audit(
                connection,
                actor=updated_by,
                event_type="channel_control.updated",
                object_type="channel_control",
                object_id=key,
                platform_id=platform_id,
                payload={"scope": scope_value, "state": state_value, "reason": reason},
                created_at=timestamp,
            )
        return key

    def set_kill_switch(
        self,
        scope: ChannelControlScope | str,
        paused: bool,
        *,
        platform_id: str | None = None,
        connection_id: int | None = None,
        action_type: ActionKind | str | None = None,
        reason: str = "",
        updated_by: str,
        now: str | None = None,
    ) -> str:
        return self.set_channel_control(
            scope,
            ChannelState.PAUSED if paused else ChannelState.ENABLED,
            platform_id=platform_id,
            connection_id=connection_id,
            action_type=action_type,
            reason=reason,
            updated_by=updated_by,
            now=now,
        )

    def get_effective_channel_state(
        self,
        platform_id: str,
        *,
        connection_id: int | None = None,
        action_type: ActionKind | str | None = None,
    ) -> dict[str, Any]:
        normalized_action = (
            enum_value(action_type, ActionKind) if action_type is not None else None
        )
        with self.database.connection() as connection:
            self._validate_connection(connection, connection_id, platform_id)
            return self._effective_channel_state(
                connection,
                platform_id=platform_id,
                connection_id=connection_id,
                action_type=normalized_action,
            )

    def list_channel_controls(
        self, *, state: ChannelState | str | None = None
    ) -> list[dict[str, Any]]:
        where = ""
        values: list[Any] = []
        if state is not None:
            where = "WHERE state=?"
            values.append(enum_value(state, ChannelState))
        with self.database.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM automation_channel_controls {where}
                ORDER BY scope, control_key
                """,
                values,
            ).fetchall()
        return [dict(row) for row in rows]

    def append_audit_event(
        self,
        actor: str,
        event_type: str,
        object_type: str,
        object_id: str,
        *,
        platform_id: str | None = None,
        payload: Mapping[str, Any] | None = None,
        now: str | None = None,
    ) -> int:
        with self.database.transaction() as connection:
            return self._append_audit(
                connection,
                actor=_required(actor, "actor"),
                event_type=_required(event_type, "event_type"),
                object_type=_required(object_type, "object_type"),
                object_id=_required(object_id, "object_id"),
                platform_id=platform_id,
                payload=dict(payload or {}),
                created_at=now or utc_now(),
            )

    def list_audit_events(
        self,
        *,
        platform_id: str | None = None,
        object_type: str | None = None,
        object_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        if platform_id is not None:
            clauses.append("platform_id=?")
            values.append(platform_id)
        if object_type is not None:
            clauses.append("object_type=?")
            values.append(object_type)
        if object_id is not None:
            clauses.append("object_id=?")
            values.append(object_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        values.append(_bounded_int(limit, 1, 1000, "limit"))
        with self.database.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM automation_audit_events {where}
                ORDER BY created_at DESC, id DESC LIMIT ?
                """,
                values,
            ).fetchall()
        return [_record(row, AUDIT_JSON) for row in rows]  # type: ignore[misc]

    # Platform-center summaries

    def get_platform_summaries(
        self, platform_ids: Iterable[str] | None = None
    ) -> list[dict[str, Any]]:
        requested = list(dict.fromkeys(platform_ids or ()))
        with self.database.connection() as connection:
            if requested:
                platforms = requested
            else:
                rows = connection.execute(
                    """
                    SELECT platform_id FROM automation_platform_connections
                    UNION SELECT platform_id FROM automation_jobs
                    UNION SELECT platform_id FROM automation_opportunities
                    UNION SELECT platform_id FROM automation_actions
                    ORDER BY platform_id
                    """
                ).fetchall()
                platforms = [str(row["platform_id"]) for row in rows]
            summaries: list[dict[str, Any]] = []
            for platform_id in platforms:
                connection_counts = connection.execute(
                    """
                    SELECT COUNT(*) AS total,
                        COALESCE(SUM(enabled), 0) AS enabled,
                        COALESCE(SUM(CASE WHEN status IN ('degraded','disconnected','paused')
                            THEN 1 ELSE 0 END), 0) AS unhealthy
                    FROM automation_platform_connections WHERE platform_id=?
                    """,
                    (platform_id,),
                ).fetchone()
                job_counts = _grouped_counts(
                    connection,
                    "automation_jobs",
                    "status",
                    "platform_id=?",
                    (platform_id,),
                )
                opportunity_counts = _grouped_counts(
                    connection,
                    "automation_opportunities",
                    "status",
                    "platform_id=?",
                    (platform_id,),
                )
                action_counts = _grouped_counts(
                    connection,
                    "automation_actions",
                    "status",
                    "platform_id=?",
                    (platform_id,),
                )
                paused_channels = connection.execute(
                    """
                    SELECT COUNT(*) AS count FROM automation_channel_controls
                    WHERE state='paused' AND (
                        (scope='platform' AND platform_id=?) OR
                        (scope='account' AND platform_id=?)
                    )
                    """,
                    (platform_id, platform_id),
                ).fetchone()
                exception_actions = action_counts.get(ActionStatus.CONFIRMATION_REQUIRED.value, 0)
                exception_actions += action_counts.get(ActionStatus.FAILED.value, 0)
                summaries.append(
                    {
                        "platform_id": platform_id,
                        "connection_count": int(connection_counts["total"]),
                        "enabled_connection_count": int(connection_counts["enabled"]),
                        "unhealthy_connection_count": int(connection_counts["unhealthy"]),
                        "queued_job_count": job_counts.get(CoreJobStatus.QUEUED.value, 0),
                        "running_job_count": job_counts.get(CoreJobStatus.RUNNING.value, 0),
                        "failed_job_count": job_counts.get(CoreJobStatus.FAILED.value, 0),
                        "open_opportunity_count": opportunity_counts.get(
                            OpportunityStatus.OPEN.value, 0
                        ),
                        "review_action_count": action_counts.get(
                            ActionStatus.NEEDS_REVIEW.value, 0
                        ),
                        "succeeded_action_count": action_counts.get(
                            ActionStatus.SUCCEEDED.value, 0
                        ),
                        "exception_action_count": exception_actions,
                        "paused_channel_count": int(paused_channels["count"]),
                    }
                )
        return summaries

    def get_global_summary(self) -> dict[str, Any]:
        with self.database.connection() as connection:
            global_control = connection.execute(
                """
                SELECT state, reason FROM automation_channel_controls
                WHERE control_key='global'
                """
            ).fetchone()
            row = connection.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM automation_jobs WHERE status='running') AS running_jobs,
                    (SELECT COUNT(*) FROM automation_opportunities WHERE status='open') AS open_opportunities,
                    (SELECT COUNT(*) FROM automation_actions WHERE status='needs_review') AS review_actions,
                    (SELECT COUNT(*) FROM automation_actions
                        WHERE status IN ('confirmation_required','failed')) AS exception_actions,
                    (SELECT COUNT(*) FROM automation_platform_connections
                        WHERE status IN ('degraded','disconnected','paused')) AS unhealthy_connections
                """
            ).fetchone()
        output = dict(row)
        output["global_kill_switch"] = bool(
            global_control is not None and global_control["state"] == ChannelState.PAUSED.value
        )
        output["global_kill_switch_reason"] = (
            str(global_control["reason"]) if global_control is not None else ""
        )
        output["alert_count"] = int(output["exception_actions"]) + int(
            output["unhealthy_connections"]
        )
        return output

    # Internal helpers; callers should keep product decisions in services.

    def _validate_connection(
        self,
        connection: sqlite3.Connection,
        connection_id: int | None,
        platform_id: str,
    ) -> None:
        if connection_id is None:
            return
        row = connection.execute(
            "SELECT platform_id FROM automation_platform_connections WHERE id=?",
            (connection_id,),
        ).fetchone()
        if row is None:
            raise RecordNotFoundError(f"Platform connection {connection_id} does not exist")
        if row["platform_id"] != platform_id:
            raise ValueError(
                f"Connection {connection_id} belongs to {row['platform_id']}, not {platform_id}"
            )

    def _raise_job_transition(
        self, connection: sqlite3.Connection, job_id: int, target: str
    ) -> None:
        row = connection.execute(
            "SELECT status FROM automation_jobs WHERE id=?", (job_id,)
        ).fetchone()
        if row is None:
            raise RecordNotFoundError(f"Job {job_id} does not exist")
        raise StateTransitionError(
            f"Job {job_id} cannot transition from {row['status']} to {target}"
        )

    def _control_key(
        self,
        scope: str,
        *,
        platform_id: str | None,
        connection_id: int | None,
        action_type: str | None,
    ) -> str:
        if scope == ChannelControlScope.GLOBAL.value:
            if any(value is not None for value in (platform_id, connection_id, action_type)):
                raise ValueError("Global control cannot specify platform, account, or action type")
            return "global"
        if scope == ChannelControlScope.PLATFORM.value:
            if not platform_id or connection_id is not None or action_type is not None:
                raise ValueError("Platform control requires only platform_id")
            return f"platform:{platform_id}"
        if scope == ChannelControlScope.ACCOUNT.value:
            if not platform_id or connection_id is None or action_type is not None:
                raise ValueError("Account control requires platform_id and connection_id")
            return f"account:{platform_id}:{connection_id}"
        if scope == ChannelControlScope.ACTION_TYPE.value:
            if action_type is None or platform_id is not None or connection_id is not None:
                raise ValueError("Action-type control requires only action_type")
            return f"action_type:{action_type}"
        raise ValueError(f"Unsupported channel control scope: {scope}")

    def _effective_channel_state(
        self,
        connection: sqlite3.Connection,
        *,
        platform_id: str,
        connection_id: int | None,
        action_type: str | None,
    ) -> dict[str, Any]:
        keys = ["global", f"platform:{platform_id}"]
        if connection_id is not None:
            keys.append(f"account:{platform_id}:{connection_id}")
        if action_type is not None:
            keys.append(f"action_type:{action_type}")
        rows = connection.execute(
            f"""
            SELECT * FROM automation_channel_controls
            WHERE control_key IN ({','.join('?' for _ in keys)})
            ORDER BY CASE scope WHEN 'global' THEN 1 WHEN 'platform' THEN 2
                WHEN 'account' THEN 3 ELSE 4 END
            """,
            keys,
        ).fetchall()
        blocked_by = [dict(row) for row in rows if row["state"] == ChannelState.PAUSED.value]
        if connection_id is not None:
            account = connection.execute(
                """
                SELECT enabled, status, last_health_error
                FROM automation_platform_connections WHERE id=?
                """,
                (connection_id,),
            ).fetchone()
            if account is not None and (
                not bool(account["enabled"])
                or account["status"] != PlatformConnectionStatus.CONNECTED.value
            ):
                blocked_by.append(
                    {
                        "control_key": f"connection:{connection_id}",
                        "scope": ChannelControlScope.ACCOUNT.value,
                        "platform_id": platform_id,
                        "connection_id": connection_id,
                        "action_type": None,
                        "state": ChannelState.PAUSED.value,
                        "reason": account["last_health_error"]
                        or f"connection is {account['status']}",
                        "source": "connection_health",
                    }
                )
        return {"allowed": not blocked_by, "blocked_by": blocked_by}

    def _append_audit(
        self,
        connection: sqlite3.Connection,
        *,
        actor: str,
        event_type: str,
        object_type: str,
        object_id: str,
        platform_id: str | None,
        payload: Mapping[str, Any],
        created_at: str,
    ) -> int:
        cursor = connection.execute(
            """
            INSERT INTO automation_audit_events(
                actor, event_type, object_type, object_id, platform_id, payload_json, created_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (
                actor,
                event_type,
                object_type,
                object_id,
                platform_id,
                _json(dict(payload)),
                created_at,
            ),
        )
        return int(cursor.lastrowid)


def _required(value: str, field: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field} is required")
    return normalized


def _bounded_int(value: int, minimum: int, maximum: int, field: str) -> int:
    normalized = int(value)
    if not minimum <= normalized <= maximum:
        raise ValueError(f"{field} must be between {minimum} and {maximum}")
    return normalized


def _bounded_float(value: float, minimum: float, maximum: float, field: str) -> float:
    normalized = float(value)
    if not minimum <= normalized <= maximum:
        raise ValueError(f"{field} must be between {minimum} and {maximum}")
    return normalized


def _grouped_counts(
    connection: sqlite3.Connection,
    table: str,
    column: str,
    where: str,
    values: Sequence[Any],
) -> dict[str, int]:
    # Table and column are internal constants, never caller-provided identifiers.
    rows = connection.execute(
        f"SELECT {column} AS name, COUNT(*) AS count FROM {table} WHERE {where} GROUP BY {column}",
        values,
    ).fetchall()
    return {str(row["name"]): int(row["count"]) for row in rows}
