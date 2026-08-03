from __future__ import annotations

import json
import os
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator

from .repository import AutomationStore, RecordNotFoundError, utc_now


AgentStatus = Literal["draft", "running", "paused", "auto_paused", "archived"]

OPERATION_FIELDS = {
    "scan_interval_minutes", "candidates_per_scan", "replies_per_scan",
    "post_freshness_hours", "reply_hourly_limit", "reply_daily_limit",
    "original_posts_daily", "active_start", "active_end", "timezone",
    "author_cooldown_days", "conversation_turn_limit", "failure_pause_threshold",
    "score_threshold", "reply_delay_min_minutes", "reply_delay_max_minutes",
    "original_post_times",
}


class AgentConfig(BaseModel):
    """Versioned, non-secret configuration for one public social agent."""

    role: str = "DeFi researcher"
    audience: str = "Crypto builders, researchers, and informed users"
    expertise: list[str] = Field(default_factory=lambda: ["DeFi", "crypto"])
    tone: str = "insightful, concise, conversational"
    primary_language: str = "English"
    system_prompt: str = ""
    keywords: list[str] = Field(default_factory=list)
    priority_accounts: list[str] = Field(default_factory=list)
    excluded_accounts: list[str] = Field(default_factory=list)
    approved_domains: list[str] = Field(default_factory=list)
    model: str = ""
    temperature: float = Field(default=0.5, ge=0, le=2)
    max_output_tokens: int = Field(default=2000, ge=64, le=4000)
    reasoning_effort: str = "medium"
    inherit_global_operations: bool = True
    request_timeout_seconds: int = Field(default=60, ge=5, le=180)
    model_retries: int = Field(default=1, ge=0, le=3)
    scan_interval_minutes: int = Field(default=30, ge=10, le=1440)
    candidates_per_scan: int = Field(default=50, ge=1, le=100)
    replies_per_scan: int = Field(default=3, ge=1, le=3)
    post_freshness_hours: int = Field(default=6, ge=1, le=24)
    reply_hourly_limit: int = Field(default=3, ge=0, le=3)
    reply_daily_limit: int = Field(default=24, ge=0, le=24)
    original_posts_daily: int = Field(default=4, ge=0, le=4)
    active_start: str = "08:00"
    active_end: str = "23:00"
    timezone: str = "Asia/Shanghai"
    author_cooldown_days: int = Field(default=7, ge=1, le=90)
    conversation_turn_limit: int = Field(default=3, ge=0, le=3)
    failure_pause_threshold: int = Field(default=3, ge=1, le=10)
    score_threshold: float = Field(default=80, ge=80, le=100)
    reply_delay_min_minutes: int = Field(default=2, ge=0, le=30)
    reply_delay_max_minutes: int = Field(default=8, ge=0, le=60)
    original_post_times: list[str] = Field(
        default_factory=lambda: ["09:30", "12:30", "17:30", "21:00"]
    )

    @field_validator(
        "expertise",
        "keywords",
        "priority_accounts",
        "excluded_accounts",
        "approved_domains",
        mode="before",
    )
    @classmethod
    def normalize_string_list(cls, value: Any) -> list[str]:
        if isinstance(value, str):
            value = value.replace("\r", "\n").replace(",", "\n").splitlines()
        return list(dict.fromkeys(str(item).strip() for item in (value or ()) if str(item).strip()))

    @field_validator("active_start", "active_end")
    @classmethod
    def validate_clock(cls, value: str) -> str:
        datetime.strptime(value, "%H:%M")
        return value

    @field_validator("original_post_times", mode="before")
    @classmethod
    def normalize_post_times(cls, value: Any) -> list[str]:
        if isinstance(value, str):
            value = value.replace(",", "\n").splitlines()
        output = list(dict.fromkeys(str(item).strip() for item in (value or ()) if str(item).strip()))
        for item in output:
            datetime.strptime(item, "%H:%M")
        return output

    @model_validator(mode="after")
    def validate_limits(self) -> "AgentConfig":
        if self.reply_delay_max_minutes < self.reply_delay_min_minutes:
            raise ValueError("Maximum reply delay must not be below the minimum")
        if len(self.original_post_times) != self.original_posts_daily:
            raise ValueError("Original post times must match the daily original-post count")
        try:
            ZoneInfo(self.timezone)
        except Exception as exc:
            raise ValueError("Invalid agent timezone") from exc
        return self


class RuntimeModel(BaseModel):
    id: str
    label: str = ""


