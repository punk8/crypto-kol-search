from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str


MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        1,
        "platform_connections_and_jobs",
        """
        CREATE TABLE automation_platform_connections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            platform_id TEXT NOT NULL,
            connection_key TEXT NOT NULL DEFAULT 'default',
            display_name TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'disconnected'
                CHECK(status IN ('connected', 'degraded', 'disconnected', 'paused')),
            enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1)),
            capabilities_json TEXT NOT NULL DEFAULT '[]',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            consecutive_failures INTEGER NOT NULL DEFAULT 0 CHECK(consecutive_failures >= 0),
            last_health_at TEXT,
            last_health_error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(platform_id, connection_key)
        );
        CREATE INDEX idx_automation_connections_platform
            ON automation_platform_connections(platform_id, enabled, status);

        CREATE TABLE automation_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_type TEXT NOT NULL CHECK(job_type IN (
                'manual_discovery', 'discovery_refresh', 'signal_refresh',
                'dispatch_actions', 'refresh_outcomes'
            )),
            platform_id TEXT NOT NULL,
            connection_id INTEGER,
            status TEXT NOT NULL DEFAULT 'queued'
                CHECK(status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')),
            priority INTEGER NOT NULL DEFAULT 50 CHECK(priority BETWEEN 0 AND 100),
            progress INTEGER NOT NULL DEFAULT 0 CHECK(progress BETWEEN 0 AND 100),
            phase TEXT NOT NULL DEFAULT 'queued',
            payload_json TEXT NOT NULL DEFAULT '{}',
            result_json TEXT NOT NULL DEFAULT '{}',
            idempotency_key TEXT UNIQUE,
            attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
            available_at TEXT NOT NULL,
            locked_by TEXT,
            locked_at TEXT,
            started_at TEXT,
            finished_at TEXT,
            error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(connection_id) REFERENCES automation_platform_connections(id)
                ON DELETE SET NULL
        );
        CREATE INDEX idx_automation_jobs_queue
            ON automation_jobs(status, available_at, priority DESC, id);
        CREATE INDEX idx_automation_jobs_platform
            ON automation_jobs(platform_id, status, created_at DESC);
        """,
    ),
    Migration(
        2,
        "opportunities_actions_and_policy",
        """
        CREATE TABLE automation_opportunities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            platform_id TEXT NOT NULL,
            opportunity_type TEXT NOT NULL,
            native_object_type TEXT NOT NULL,
            native_object_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open'
                CHECK(status IN ('open', 'planned', 'dismissed', 'expired')),
            priority INTEGER NOT NULL DEFAULT 50 CHECK(priority BETWEEN 0 AND 100),
            score REAL NOT NULL DEFAULT 0 CHECK(score BETWEEN 0 AND 100),
            title TEXT NOT NULL DEFAULT '',
            evidence_json TEXT NOT NULL DEFAULT '[]',
            payload_json TEXT NOT NULL DEFAULT '{}',
            idempotency_key TEXT NOT NULL UNIQUE,
            expires_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX idx_automation_opportunities_queue
            ON automation_opportunities(platform_id, status, priority DESC, score DESC, id);
        CREATE INDEX idx_automation_opportunities_native
            ON automation_opportunities(platform_id, native_object_type, native_object_id);

        CREATE TABLE automation_actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            opportunity_id INTEGER,
            platform_id TEXT NOT NULL,
            connection_id INTEGER,
            action_type TEXT NOT NULL
                CHECK(action_type IN ('comment', 'dm', 'owned_post')),
            native_object_type TEXT NOT NULL,
            native_object_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'proposed' CHECK(status IN (
                'proposed', 'needs_review', 'scheduled', 'executing', 'succeeded',
                'confirmation_required', 'failed', 'cancelled'
            )),
            draft TEXT NOT NULL DEFAULT '',
            final_text TEXT,
            idempotency_key TEXT NOT NULL UNIQUE,
            scheduled_at TEXT,
            approved_by TEXT,
            approved_at TEXT,
            locked_by TEXT,
            locked_at TEXT,
            started_at TEXT,
            finished_at TEXT,
            external_id TEXT,
            receipt_url TEXT,
            receipt_json TEXT NOT NULL DEFAULT '{}',
            error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(opportunity_id) REFERENCES automation_opportunities(id)
                ON DELETE SET NULL,
            FOREIGN KEY(connection_id) REFERENCES automation_platform_connections(id)
                ON DELETE SET NULL
        );
        CREATE INDEX idx_automation_actions_queue
            ON automation_actions(platform_id, status, scheduled_at, id);
        CREATE INDEX idx_automation_actions_limits
            ON automation_actions(connection_id, action_type, finished_at);

        CREATE TABLE automation_policy_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            opportunity_id INTEGER,
            action_id INTEGER,
            outcome TEXT NOT NULL
                CHECK(outcome IN ('auto_execute', 'needs_review', 'rejected')),
            policy_version TEXT NOT NULL,
            score REAL CHECK(score IS NULL OR score BETWEEN 0 AND 100),
            rules_json TEXT NOT NULL DEFAULT '[]',
            explanation TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            CHECK(
                (opportunity_id IS NOT NULL AND action_id IS NULL) OR
                (opportunity_id IS NULL AND action_id IS NOT NULL)
            ),
            FOREIGN KEY(opportunity_id) REFERENCES automation_opportunities(id)
                ON DELETE CASCADE,
            FOREIGN KEY(action_id) REFERENCES automation_actions(id)
                ON DELETE CASCADE
        );
        CREATE INDEX idx_automation_policy_opportunity
            ON automation_policy_decisions(opportunity_id, created_at DESC);
        CREATE INDEX idx_automation_policy_action
            ON automation_policy_decisions(action_id, created_at DESC);
        """,
    ),
    Migration(
        3,
        "channel_controls_and_audit",
        """
        CREATE TABLE automation_channel_controls (
            control_key TEXT PRIMARY KEY,
            scope TEXT NOT NULL
                CHECK(scope IN ('global', 'platform', 'account', 'action_type')),
            platform_id TEXT,
            connection_id INTEGER,
            action_type TEXT CHECK(
                action_type IS NULL OR action_type IN ('comment', 'dm', 'owned_post')
            ),
            state TEXT NOT NULL DEFAULT 'enabled'
                CHECK(state IN ('enabled', 'paused')),
            reason TEXT NOT NULL DEFAULT '',
            updated_by TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            CHECK(
                (scope = 'global' AND platform_id IS NULL AND connection_id IS NULL AND action_type IS NULL) OR
                (scope = 'platform' AND platform_id IS NOT NULL AND connection_id IS NULL AND action_type IS NULL) OR
                (scope = 'account' AND platform_id IS NOT NULL AND connection_id IS NOT NULL AND action_type IS NULL) OR
                (scope = 'action_type' AND platform_id IS NULL AND connection_id IS NULL AND action_type IS NOT NULL)
            ),
            FOREIGN KEY(connection_id) REFERENCES automation_platform_connections(id)
                ON DELETE CASCADE
        );
        CREATE INDEX idx_automation_controls_lookup
            ON automation_channel_controls(scope, platform_id, connection_id, action_type, state);

        CREATE TABLE automation_audit_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            actor TEXT NOT NULL,
            event_type TEXT NOT NULL,
            object_type TEXT NOT NULL,
            object_id TEXT NOT NULL,
            platform_id TEXT,
            payload_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );
        CREATE INDEX idx_automation_audit_object
            ON automation_audit_events(object_type, object_id, created_at DESC);
        CREATE INDEX idx_automation_audit_platform
            ON automation_audit_events(platform_id, created_at DESC);
        """,
    ),
    Migration(
        4,
        "brand_configuration_and_account_quotas",
        """
        CREATE TABLE automation_brand_config (
            id INTEGER PRIMARY KEY CHECK(id=1),
            brand_name TEXT NOT NULL DEFAULT '',
            description TEXT NOT NULL DEFAULT '',
            audience TEXT NOT NULL DEFAULT '',
            tone TEXT NOT NULL DEFAULT 'professional, concise, conversational',
            product_url TEXT NOT NULL DEFAULT '',
            platform_handles_json TEXT NOT NULL DEFAULT '{}',
            allowed_claims_json TEXT NOT NULL DEFAULT '[]',
            forbidden_terms_json TEXT NOT NULL DEFAULT '[]',
            approved_domains_json TEXT NOT NULL DEFAULT '[]',
            updated_at TEXT NOT NULL
        );

        CREATE TABLE automation_account_quotas (
            connection_id INTEGER NOT NULL,
            action_type TEXT NOT NULL
                CHECK(action_type IN ('comment', 'dm', 'owned_post')),
            hourly_limit INTEGER NOT NULL CHECK(hourly_limit >= 0),
            daily_limit INTEGER NOT NULL CHECK(daily_limit >= 0),
            cooldown_seconds INTEGER NOT NULL DEFAULT 0 CHECK(cooldown_seconds >= 0),
            updated_at TEXT NOT NULL,
            PRIMARY KEY(connection_id, action_type),
            FOREIGN KEY(connection_id) REFERENCES automation_platform_connections(id)
                ON DELETE CASCADE
        );
        """,
    ),
    Migration(
        5,
        "platform_action_payload",
        """
        ALTER TABLE automation_actions
        ADD COLUMN payload_json TEXT NOT NULL DEFAULT '{}';
        """,
    ),
    Migration(
        6,
        "verified_conversation_state",
        """
        CREATE TABLE automation_conversations (
            platform_id TEXT NOT NULL,
            native_conversation_id TEXT NOT NULL,
            native_account_id TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('valid', 'revoked')),
            evidence_json TEXT NOT NULL DEFAULT '{}',
            verified_by TEXT NOT NULL,
            verified_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(platform_id, native_conversation_id)
        );
        CREATE INDEX idx_automation_conversations_account
            ON automation_conversations(platform_id, native_account_id, state);
        """,
    ),
    Migration(
        7,
        "worker_heartbeats",
        """
        CREATE TABLE automation_worker_heartbeats (
            worker_id TEXT PRIMARY KEY,
            host_label TEXT NOT NULL,
            version TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'online'
                CHECK(status IN ('online', 'stopping', 'offline')),
            started_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX idx_automation_worker_heartbeats_seen
            ON automation_worker_heartbeats(last_seen_at DESC);
        """,
    ),
)