class RuntimeModelConfig(BaseModel):
    base_url: str = "https://api.openai.com/v1"
    api_key: SecretStr
    allowed_models: list[RuntimeModel | str]

    @field_validator("allowed_models", mode="after")
    @classmethod
    def require_models(cls, values: list[RuntimeModel | str]) -> list[RuntimeModel | str]:
        if not values:
            raise ValueError("At least one model is required")
        return values

    def model_ids(self) -> tuple[str, ...]:
        return tuple(value if isinstance(value, str) else value.id for value in self.allowed_models)


class TelegramConfig(BaseModel):
    enabled: bool = False
    bot_token: SecretStr = SecretStr("")
    chat_id: str = ""
    daily_summary_time: str = "23:10"
    timezone: str = "Asia/Shanghai"
    timeout_seconds: int = Field(default=10, ge=2, le=30)


class RuntimeHardLimits(BaseModel):
    minimum_scan_interval_minutes: int = Field(default=10, ge=1)
    maximum_candidates_per_scan: int = Field(default=100, ge=1, le=100)
    maximum_replies_per_scan: int = Field(default=3, ge=1, le=3)
    maximum_replies_hourly: int = Field(default=3, ge=0, le=3)
    maximum_replies_daily: int = Field(default=24, ge=0, le=24)
    maximum_original_posts_daily: int = Field(default=4, ge=0, le=4)


class RuntimeAgentConfig(BaseModel):
    version: int = 1
    model: RuntimeModelConfig
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    hard_limits: RuntimeHardLimits = Field(default_factory=RuntimeHardLimits)

    def validate_agent(self, config: AgentConfig) -> None:
        limits = self.hard_limits
        errors: list[str] = []
        if config.scan_interval_minutes < limits.minimum_scan_interval_minutes:
            errors.append("scan interval is below the local hard limit")
        if config.candidates_per_scan > limits.maximum_candidates_per_scan:
            errors.append("candidate count exceeds the local hard limit")
        if config.replies_per_scan > limits.maximum_replies_per_scan:
            errors.append("replies per scan exceed the local hard limit")
        if config.reply_hourly_limit > limits.maximum_replies_hourly:
            errors.append("hourly reply limit exceeds the local hard limit")
        if config.reply_daily_limit > limits.maximum_replies_daily:
            errors.append("daily reply limit exceeds the local hard limit")
        if config.original_posts_daily > limits.maximum_original_posts_daily:
            errors.append("daily original-post limit exceeds the local hard limit")
        if errors:
            raise ValueError("; ".join(errors))

    @classmethod
    def load(cls, path: Path | None = None) -> "RuntimeAgentConfig":
        resolved = path or agent_runtime_config_path()
        info = resolved.stat()
        if info.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise RuntimeError(f"Agent config permissions must be 0600: {resolved}")
        try:
            payload = json.loads(resolved.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Unable to read agent runtime config: {resolved}") from exc
        return cls.model_validate(payload)


def agent_runtime_config_path() -> Path:
    configured = os.environ.get("KOL_AGENT_CONFIG_PATH", "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".config" / "kol-search" / "config.json"


DEFAULT_AGENT_CONFIG = AgentConfig().model_dump()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))


def _decode(value: str | None, fallback: Any) -> Any:
    try:
        return json.loads(value or "")
    except (json.JSONDecodeError, TypeError):
        return fallback


class AgentStore:
    """Persistence and state transitions for 1:1 platform-account agents."""

    def __init__(self, automation: AutomationStore) -> None:
        self.automation = automation
        self.database = automation.database

    def record_runtime_status(
        self,
        worker_id: str,
        *,
        model_ready: bool,
        telegram_ready: bool,
        write_ready: bool,
        allowed_models: Sequence[str] = (),
        config_error: str | None = None,
        now: str | None = None,
    ) -> None:
        timestamp = now or utc_now()
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO automation_agent_runtime_status(
                    worker_id, model_ready, telegram_ready, write_ready, allowed_models_json,
                    config_error, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(worker_id) DO UPDATE SET
                    model_ready=excluded.model_ready,
                    telegram_ready=excluded.telegram_ready,
                    write_ready=excluded.write_ready,
                    allowed_models_json=excluded.allowed_models_json,
                    config_error=excluded.config_error,
                    updated_at=excluded.updated_at
                """,
                (
                    worker_id, int(model_ready), int(telegram_ready), int(write_ready),
                    _json(list(allowed_models)), config_error, timestamp,
                ),
            )

    def latest_runtime_status(self) -> dict[str, Any] | None:
        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM automation_agent_runtime_status
                ORDER BY updated_at DESC LIMIT 1
                """
            ).fetchone()
        if not row:
            return None
        output = dict(row)
        output["model_ready"] = bool(output["model_ready"])
        output["telegram_ready"] = bool(output["telegram_ready"])
        output["write_ready"] = bool(output["write_ready"])
        output["allowed_models"] = _decode(
            output.pop("allowed_models_json", None), []
        )
        return output

    def get_defaults(self) -> dict[str, Any]:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT config_json FROM automation_agent_defaults WHERE id=1"
            ).fetchone()
        stored = _decode(row["config_json"], {}) if row else {}
        return AgentConfig.model_validate({**DEFAULT_AGENT_CONFIG, **stored}).model_dump()

    def save_defaults(self, config: Mapping[str, Any], *, now: str | None = None) -> None:
        normalized = AgentConfig.model_validate(config).model_dump()
        timestamp = now or utc_now()
        inherited_agent_ids: list[int] = []
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO automation_agent_defaults(id, config_json, updated_at)
                VALUES(1, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    config_json=excluded.config_json, updated_at=excluded.updated_at
                """,
                (_json(normalized), timestamp),
            )
            agents = connection.execute(
                """
                SELECT a.id, a.current_config_version_id, v.config_json,
                    COALESCE(MAX(all_versions.version), 0) AS max_version
                FROM automation_agents a
                JOIN automation_agent_config_versions v ON v.id=a.current_config_version_id
                JOIN automation_agent_config_versions all_versions ON all_versions.agent_id=a.id
                WHERE a.status<>'archived'
                GROUP BY a.id, a.current_config_version_id, v.config_json
                """
            ).fetchall()
            for agent in agents:
                existing = AgentConfig.model_validate(
                    _decode(agent["config_json"], {})
                ).model_dump()
                if not existing.get("inherit_global_operations", True):
                    continue
                inherited_agent_ids.append(int(agent["id"]))
                resolved = dict(existing)
                for key in OPERATION_FIELDS:
                    resolved[key] = normalized[key]
                cursor = connection.execute(
                    """
                    INSERT INTO automation_agent_config_versions(
                        agent_id, version, config_json, created_by, created_at
                    ) VALUES(?, ?, ?, 'global-defaults', ?) RETURNING id
                    """,
                    (
                        agent["id"], int(agent["max_version"]) + 1,
                        _json(resolved), timestamp,
                    ),
                )
                version_id = int(cursor.fetchone()["id"])
                connection.execute(
                    """
                    UPDATE automation_agents SET current_config_version_id=?, updated_at=?
                    WHERE id=?
                    """,
                    (version_id, timestamp, agent["id"]),
                )
        for agent_id in inherited_agent_ids:
            self._sync_quotas(agent_id, self.get_config(agent_id))

    def create_agent(
        self,
        *,
        name: str,
        platform_id: str,
        native_account_id: str,
        native_username: str,
        reply_connection_id: int,
        publish_connection_id: int,
        config: Mapping[str, Any] | None,
        actor: str,
        now: str | None = None,
    ) -> int:
        timestamp = now or utc_now()
        if not name.strip() or not native_account_id.strip() or not native_username.strip():
            raise ValueError("Agent name, account ID, and username are required")
        reply = self.automation.get_platform_connection(reply_connection_id)
        publish = self.automation.get_platform_connection(publish_connection_id)
        if not reply or not publish:
            raise ValueError("Both reply and publishing connections are required")
        if reply["platform_id"] != platform_id or publish["platform_id"] != platform_id:
            raise ValueError("Agent connections must belong to the selected platform")
        if "comment" not in reply.get("capabilities", ()):
            raise ValueError("Reply connection does not support comments")
        if "owned_publish" not in publish.get("capabilities", ()):
            raise ValueError("Publishing connection does not support owned posts")
        wanted_id = native_account_id.strip()
        wanted_username = native_username.strip().lstrip("@").casefold()
        for label, connection in (("OpenCLI", reply), ("Postiz", publish)):
            metadata = connection.get("metadata") or {}
            actual_id = str(metadata.get("external_account_id") or "").strip()
            actual_username = str(metadata.get("username") or "").strip().lstrip("@").casefold()
            if not actual_id or not actual_username:
                raise ValueError(f"{label} connection must declare its X account ID and username")
            if actual_id and actual_id != wanted_id:
                raise ValueError(f"{label} connection belongs to a different account ID")
            if actual_username and actual_username != wanted_username:
                raise ValueError(f"{label} connection belongs to a different username")
        normalized = AgentConfig.model_validate(
            {**self.get_defaults(), **dict(config or {})}
        ).model_dump()
        if normalized.get("inherit_global_operations", True):
            defaults = self.get_defaults()
            for key in OPERATION_FIELDS:
                normalized[key] = defaults[key]
        if normalized["model"] == "":
            raise ValueError("Agent model is required")
        with self.database.transaction(immediate=True) as connection:
            cursor = connection.execute(
                """
                INSERT INTO automation_agents(
                    name, platform_id, native_account_id, native_username,
                    reply_connection_id, publish_connection_id, status,
                    created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, 'draft', ?, ?)
                RETURNING id
                """,
                (
                    name.strip(), platform_id, wanted_id,
                    native_username.strip().lstrip("@"), reply_connection_id,
                    publish_connection_id, timestamp, timestamp,
                ),
            )
            agent_id = int(cursor.fetchone()["id"])
            version_cursor = connection.execute(
                """
                INSERT INTO automation_agent_config_versions(
                    agent_id, version, config_json, created_by, created_at
                ) VALUES(?, 1, ?, ?, ?) RETURNING id
                """,
                (agent_id, _json(normalized), actor, timestamp),
            )
            version_id = int(version_cursor.fetchone()["id"])
            connection.execute(
                "UPDATE automation_agents SET current_config_version_id=? WHERE id=?",
                (version_id, agent_id),
            )
        self.append_event(
            agent_id,
            "agent.created",
            title="Agent 已创建",
            message="Agent 默认暂停，启用后将直接执行真实平台写入。",
            payload={"actor": actor},
            now=timestamp,
        )
        self._sync_quotas(agent_id, normalized)
        return agent_id

    def list_agents(self, *, include_archived: bool = False) -> list[dict[str, Any]]:
        where = "" if include_archived else "WHERE a.status<>'archived'"
        with self.database.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT a.*,
                    rc.status AS reply_connection_status,
                    pc.status AS publish_connection_status,
                    (SELECT COUNT(*) FROM automation_actions x
                     WHERE x.agent_id=a.id AND x.action_type='comment'
                       AND x.created_at>=?) AS replies_today,
                    (SELECT COUNT(*) FROM automation_actions x
                     WHERE x.agent_id=a.id AND x.action_type='owned_post'
                       AND x.created_at>=?) AS posts_today,
                    (SELECT MAX(x.updated_at) FROM automation_actions x
                     WHERE x.agent_id=a.id) AS last_activity_at
                FROM automation_agents a
                JOIN automation_platform_connections rc ON rc.id=a.reply_connection_id
                JOIN automation_platform_connections pc ON pc.id=a.publish_connection_id
                {where}
                ORDER BY CASE a.status WHEN 'running' THEN 0 WHEN 'auto_paused' THEN 1
                    WHEN 'paused' THEN 2 WHEN 'draft' THEN 3 ELSE 4 END, a.id
                """,
                (self._day_start(), self._day_start()),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_agent(self, agent_id: int) -> dict[str, Any] | None:
        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT a.*, rc.display_name AS reply_connection_name,
                    rc.status AS reply_connection_status,
                    rc.metadata_json AS reply_metadata_json,
                    pc.display_name AS publish_connection_name,
                    pc.status AS publish_connection_status,
                    pc.metadata_json AS publish_metadata_json,
                    v.id AS config_id, v.version AS config_version,
                    v.config_json AS agent_config_json
                FROM automation_agents a
                JOIN automation_platform_connections rc ON rc.id=a.reply_connection_id
                JOIN automation_platform_connections pc ON pc.id=a.publish_connection_id
                LEFT JOIN automation_agent_config_versions v
                    ON v.id=a.current_config_version_id
                WHERE a.id=?
                """,
                (agent_id,),
            ).fetchone()
        if not row:
            return None
        output = dict(row)
        output["reply_metadata"] = _decode(output.pop("reply_metadata_json", None), {})
        output["publish_metadata"] = _decode(output.pop("publish_metadata_json", None), {})
        config_json = output.pop("agent_config_json", None)
        config_id = output.pop("config_id", None)
        config_version = output.pop("config_version", None)
        if config_json is None or config_id is None or config_version is None:
            raise RecordNotFoundError(f"Agent {agent_id} has no configuration")
        output["config"] = {
            "id": int(config_id),
            "version": int(config_version),
            **AgentConfig.model_validate(_decode(config_json, {})).model_dump(),
        }
        return output

    def get_config(self, agent_id: int) -> dict[str, Any]:
        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT v.* FROM automation_agent_config_versions v
                JOIN automation_agents a ON a.current_config_version_id=v.id
                WHERE a.id=?
                """,
                (agent_id,),
            ).fetchone()
        if not row:
            raise RecordNotFoundError(f"Agent {agent_id} has no configuration")
        return {
            "id": int(row["id"]),
            "version": int(row["version"]),
            **AgentConfig.model_validate(_decode(row["config_json"], {})).model_dump(),
        }

    def save_config(
        self,
        agent_id: int,
        config: Mapping[str, Any],
        *,
        actor: str,
        now: str | None = None,
    ) -> int:
        agent = self.get_agent(agent_id)
        if not agent:
            raise RecordNotFoundError(f"Agent {agent_id} does not exist")
        if agent["status"] == "running":
            raise ValueError("Pause the Agent before changing its configuration")
        normalized = AgentConfig.model_validate(config).model_dump()
        if normalized.get("inherit_global_operations", True):
            defaults = self.get_defaults()
            for key in OPERATION_FIELDS:
                normalized[key] = defaults[key]
        timestamp = now or utc_now()
        with self.database.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(version), 0) AS version FROM automation_agent_config_versions WHERE agent_id=?",
                (agent_id,),
            ).fetchone()
            version = int(row["version"]) + 1
            cursor = connection.execute(
                """
                INSERT INTO automation_agent_config_versions(
                    agent_id, version, config_json, created_by, created_at
                ) VALUES(?, ?, ?, ?, ?) RETURNING id
                """,
                (agent_id, version, _json(normalized), actor, timestamp),
            )
            version_id = int(cursor.fetchone()["id"])
            connection.execute(
                """
                UPDATE automation_agents SET current_config_version_id=?, updated_at=?
                WHERE id=?
                """,
                (version_id, timestamp, agent_id),
            )
            connection.execute(
                """
                UPDATE automation_actions SET status='cancelled',
                    error='cancelled after Agent configuration changed', updated_at=?
                WHERE agent_id=? AND status IN ('proposed', 'needs_review', 'scheduled')
                """,
                (timestamp, agent_id),
            )
        self.append_event(
            agent_id,
            "agent.config.updated",
            title=f"配置已更新至 v{version}",
            message="旧配置下尚未执行的内容已取消。",
            payload={"actor": actor, "config_version_id": version_id},
            now=timestamp,
        )
        self._sync_quotas(agent_id, normalized)
        return version_id

    def set_status(
        self,
        agent_id: int,
        status: AgentStatus,
        *,
        actor: str,
        reason: str = "",
        now: str | None = None,
    ) -> None:
        agent = self.get_agent(agent_id)
        if not agent:
            raise RecordNotFoundError(f"Agent {agent_id} does not exist")
        if agent["status"] == "archived" and status not in {"paused", "archived"}:
            raise ValueError("Restore an archived Agent to paused before enabling it")
        if status == "running":
            self.validate_identity(agent_id)
        timestamp = now or utc_now()
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                """
                UPDATE automation_agents SET status=?, pause_reason=?,
                    next_scan_at=CASE WHEN ?='running' THEN ? ELSE next_scan_at END,
                    consecutive_failures=CASE WHEN ?='running' THEN 0
                        ELSE consecutive_failures END,
                    updated_at=? WHERE id=?
                """,
                (status, reason, status, timestamp, status, timestamp, agent_id),
            )
        paused = status != "running"
        for connection_id in {
            int(agent["reply_connection_id"]), int(agent["publish_connection_id"])
        }:
            self.automation.set_kill_switch(
                "account",
                paused,
                platform_id=str(agent["platform_id"]),
                connection_id=connection_id,
                reason=reason or f"Agent status: {status}",
                updated_by=actor,
                now=timestamp,
            )
        self.append_event(
            agent_id,
            f"agent.{status}",
            severity="critical" if status == "auto_paused" else "info",
            title={
                "running": "Agent 已启用",
                "paused": "Agent 已暂停",
                "auto_paused": "Agent 已自动暂停",
                "archived": "Agent 已归档",
                "draft": "Agent 草稿",
            }[status],
            message=reason,
            payload={"actor": actor},
            now=timestamp,
        )

    def validate_identity(self, agent_id: int) -> None:
        agent = self.get_agent(agent_id)
        if not agent:
            raise RecordNotFoundError(f"Agent {agent_id} does not exist")
        wanted_id = str(agent["native_account_id"])
        wanted_username = str(agent["native_username"]).casefold().lstrip("@")
        for label, metadata in (
            ("OpenCLI", agent.get("reply_metadata") or {}),
            ("Postiz", agent.get("publish_metadata") or {}),
        ):
            actual_id = str(metadata.get("external_account_id") or "")
            actual_username = str(metadata.get("username") or "").casefold().lstrip("@")
            if actual_id != wanted_id or actual_username != wanted_username:
                raise ValueError(f"{label} identity no longer matches this Agent")

    def queue_command(self, agent_id: int, command_type: str, *, actor: str) -> int:
        if command_type not in {"scan_now", "post_now"}:
            raise ValueError("Unknown Agent command")
        agent = self.get_agent(agent_id)
        if not agent or agent["status"] != "running":
            raise ValueError("Agent must be running before a command can execute")
        timestamp = utc_now()
        with self.database.transaction(immediate=True) as connection:
            cursor = connection.execute(
                """
                INSERT INTO automation_agent_commands(
                    agent_id, command_type, requested_by, created_at
                ) VALUES(?, ?, ?, ?) RETURNING id
                """,
                (agent_id, command_type, actor, timestamp),
            )
            command_id = int(cursor.fetchone()["id"])
        self.append_event(
            agent_id,
            "agent.command.queued",
            title="立即任务已排队",
            message="立即扫描" if command_type == "scan_now" else "立即生成原创帖",
            payload={"command_id": command_id, "command_type": command_type},
        )
        return command_id

    def claim_command(self) -> dict[str, Any] | None:
        timestamp = utc_now()
        with self.database.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM automation_agent_commands WHERE status='queued' ORDER BY id LIMIT 1"
            ).fetchone()
            if not row:
                return None
            cursor = connection.execute(
                "UPDATE automation_agent_commands SET status='running', started_at=? WHERE id=? AND status='queued'",
                (timestamp, row["id"]),
            )
            return dict(row) if cursor.rowcount == 1 else None

    def finish_command(self, command_id: int, *, error: str | None = None) -> None:
        timestamp = utc_now()
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                """
                UPDATE automation_agent_commands SET status=?, error=?, finished_at=?
                WHERE id=? AND status='running'
                """,
                ("failed" if error else "succeeded", error, timestamp, command_id),
            )

    def due_agents(self, *, now: str | None = None) -> list[dict[str, Any]]:
        timestamp = now or utc_now()
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM automation_agents
                WHERE status='running' AND (next_scan_at IS NULL OR next_scan_at<=?)
                ORDER BY COALESCE(next_scan_at, created_at), id
                """,
                (timestamp,),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_scanned(
        self,
        agent_id: int,
        *,
        interval_minutes: int,
        cursor: str | None = None,
        now: str | None = None,
    ) -> None:
        timestamp = now or utc_now()
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        next_scan = (parsed + timedelta(minutes=interval_minutes)).isoformat()
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                """
                UPDATE automation_agents SET last_scan_at=?, next_scan_at=?,
                    scan_cursor=COALESCE(?, scan_cursor),
                    consecutive_failures=0, updated_at=? WHERE id=?
                """,
                (timestamp, next_scan, cursor, timestamp, agent_id),
            )

    def record_failure(self, agent_id: int, error: str) -> int:
        timestamp = utc_now()
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                """
                UPDATE automation_agents SET consecutive_failures=consecutive_failures+1,
                    updated_at=? WHERE id=?
                """,
                (timestamp, agent_id),
            )
            row = connection.execute(
                "SELECT consecutive_failures FROM automation_agents WHERE id=?",
                (agent_id,),
            ).fetchone()
        count = int(row["consecutive_failures"])
        self.append_event(
            agent_id,
            "agent.failure",
            severity="warning",
            title="Agent 执行失败",
            message=error,
            payload={"consecutive_failures": count},
        )
        return count

    def clear_failures(self, agent_id: int) -> None:
        """Reset the consecutive failure counter after a successful Agent operation."""

        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                """
                UPDATE automation_agents SET consecutive_failures=0, updated_at=?
                WHERE id=? AND consecutive_failures<>0
                """,
                (utc_now(), agent_id),
            )

    def attach_opportunity(self, opportunity_id: int, agent_id: int, config_version_id: int) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE automation_opportunities SET agent_id=?, agent_config_version_id=? WHERE id=?",
                (agent_id, config_version_id, opportunity_id),
            )

    def attach_action(self, action_id: int, agent_id: int, config_version_id: int) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE automation_actions SET agent_id=?, agent_config_version_id=? WHERE id=?",
                (agent_id, config_version_id, action_id),
            )

    def action_count(
        self,
        agent_id: int,
        action_type: str,
        *,
        since: str,
        statuses: Sequence[str] = ("scheduled", "executing", "succeeded", "confirmation_required"),
    ) -> int:
        placeholders = ",".join("?" for _ in statuses)
        with self.database.connection() as connection:
            row = connection.execute(
                f"""
                SELECT COUNT(*) AS count FROM automation_actions
                WHERE agent_id=? AND action_type=? AND created_at>=?
                    AND status IN ({placeholders})
                """,
                (agent_id, action_type, since, *statuses),
            ).fetchone()
        return int(row["count"])

    def usage_counts(
        self,
        agent_id: int,
        *,
        day_start: str,
        hour_start: str,
    ) -> dict[str, int]:
        """Fetch the Agent detail quotas in one database round trip."""

        active = ("scheduled", "executing", "succeeded", "confirmation_required")
        placeholders = ",".join("?" for _ in active)
        with self.database.connection() as connection:
            row = connection.execute(
                f"""
                SELECT
                    SUM(CASE WHEN action_type='comment' AND created_at>=?
                        AND status IN ({placeholders}) THEN 1 ELSE 0 END) AS replies_today,
                    SUM(CASE WHEN action_type='comment' AND created_at>=?
                        AND status IN ({placeholders}) THEN 1 ELSE 0 END) AS replies_hour,
                    SUM(CASE WHEN action_type='owned_post' AND created_at>=?
                        AND status IN ({placeholders}) THEN 1 ELSE 0 END) AS posts_today
                FROM automation_actions WHERE agent_id=?
                """,
                (
                    day_start, *active,
                    hour_start, *active,
                    day_start, *active,
                    agent_id,
                ),
            ).fetchone()
        return {
            "replies_today": int(row["replies_today"] or 0),
            "replies_hour": int(row["replies_hour"] or 0),
            "posts_today": int(row["posts_today"] or 0),
        }

    def has_target_action(self, agent_id: int, native_object_id: str) -> bool:
        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM automation_actions
                WHERE agent_id=? AND action_type='comment' AND native_object_id=?
                    AND status<>'cancelled' LIMIT 1
                """,
                (agent_id, native_object_id),
            ).fetchone()
        return row is not None

    def has_action_key(self, idempotency_key: str) -> bool:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT 1 FROM automation_actions WHERE idempotency_key=? LIMIT 1",
                (idempotency_key,),
            ).fetchone()
        return row is not None

    def append_event(
        self,
        agent_id: int,
        event_type: str,
        *,
        severity: str = "info",
        title: str = "",
        message: str = "",
        payload: Mapping[str, Any] | None = None,
        now: str | None = None,
    ) -> int:
        timestamp = now or utc_now()
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                INSERT INTO automation_agent_events(
                    agent_id, event_type, severity, title, message, payload_json, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?) RETURNING id
                """,
                (agent_id, event_type, severity, title, message, _json(dict(payload or {})), timestamp),
            )
            return int(cursor.fetchone()["id"])

    def timeline(
        self,
        agent_id: int,
        *,
        before_created_at: str | None = None,
        before_id: int | None = None,
        limit: int = 30,
    ) -> list[dict[str, Any]]:
        clauses = ["a.agent_id=?", "a.action_type IN ('comment', 'owned_post')"]
        values: list[Any] = [agent_id]
        if before_created_at is not None and before_id is not None:
            clauses.append("(a.created_at<? OR (a.created_at=? AND a.id<?))")
            values.extend((before_created_at, before_created_at, before_id))
        values.append(max(1, min(limit, 100)))
        with self.database.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT a.*, o.title AS opportunity_title,
                    o.payload_json AS opportunity_payload_json
                FROM automation_actions a
                LEFT JOIN automation_opportunities o ON o.id=a.opportunity_id
                WHERE {' AND '.join(clauses)}
                ORDER BY a.created_at DESC, a.id DESC LIMIT ?
                """,
                values,
            ).fetchall()
        output: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["payload"] = _decode(item.pop("payload_json", None), {})
            item["receipt"] = _decode(item.pop("receipt_json", None), {})
            item["opportunity_payload"] = _decode(
                item.pop("opportunity_payload_json", None), {}
            )
            output.append(item)
        return output

    def recent_events(self, agent_id: int, *, limit: int = 20) -> list[dict[str, Any]]:
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM automation_agent_events WHERE agent_id=?
                ORDER BY created_at DESC, id DESC LIMIT ?
                """,
                (agent_id, max(1, min(limit, 100))),
            ).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            item["payload"] = _decode(item.pop("payload_json", None), {})
            output.append(item)
        return output

    def pending_metric_actions(
        self, window_hours: int, *, limit: int = 100, now: str | None = None
    ) -> list[dict[str, Any]]:
        if window_hours not in {24, 72}:
            raise ValueError("Metric window must be 24 or 72 hours")
        timestamp = datetime.fromisoformat((now or utc_now()).replace("Z", "+00:00"))
        before = (timestamp - timedelta(hours=window_hours)).isoformat()
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT a.* FROM automation_actions a
                LEFT JOIN automation_action_metrics m
                    ON m.action_id=a.id AND m.window_hours=?
                WHERE a.agent_id IS NOT NULL AND a.status='succeeded'
                    AND a.finished_at<=? AND a.external_id IS NOT NULL
                    AND m.action_id IS NULL
                ORDER BY a.finished_at, a.id LIMIT ?
                """,
                (window_hours, before, max(1, min(limit, 500))),
            ).fetchall()
        return [dict(row) for row in rows]

    def save_action_metrics(
        self,
        action_id: int,
        window_hours: int,
        metrics: Mapping[str, Any],
        *,
        now: str | None = None,
    ) -> None:
        timestamp = now or utc_now()
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO automation_action_metrics(
                    action_id, window_hours, metrics_json, captured_at
                ) VALUES(?, ?, ?, ?)
                ON CONFLICT(action_id, window_hours) DO UPDATE SET
                    metrics_json=excluded.metrics_json,
                    captured_at=excluded.captured_at
                """,
                (action_id, window_hours, _json(dict(metrics)), timestamp),
            )

    def metric_summary(self, agent_id: int) -> dict[str, int]:
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT m.metrics_json FROM automation_action_metrics m
                JOIN automation_actions a ON a.id=m.action_id
                WHERE a.agent_id=? AND m.window_hours=72
                """,
                (agent_id,),
            ).fetchall()
            follower_rows = connection.execute(
                """
                SELECT followers_count FROM automation_agent_metric_snapshots
                WHERE agent_id=? AND followers_count IS NOT NULL
                ORDER BY captured_at
                """,
                (agent_id,),
            ).fetchall()
        summary = {"replies": 0, "likes": 0, "reposts": 0, "quotes": 0}
        for row in rows:
            metrics = _decode(row["metrics_json"], {})
            for key in summary:
                summary[key] += int(metrics.get(key) or 0)
        summary["high_quality_replies"] = summary["replies"]
        summary["follower_change"] = (
            int(follower_rows[-1]["followers_count"])
            - int(follower_rows[0]["followers_count"])
            if len(follower_rows) >= 2
            else 0
        )
        return summary

    def save_metric_snapshot(
        self,
        agent_id: int,
        *,
        followers_count: int | None,
        metrics: Mapping[str, Any] | None = None,
        now: str | None = None,
    ) -> None:
        timestamp = now or utc_now()
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO automation_agent_metric_snapshots(
                    agent_id, captured_at, followers_count, metrics_json
                ) VALUES(?, ?, ?, ?)
                ON CONFLICT(agent_id, captured_at) DO UPDATE SET
                    followers_count=excluded.followers_count,
                    metrics_json=excluded.metrics_json
                """,
                (agent_id, timestamp, followers_count, _json(dict(metrics or {}))),
            )

    def _day_start(self) -> str:
        local = datetime.now(ZoneInfo("Asia/Shanghai"))
        return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(
            timezone.utc
        ).isoformat()

    def _sync_quotas(self, agent_id: int, config: Mapping[str, Any]) -> None:
        agent = self.get_agent(agent_id)
        if not agent:
            return
        self.automation.upsert_account_quota(
            int(agent["reply_connection_id"]),
            "comment",
            hourly_limit=int(config["reply_hourly_limit"]),
            daily_limit=int(config["reply_daily_limit"]),
            cooldown_seconds=int(config["author_cooldown_days"]) * 86400,
        )
        posts = int(config["original_posts_daily"])
        self.automation.upsert_account_quota(
            int(agent["publish_connection_id"]),
            "owned_post",
            hourly_limit=posts,
            daily_limit=posts,
            cooldown_seconds=0,
        )


__all__ = [
    "AgentConfig",
    "AgentStatus",
    "AgentStore",
    "DEFAULT_AGENT_CONFIG",
    "RuntimeAgentConfig",
    "agent_runtime_config_path",
]
