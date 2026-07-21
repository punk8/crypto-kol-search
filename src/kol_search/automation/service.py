from __future__ import annotations

import hashlib
import logging
import socket
import threading
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping

from apscheduler.schedulers.background import BackgroundScheduler

from kol_search.automation.models import ActionStatus, CoreJobType
from kol_search.automation.policy import (
    ActionType,
    AutomationPolicy,
    PolicyContext,
    PolicyDecision,
    PolicyLimits,
    PolicyOutcome,
)
from kol_search.automation.repository import AutomationStore
from kol_search.discovery.promotion import KolStatus
from kol_search.platforms import (
    ActionExecutionResult,
    PlatformCapability,
    PlatformOutcomeUpdate,
    PlatformOpportunityProposal,
    PlatformProjectionResult,
    PlatformRegistry,
    PlatformSuggestedAction,
    PlatformTaskName,
    PlatformTaskResult,
)
from kol_search.settings import Settings


logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _key(*values: object) -> str:
    material = "\x1f".join(str(value) for value in values)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _policy_pause_flags(effective: Mapping[str, Any]) -> dict[str, bool]:
    """Preserve the blocking scope so policy decisions remain auditable."""

    scopes = {
        str(row.get("scope") or "")
        for row in effective.get("blocked_by", ())
        if isinstance(row, Mapping)
    }
    return {
        "global_paused": "global" in scopes,
        "platform_paused": "platform" in scopes,
        "account_paused": "account" in scopes,
        "action_type_paused": "action_type" in scopes,
    }


class PlatformAutomationService:
    """Orchestrates registered platforms without interpreting their native schemas globally."""

    def __init__(
        self,
        automation: AutomationStore,
        registry: PlatformRegistry,
        settings: Settings,
    ) -> None:
        self.automation = automation
        self.registry = registry
        self.settings = settings
        self.policy = AutomationPolicy(
            PolicyLimits(
                comment_auto_score=settings.auto_comment_score,
                dm_followup_auto_score=settings.auto_dm_followup_score,
                owned_post_auto_score=settings.auto_publish_score,
                comment_hourly_limit=settings.comment_hourly_limit,
                comment_daily_limit=settings.comment_daily_limit,
                dm_hourly_limit=settings.dm_hourly_limit,
                dm_daily_limit=settings.dm_daily_limit,
                owned_post_daily_limit=settings.publish_daily_limit,
            )
        )

    def initialize_connections(self) -> None:
        """Initialize shared brand defaults and registered read connections."""

        self.initialize_defaults()

        health_by_platform = {
            result.platform_id: result for result in self.registry.health()
        }
        for manifest in self.registry.list_manifests():
            health = health_by_platform.get(manifest.platform_id)
            ready = bool(health and health.ready)
            detail = health.detail if health else "health check is not configured"
            read_capabilities = {
                PlatformCapability.ACCOUNT_SEARCH,
                PlatformCapability.CONTENT_SEARCH,
                PlatformCapability.TIMELINE_FEED,
                PlatformCapability.RELATIONS,
                PlatformCapability.NATIVE_TRENDS,
                PlatformCapability.ANALYTICS,
            }
            self.automation.upsert_platform_connection(
                manifest.platform_id,
                connection_key="reader",
                display_name=f"{manifest.name} reader",
                status="connected" if ready else "disconnected",
                capabilities=[
                    capability.value
                    for capability in manifest.capabilities
                    if capability in read_capabilities
                ],
                metadata={"kind": "reader", "detail": detail or ""},
            )

    def initialize_defaults(self) -> None:
        """Initialize deterministic database defaults without platform I/O."""

        if self.settings.global_kill_switch:
            self.automation.set_kill_switch(
                "global",
                True,
                reason="KOL_GLOBAL_KILL_SWITCH is enabled",
                updated_by="configuration",
            )
        if self.automation.get_brand_config().get("updated_at") is None:
            self.automation.save_brand_config(
                brand_name="",
                description="",
                audience="",
                tone="professional, concise, conversational",
                product_url="",
                platform_handles={},
                allowed_claims=[],
                forbidden_terms=[],
                approved_domains=self.settings.product_domain_allowlist(),
            )


    def refresh_health(self) -> list[dict[str, Any]]:
        self.initialize_connections()
        output: list[dict[str, Any]] = []
        by_platform = {
            row["platform_id"]: row
            for row in self.automation.list_platform_connections()
            if row["connection_key"] == "reader"
        }
        for result in self.registry.health():
            connection = by_platform.get(result.platform_id)
            if connection:
                self.automation.record_connection_health(
                    int(connection["id"]),
                    "connected" if result.ready else "disconnected",
                    error=result.detail,
                )
            output.append(
                {
                    "platform_id": result.platform_id,
                    "ready": result.ready,
                    "account": result.account,
                    "detail": result.detail,
                }
            )
        return output

    def register_connection(
        self,
        platform_id: str,
        *,
        connection_key: str,
        display_name: str,
        capabilities: Iterable[PlatformCapability | str],
        metadata: Mapping[str, Any],
        status: str = "connected",
    ) -> int:
        """Register a governed writer connection in the shared core."""

        manifest = self.registry.require(platform_id).manifest
        normalized = tuple(
            dict.fromkeys(PlatformCapability(value).value for value in capabilities)
        )
        if not normalized:
            raise ValueError("A managed connection requires at least one capability")
        unsupported = [
            value for value in normalized if not manifest.supports(PlatformCapability(value))
        ]
        if unsupported:
            raise ValueError(
                f"Platform {platform_id} does not declare: {', '.join(unsupported)}"
            )
        write_capabilities = {
            PlatformCapability.COMMENT.value,
            PlatformCapability.DM.value,
            PlatformCapability.OWNED_PUBLISH.value,
        }
        if not set(normalized) <= write_capabilities:
            raise ValueError("Managed connections may only declare write capabilities")
        connection_id = self.automation.upsert_platform_connection(
            platform_id,
            connection_key=connection_key,
            display_name=display_name,
            status=status,
            capabilities=normalized,
            metadata=dict(metadata),
        )
        self._configure_connection_quotas(
            connection_id,
            manifest.safety_limits,
            normalized,
        )
        return connection_id

    def _configure_connection_quotas(
        self,
        connection_id: int,
        limits: Mapping[str, int | float],
        capabilities: Iterable[str],
    ) -> None:
        values = set(capabilities)
        cooldown = int(
            limits.get("author_cooldown_days", self.settings.author_cooldown_days)
        ) * 86400
        if PlatformCapability.COMMENT.value in values:
            self.automation.upsert_account_quota(
                connection_id,
                ActionType.COMMENT,
                hourly_limit=int(
                    limits.get("comment_hourly", self.settings.comment_hourly_limit)
                ),
                daily_limit=int(
                    limits.get("comment_daily", self.settings.comment_daily_limit)
                ),
                cooldown_seconds=cooldown,
            )
        if PlatformCapability.DM.value in values:
            self.automation.upsert_account_quota(
                connection_id,
                ActionType.DM,
                hourly_limit=int(limits.get("dm_hourly", self.settings.dm_hourly_limit)),
                daily_limit=int(limits.get("dm_daily", self.settings.dm_daily_limit)),
                cooldown_seconds=cooldown,
            )
        if PlatformCapability.OWNED_PUBLISH.value in values:
            daily_limit = int(
                limits.get("owned_publish_daily", self.settings.publish_daily_limit)
            )
            self.automation.upsert_account_quota(
                connection_id,
                ActionType.OWNED_POST,
                hourly_limit=daily_limit,
                daily_limit=daily_limit,
            )

    def enqueue_discovery(
        self,
        platform_id: str,
        *,
        query: str | None = None,
        limit: int = 20,
        manual: bool = True,
        source: str | None = None,
    ) -> int:
        self.registry.require(platform_id)
        if not self.registry.has_handler(platform_id, PlatformTaskName.DISCOVER):
            raise ValueError(f"Platform {platform_id} does not support discovery")
        payload: dict[str, Any] = {"limit": max(1, min(limit, 100))}
        normalized_source = (source or "").strip().lower()
        if normalized_source and normalized_source not in {
            "accounts",
            "content",
            "relations",
        }:
            raise ValueError(f"Unknown discovery source: {source}")
        required_capability = {
            "accounts": PlatformCapability.ACCOUNT_SEARCH,
            "content": PlatformCapability.CONTENT_SEARCH,
            "relations": PlatformCapability.RELATIONS,
        }.get(normalized_source)
        if required_capability and not self.registry.supports(
            platform_id, required_capability
        ):
            raise ValueError(
                f"Platform {platform_id} does not support "
                f"{required_capability.value} discovery"
            )
        if normalized_source:
            payload["source"] = normalized_source
        if query and query.strip() and normalized_source != "relations":
            payload["query"] = query.strip()
        if manual and normalized_source != "relations" and not payload.get("query"):
            raise ValueError("Manual discovery requires a query")
        bucket = (
            _now().strftime("%Y%m%d%H%M")
            if manual
            else str(
                int(_now().timestamp())
                // max(3600, self.settings.discovery_interval_hours * 3600)
            )
        )
        return self.automation.create_job(
            CoreJobType.MANUAL_DISCOVERY if manual else CoreJobType.DISCOVERY_REFRESH,
            platform_id,
            priority=80 if manual else 50,
            payload=payload,
            idempotency_key=_key(
                "discover",
                platform_id,
                normalized_source or "automatic",
                query or "refresh",
                bucket,
            ),
        )

    def enqueue_signal_refresh(self, platform_id: str, *, manual: bool = False) -> int:
        plugin = self.registry.require(platform_id)
        if not self.registry.has_handler(platform_id, PlatformTaskName.SCAN_SIGNALS):
            raise ValueError(f"Platform {platform_id} does not support signal scanning")
        if not self.registry.has_handler(
            platform_id, PlatformTaskName.BUILD_OPPORTUNITIES
        ):
            raise ValueError(
                f"Platform {platform_id} does not support opportunity building"
            )
        bucket = (
            _now().strftime("%Y%m%d%H%M")
            if manual
            else str(
                int(_now().timestamp())
                // max(60, plugin.manifest.default_scan_interval_seconds)
            )
        )
        return self.automation.create_job(
            CoreJobType.SIGNAL_REFRESH,
            platform_id,
            priority=70 if manual else 50,
            payload={
                "limit": self.settings.signal_posts_per_account,
                "include_trends": True,
                "include_comments": True,
                "comment_limit": self.settings.signal_reply_limit,
                "comment_threads_limit": 3,
            },
            idempotency_key=_key("signals", platform_id, bucket),
        )

    def enqueue_action_dispatch(self, platform_id: str) -> int:
        plugin = self.registry.require(platform_id)
        write_capabilities = {
            PlatformCapability.COMMENT,
            PlatformCapability.DM,
            PlatformCapability.OWNED_PUBLISH,
        }
        if not plugin.manifest.capabilities & write_capabilities:
            raise ValueError(
                f"Platform {platform_id} does not declare an executable action capability"
            )
        if not self.registry.has_handler(platform_id, PlatformTaskName.EXECUTE_ACTION):
            raise ValueError(f"Platform {platform_id} does not support action execution")
        bucket_seconds = max(10, self.settings.action_dispatch_seconds)
        bucket = int(_now().timestamp()) // bucket_seconds
        return self.automation.create_job(
            CoreJobType.DISPATCH_ACTIONS,
            platform_id,
            priority=90,
            payload={"limit": 20},
            idempotency_key=_key("dispatch", platform_id, bucket),
        )

    def enqueue_outcome_refresh(self, platform_id: str) -> int:
        self.registry.require(platform_id)
        if not self.registry.has_handler(platform_id, PlatformTaskName.REFRESH_OUTCOMES):
            raise ValueError(f"Platform {platform_id} does not support outcome refresh")
        bucket = _now().strftime("%Y%m%d%H")
        return self.automation.create_job(
            CoreJobType.REFRESH_OUTCOMES,
            platform_id,
            priority=40,
            idempotency_key=_key("outcomes", platform_id, bucket),
        )

    def run_job(self, job: dict[str, Any]) -> dict[str, Any]:
        job_id = int(job["id"])
        platform_id = str(job["platform_id"])
        payload = dict(job.get("payload") or {})
        job_type: CoreJobType | None = None
        try:
            job_type = CoreJobType(job["job_type"])
            if job_type in {CoreJobType.MANUAL_DISCOVERY, CoreJobType.DISCOVERY_REFRESH}:
                self.automation.update_job_progress(job_id, progress=15, phase="discovering")
                payload = self._discovery_payload(platform_id, payload)
                result = self.registry.dispatch(
                    platform_id,
                    PlatformTaskName.DISCOVER,
                    payload=payload,
                    settings=self.settings,
                )
                assert isinstance(result, PlatformTaskResult)
                if not result.success:
                    raise RuntimeError(result.error or f"{platform_id} discovery failed")
                projection = self._project_discovery(platform_id, result.items, payload)
                projection_warnings = list(projection.warnings)
                output = {
                    **dict(projection.native_counts),
                    "cursor": result.cursor,
                    "warnings": [*result.warnings, *projection_warnings],
                }
                if "next_target_cursor" in payload:
                    output["target_cursor"] = payload.get("next_target_cursor")
                if projection_warnings:
                    output["projection_warnings"] = len(projection_warnings)
            elif job_type == CoreJobType.SIGNAL_REFRESH:
                self.automation.update_job_progress(job_id, progress=15, phase="collecting")
                self.automation.expire_opportunities(platform_id=platform_id)
                payload = self._signal_payload(platform_id, payload)
                result = self.registry.dispatch(
                    platform_id,
                    PlatformTaskName.SCAN_SIGNALS,
                    payload=payload,
                    settings=self.settings,
                )
                assert isinstance(result, PlatformTaskResult)
                if not result.success:
                    raise RuntimeError(result.error or f"{platform_id} signal scan failed")
                built = self.registry.dispatch(
                    platform_id,
                    PlatformTaskName.BUILD_OPPORTUNITIES,
                    payload={**payload, "items": result.items},
                    settings=self.settings,
                )
                assert isinstance(built, PlatformTaskResult)
                if not built.success:
                    raise RuntimeError(
                        built.error or f"{platform_id} opportunity build failed"
                    )
                projection = self._project_signals(
                    platform_id,
                    PlatformProjectionResult(
                        native_counts=dict(built.metadata.get("native_counts") or {}),
                        opportunities=tuple(
                            item
                            for item in built.items
                            if isinstance(item, PlatformOpportunityProposal)
                        ),
                        warnings=built.warnings,
                        metadata=dict(built.metadata),
                    ),
                )
                output = {
                    **projection,
                    "cursor": result.cursor,
                    "target_cursor": payload.get("next_target_cursor"),
                    "warnings": [*result.warnings, *built.warnings],
                }
            elif job_type == CoreJobType.DISPATCH_ACTIONS:
                count = 0
                while count < int(payload.get("limit") or 20) and self.dispatch_action_once(
                    platform_id=platform_id
                ):
                    count += 1
                output = {"actions_dispatched": count}
            elif job_type == CoreJobType.REFRESH_OUTCOMES:
                actions: list[dict[str, Any]] = []
                for action in self.automation.list_actions(
                    platform_id=platform_id,
                    status=ActionStatus.CONFIRMATION_REQUIRED,
                    limit=100,
                ):
                    enriched = dict(action)
                    enriched["connection"] = (
                        self.automation.get_platform_connection(
                            int(action["connection_id"])
                        )
                        if action.get("connection_id")
                        else None
                    ) or {}
                    actions.append(enriched)
                payload["actions"] = actions
                result = self.registry.dispatch(
                    platform_id,
                    PlatformTaskName.REFRESH_OUTCOMES,
                    payload=payload,
                    settings=self.settings,
                )
                assert isinstance(result, PlatformTaskResult)
                if not result.success:
                    raise RuntimeError(result.error or f"{platform_id} outcome refresh failed")
                outcomes_updated = 0
                outcome_warnings = list(result.warnings)
                for item in result.items:
                    if not isinstance(item, PlatformOutcomeUpdate):
                        continue
                    action = self.automation.get_action(item.action_id)
                    if not action or action["platform_id"] != platform_id:
                        raise ValueError(
                            f"{platform_id} returned an outcome for an unrelated action"
                        )
                    if action["status"] != ActionStatus.CONFIRMATION_REQUIRED.value:
                        continue
                    outcome = item.result
                    if outcome.success and not outcome.confirmed:
                        outcome_warnings.append(
                            f"action {item.action_id} remains unconfirmed"
                        )
                        continue
                    receipt = None
                    if outcome.raw_receipt is not None:
                        receipt = (
                            outcome.raw_receipt
                            if isinstance(outcome.raw_receipt, dict)
                            else {"raw": outcome.raw_receipt}
                        )
                    self.automation.record_action_result(
                        item.action_id,
                        success=outcome.success,
                        confirmed=outcome.confirmed,
                        external_id=outcome.external_id,
                        receipt_url=outcome.receipt_url,
                        receipt=receipt,
                        error=outcome.error,
                        actor=f"{platform_id}-outcome-worker",
                    )
                    outcomes_updated += 1
                output = {
                    "items_processed": result.items_processed,
                    "outcomes_updated": outcomes_updated,
                    "warnings": outcome_warnings,
                }
            else:
                raise ValueError(f"Unsupported core job: {job_type.value}")
            if job_type in {
                CoreJobType.MANUAL_DISCOVERY,
                CoreJobType.DISCOVERY_REFRESH,
                CoreJobType.SIGNAL_REFRESH,
            }:
                warnings = [
                    str(value)
                    for value in output.get("warnings", ())
                    if str(value).strip()
                ]
                self._record_reader_health(
                    platform_id,
                    "degraded" if warnings else "connected",
                    "; ".join(warnings[:3]) if warnings else None,
                )
            self.automation.complete_job(job_id, result=output)
            return output
        except Exception as exc:
            if job_type in {
                CoreJobType.MANUAL_DISCOVERY,
                CoreJobType.DISCOVERY_REFRESH,
                CoreJobType.SIGNAL_REFRESH,
            }:
                self._record_reader_health(platform_id, "degraded", str(exc))
            self.automation.fail_job(job_id, str(exc))
            raise

    def _record_reader_health(
        self, platform_id: str, status: str, error: str | None = None
    ) -> None:
        reader = next(
            (
                row
                for row in self.automation.list_platform_connections(
                    platform_id=platform_id
                )
                if row["connection_key"] == "reader"
            ),
            None,
        )
        if reader:
            self.automation.record_connection_health(
                int(reader["id"]), status, error=error
            )

    def dispatch_action_once(self, *, platform_id: str | None = None) -> bool:
        # The live-write switch gates both automatically approved and manually
        # approved actions. Leaving them scheduled makes enabling the switch a
        # deliberate, recoverable operation instead of recording a false failure.
        if not self.settings.live_write_enabled:
            return False
        action = self.automation.claim_next_action(
            "platform-action-worker", platform_id=platform_id
        )
        if not action:
            return False
        action_id = int(action["id"])
        opportunity = (
            self.automation.get_opportunity(int(action["opportunity_id"]))
            if action.get("opportunity_id")
            else None
        )
        connection = (
            self.automation.get_platform_connection(int(action["connection_id"]))
            if action.get("connection_id")
            else None
        )
        preflight_error = self._dispatch_preflight(action, opportunity, connection)
        if preflight_error:
            self.automation.record_action_result(
                action_id,
                success=False,
                confirmed=False,
                error=preflight_error,
                actor="policy",
            )
            return True

        try:
            result = self.registry.dispatch(
                action["platform_id"],
                PlatformTaskName.EXECUTE_ACTION,
                payload={
                    "action_type": action["action_type"],
                    "core_action_id": action_id,
                    "native_object_type": action["native_object_type"],
                    "native_object_id": action["native_object_id"],
                    "draft": action.get("final_text") or action.get("draft") or "",
                    "platform_payload": action.get("payload") or {},
                    "opportunity": opportunity or {},
                    "connection": connection or {},
                },
                settings=self.settings,
                task_id=action_id,
            )
            assert isinstance(result, ActionExecutionResult)
            receipt = result.raw_receipt if isinstance(result.raw_receipt, dict) else {
                "raw": result.raw_receipt
            }
            self.automation.record_action_result(
                action_id,
                success=result.success,
                confirmed=result.confirmed,
                external_id=result.external_id,
                receipt_url=result.receipt_url,
                receipt=receipt,
                error=result.error,
                pause_connection=not result.success or not result.confirmed,
                pause_reason=result.error or "unconfirmed platform write",
            )
        except Exception as exc:
            self.automation.record_action_result(
                action_id,
                success=False,
                confirmed=False,
                error=str(exc),
                pause_connection=True,
                pause_reason=str(exc),
            )
        return True

    def approve_action(self, action_id: int, *, actor: str, final_text: str | None = None) -> None:
        action = self.automation.get_action(action_id)
        if not action:
            raise KeyError(action_id)
        if action["status"] != ActionStatus.NEEDS_REVIEW.value:
            raise ValueError("Only needs_review actions can be approved")
        self.automation.transition_action(
            action_id,
            ActionStatus.SCHEDULED,
            expected_status=ActionStatus.NEEDS_REVIEW,
            actor=actor,
            final_text=final_text or action.get("draft") or "",
            approved_by=actor,
        )

    def set_kol_status(self, platform_id: str, native_id: str, status: str) -> None:
        value = KolStatus(status)
        self.registry.resolve_automation_adapter(platform_id).set_kol_status(
            native_id, value.value
        )

    def add_seed(
        self,
        platform_id: str,
        native_id: str,
        *,
        display_name: str | None = None,
    ) -> None:
        self.registry.require(platform_id)
        self.registry.resolve_automation_adapter(platform_id).add_seed(
            native_id, display_name
        )

    def propose_dm(
        self,
        platform_id: str,
        native_account_id: str,
        draft: str,
        *,
        conversation_id: str | None = None,
        has_valid_conversation: bool = False,
    ) -> int:
        """Create a governed DM opportunity; cold outreach always remains review-only."""

        if not self.registry.supports(platform_id, PlatformCapability.DM):
            raise ValueError(f"Platform {platform_id} does not support DM")
        cleaned = draft.strip()
        if not cleaned:
            raise ValueError("DM draft cannot be empty")
        kol = self.registry.resolve_automation_adapter(platform_id).get_kol(
            native_account_id
        )
        if not kol:
            raise KeyError(native_account_id)
        if kol.get("actionable") is False:
            raise ValueError("Platform-native account identifier is unavailable")
        score = max(0.0, min(100.0, float(kol.get("score") or 0) * 100))
        target_url = kol.get("profile_url") or kol.get("url")
        # A caller cannot assert an established conversation without its native
        # identifier. Platform adapters can add a read-side verification later.
        valid_conversation = bool(
            has_valid_conversation
            and conversation_id
            and self.automation.is_valid_conversation(
                platform_id, native_account_id, conversation_id
            )
        )
        target_type = "conversation" if valid_conversation else "account"
        target_id = conversation_id if valid_conversation else native_account_id
        opportunity_id = self.automation.upsert_opportunity(
            platform_id,
            "outreach",
            target_type,
            target_id,
            priority=round(score),
            score=score,
            title=f"DM · {kol.get('display_name') or kol.get('nickname') or native_account_id}",
            evidence=[
                {
                    "kind": "existing_conversation" if valid_conversation else "kol_library",
                    "target_account_id": native_account_id,
                }
            ],
            payload={
                "target_author_id": native_account_id,
                "target_url": target_url,
                "conversation_id": conversation_id if valid_conversation else None,
                "is_initial_dm": not valid_conversation,
                "has_valid_conversation": valid_conversation,
                "draft": cleaned,
            },
            idempotency_key=_key(
                "dm-opportunity", platform_id, target_id, "followup" if valid_conversation else "cold"
            ),
        )
        self._plan_action(
            opportunity_id,
            platform_id=platform_id,
            action_type=ActionType.DM,
            score=score,
            draft=cleaned,
            native_object_type=target_type,
            native_object_id=target_id,
        )
        return opportunity_id

    def set_conversation_validity(
        self,
        platform_id: str,
        native_account_id: str,
        conversation_id: str,
        *,
        valid: bool,
        evidence: str,
        actor: str,
    ) -> None:
        """Record explicit evidence before a DM can be treated as a follow-up."""

        if not self.registry.supports(platform_id, PlatformCapability.DM):
            raise ValueError(f"Platform {platform_id} does not support DM")
        if not self.registry.resolve_automation_adapter(platform_id).get_kol(
            native_account_id
        ):
            raise KeyError(native_account_id)
        note = evidence.strip()
        if valid and not note:
            raise ValueError("Valid conversation evidence cannot be empty")
        self.automation.set_conversation_state(
            platform_id,
            conversation_id,
            native_account_id,
            valid=valid,
            evidence={"kind": "human_verified", "note": note},
            verified_by=actor,
        )

    def platform_kols(self, platform_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        return self.registry.resolve_automation_adapter(platform_id).list_kols(limit=limit)

    def native_summary(self, platform_id: str) -> dict[str, int]:
        return dict(self.registry.resolve_automation_adapter(platform_id).summary())

    def _discovery_payload(self, platform_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        payload["brand"] = self.automation.get_brand_config()
        source = str(payload.get("source") or "")
        if source == "accounts":
            payload.update({"search_accounts": True, "search_content": False})
        elif source == "content":
            payload.update({"search_accounts": False, "search_content": True})
        if source != "relations" and (payload.get("query") or payload.get("queries")):
            return payload
        previous_cursor = self._previous_job_result_value(
            platform_id,
            {CoreJobType.DISCOVERY_REFRESH, CoreJobType.MANUAL_DISCOVERY},
            "target_cursor",
        )
        active, next_cursor = self._scan_target_batch(
            platform_id,
            after=previous_cursor,
            statuses=(KolStatus.SEED.value, KolStatus.ACTIVE.value),
        )
        payload["seed_accounts"] = active
        payload["next_target_cursor"] = next_cursor
        if source != "relations" and not payload.get("query"):
            payload["query"] = "crypto"
        return payload

    def _signal_payload(self, platform_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        payload["brand"] = self.automation.get_brand_config()
        previous_target_cursor = self._previous_job_result_value(
            platform_id, {CoreJobType.SIGNAL_REFRESH}, "target_cursor"
        )
        active, next_cursor = self._scan_target_batch(
            platform_id,
            after=previous_target_cursor,
            statuses=(
                KolStatus.SEED.value,
                KolStatus.ACTIVE.value,
                KolStatus.CANDIDATE.value,
                KolStatus.REVIEW.value,
            ),
        )
        payload["account_external_ids"] = [row["id"] for row in active]
        # Preserve the platform-owned scan-target projection for adapters that
        # need a native handle as well as the stable ID (for example OpenCLI X).
        # The core does not interpret any target fields beyond the stable ID.
        payload["account_targets"] = [dict(row) for row in active]
        payload["next_target_cursor"] = next_cursor
        if not payload.get("cursor"):
            previous = self._previous_job_result_value(
                platform_id, {CoreJobType.SIGNAL_REFRESH}, "cursor"
            )
            if previous:
                payload["cursor"] = previous
        return payload

    def _scan_target_batch(
        self,
        platform_id: str,
        *,
        after: str | None,
        statuses: tuple[str, ...],
    ) -> tuple[list[dict[str, Any]], str | None]:
        adapter = self.registry.resolve_automation_adapter(platform_id)
        return adapter.list_scan_targets(
            statuses=statuses,
            after=after,
            limit=max(1, self.settings.platform_account_batch_size),
        )

    def _previous_job_result_value(
        self,
        platform_id: str,
        job_types: set[CoreJobType],
        key: str,
    ) -> str | None:
        allowed = {value.value for value in job_types}
        for row in self.automation.list_jobs(
            platform_id=platform_id,
            status="succeeded",
            job_types=job_types,
            limit=100,
        ):
            result = row.get("result") or {}
            if row["job_type"] not in allowed or key not in result:
                continue
            value = result.get(key)
            return str(value) if value is not None else None
        return None

    def _project_discovery(
        self, platform_id: str, items: tuple[object, ...], payload: dict[str, Any]
    ) -> PlatformProjectionResult:
        adapter = self.registry.resolve_automation_adapter(platform_id)
        return adapter.project_discovery(items, payload=payload)

    def _project_signals(
        self,
        platform_id: str,
        projection: PlatformProjectionResult,
    ) -> dict[str, int]:
        opportunity_count = 0
        for proposal in projection.opportunities:
            native = proposal.native_object
            if native.platform_id != platform_id:
                raise ValueError(
                    f"{platform_id} adapter returned an opportunity for {native.platform_id}"
                )
            evidence = [dict(value) for value in proposal.evidence]
            evidence.extend(
                {"kind": "score_reason", "reason": reason}
                for reason in proposal.score_reasons
            )
            opportunity_id = self.automation.upsert_opportunity(
                platform_id,
                proposal.opportunity_type,
                native.object_type,
                native.object_id,
                priority=proposal.priority,
                score=proposal.score,
                title=proposal.title,
                evidence=evidence,
                payload=dict(proposal.payload),
                expires_at=proposal.expires_at,
            )
            opportunity_count += 1
            stored_opportunity = self.automation.get_opportunity(opportunity_id) or {}
            if stored_opportunity.get("status") not in {"open", "planned"}:
                continue
            for suggested in proposal.suggested_actions:
                if suggested.action_type == PlatformSuggestedAction.OWNED_PUBLISH:
                    self._plan_owned_publish(
                        opportunity_id,
                        platform_id=platform_id,
                        draft=suggested.draft,
                        score=suggested.score,
                        action_payload=dict(suggested.payload),
                    )
                    continue
                action_type = ActionType(suggested.action_type.value)
                self._plan_action(
                    opportunity_id,
                    platform_id=platform_id,
                    action_type=action_type,
                    score=suggested.score,
                    draft=suggested.draft,
                    native_object_type=native.object_type,
                    native_object_id=native.object_id,
                    expires_at=suggested.expires_at or proposal.expires_at,
                    action_payload=dict(suggested.payload),
                )
        output = dict(projection.native_counts)
        output["opportunities"] = opportunity_count
        if projection.warnings:
            output["projection_warnings"] = len(projection.warnings)
        return output

    def _plan_owned_publish(
        self,
        opportunity_id: int,
        *,
        platform_id: str,
        draft: str,
        score: float,
        action_payload: dict[str, Any] | None = None,
    ) -> None:
        opportunity = self.automation.get_opportunity(opportunity_id) or {}
        native_object_id = str(opportunity.get("native_object_id") or "")
        connection = self._select_connection(
            platform_id, PlatformCapability.OWNED_PUBLISH.value
        )
        if not connection:
            return
        decision = self._evaluate_action_policy(
            opportunity_id,
            platform_id=platform_id,
            action_type=ActionType.OWNED_POST,
            score=score,
            draft=draft,
            native_object_id=native_object_id,
            connection=connection,
        )
        if decision.outcome == PolicyOutcome.REJECTED:
            self.automation.record_policy_decision(
                decision.outcome,
                decision.policy_version,
                opportunity_id=opportunity_id,
                score=score,
                rules=list(decision.reasons),
                explanation="; ".join(decision.reasons),
            )
            return
        delivery_payload = dict(action_payload or {})
        self._plan_action(
            opportunity_id,
            platform_id=platform_id,
            action_type=ActionType.OWNED_POST,
            score=score,
            draft=draft,
            native_object_type=opportunity["native_object_type"],
            native_object_id=opportunity["native_object_id"],
            expires_at=opportunity.get("expires_at"),
            connection=connection,
            policy_decision=decision,
            action_payload=delivery_payload,
        )

    def _plan_action(
        self,
        opportunity_id: int,
        *,
        platform_id: str,
        action_type: ActionType,
        score: float,
        draft: str,
        native_object_type: str,
        native_object_id: str,
        expires_at: str | None = None,
        connection: dict[str, Any] | None = None,
        policy_decision: PolicyDecision | None = None,
        action_payload: dict[str, Any] | None = None,
    ) -> int | None:
        capability = {
            ActionType.COMMENT: PlatformCapability.COMMENT.value,
            ActionType.DM: PlatformCapability.DM.value,
            ActionType.OWNED_POST: PlatformCapability.OWNED_PUBLISH.value,
        }[action_type]
        connection = connection or self._select_connection(platform_id, capability)
        if not connection:
            return None
        decision = policy_decision or self._evaluate_action_policy(
            opportunity_id,
            platform_id=platform_id,
            action_type=action_type,
            score=score,
            draft=draft,
            native_object_id=native_object_id,
            expires_at=expires_at,
            connection=connection,
        )
        if decision.outcome == PolicyOutcome.REJECTED:
            self.automation.record_policy_decision(
                decision.outcome,
                decision.policy_version,
                opportunity_id=opportunity_id,
                score=score,
                rules=list(decision.reasons),
                explanation="; ".join(decision.reasons),
            )
            return None
        auto_allowed = (
            decision.outcome == PolicyOutcome.AUTO_EXECUTE
            and self.settings.auto_execution_enabled
            and self.settings.live_write_enabled
        )
        if decision.outcome == PolicyOutcome.AUTO_EXECUTE and not auto_allowed:
            decision = PolicyDecision(
                PolicyOutcome.NEEDS_REVIEW,
                ("automatic writes are disabled by runtime configuration",),
                policy_version=decision.policy_version,
                checks=decision.checks,
            )
        status = ActionStatus.SCHEDULED if auto_allowed else ActionStatus.NEEDS_REVIEW
        action_id = self.automation.create_action(
            platform_id,
            action_type,
            native_object_type,
            native_object_id,
            opportunity_id=opportunity_id,
            connection_id=int(connection["id"]),
            draft=draft,
            payload=action_payload,
            initial_status=status,
            idempotency_key=_key(platform_id, action_type.value, native_object_id, draft),
        )
        self.automation.record_policy_decision(
            decision.outcome,
            decision.policy_version,
            action_id=action_id,
            score=score,
            rules=list(decision.reasons),
            explanation="; ".join(decision.reasons),
        )
        return action_id

    def _evaluate_action_policy(
        self,
        opportunity_id: int,
        *,
        platform_id: str,
        action_type: ActionType,
        score: float,
        draft: str,
        native_object_id: str,
        connection: dict[str, Any],
        expires_at: str | None = None,
    ) -> PolicyDecision:
        effective = self.automation.get_effective_channel_state(
            platform_id,
            connection_id=int(connection["id"]),
            action_type=action_type,
        )
        now = _now()
        hourly_usage = self.automation.count_action_usage(
            int(connection["id"]),
            action_type,
            since=(now - timedelta(hours=1)).isoformat(),
        )
        daily_usage = self.automation.count_action_usage(
            int(connection["id"]),
            action_type,
            since=(now - timedelta(days=1)).isoformat(),
        )
        duplicate = self._existing_action(
            platform_id,
            action_type,
            native_object_id,
            draft=draft,
        )
        opportunity = self.automation.get_opportunity(opportunity_id) or {}
        target_author_id = str(
            ((opportunity.get("payload") or {}).get("target_author_id") or "")
        )
        opportunity_payload = opportunity.get("payload") or {}
        return self._policy_for_connection(
            int(connection["id"]), action_type
        ).evaluate(
            PolicyContext(
                platform_id=platform_id,
                action_type=action_type,
                score=score,
                text=draft,
                capability_available=self.registry.supports(
                    platform_id,
                    PlatformCapability.OWNED_PUBLISH
                    if action_type == ActionType.OWNED_POST
                    else PlatformCapability(action_type.value),
                ),
                account_status="active" if connection["status"] == "connected" else "paused",
                is_initial_dm=bool(opportunity_payload.get("is_initial_dm", action_type == ActionType.DM)),
                has_valid_conversation=bool(
                    opportunity_payload.get("has_valid_conversation", False)
                ),
                expires_at=expires_at,
                **_policy_pause_flags(effective),
                duplicate=duplicate,
                author_in_cooldown=self._author_in_cooldown(
                    platform_id,
                    int(connection["id"]),
                    action_type,
                    target_author_id,
                ),
                hourly_usage=hourly_usage,
                daily_usage=daily_usage,
                forbidden_terms=self._forbidden_terms(),
                approved_link_domains=self._approved_domains(),
            )
        )

    def _existing_action(
        self,
        platform_id: str,
        action_type: ActionType,
        native_object_id: str,
        *,
        draft: str,
        exclude_action_id: int | None = None,
    ) -> bool:
        # Failed and confirmation-required writes deliberately remain blocking.
        # Only a different, confirmed DM in a verified conversation may proceed
        # after cooldown; SQL checks the complete history, not a display page.
        return self.automation.has_existing_action(
            platform_id,
            action_type,
            native_object_id,
            draft=draft,
            exclude_action_id=exclude_action_id,
        )

    def _author_in_cooldown(
        self,
        platform_id: str,
        connection_id: int,
        action_type: ActionType,
        target_author_id: str,
    ) -> bool:
        quota = self.automation.get_account_quota(connection_id, action_type) or {}
        cooldown_seconds = int(
            quota.get("cooldown_seconds")
            if quota.get("cooldown_seconds") is not None
            else self.settings.author_cooldown_days * 86400
        )
        if not target_author_id or cooldown_seconds <= 0:
            return False
        cutoff = _now() - timedelta(seconds=cooldown_seconds)
        return self.automation.has_recent_author_action(
            platform_id,
            connection_id,
            target_author_id,
            since=cutoff.isoformat(),
        )

    def _select_connection(self, platform_id: str, capability: str) -> dict[str, Any] | None:
        required_kind = (
            "managed_account"
            if capability in {
                PlatformCapability.COMMENT.value,
                PlatformCapability.DM.value,
            }
            else None
        )
        candidates = [
            row
            for row in self.automation.list_platform_connections(
                platform_id=platform_id, enabled_only=True
            )
            if row["status"] == "connected"
            and capability in (row.get("capabilities") or [])
            and (
                required_kind is None
                or (row.get("metadata") or {}).get("kind") == required_kind
            )
            and (row.get("metadata") or {}).get("kind") != "reader"
        ]
        action_type = {
            PlatformCapability.COMMENT.value: ActionType.COMMENT,
            PlatformCapability.DM.value: ActionType.DM,
            PlatformCapability.OWNED_PUBLISH.value: ActionType.OWNED_POST,
        }.get(capability)
        if action_type is None:
            candidates.sort(
                key=lambda row: (
                    not bool((row.get("metadata") or {}).get("is_default")),
                    int(row["id"]),
                )
            )
            return candidates[0] if candidates else None

        now = _now()
        hour_since = (now - timedelta(hours=1)).isoformat()
        day_since = (now - timedelta(days=1)).isoformat()
        candidates.sort(
            key=lambda row: (
                self.automation.count_action_usage(
                    int(row["id"]), action_type, since=day_since
                ),
                self.automation.count_action_usage(
                    int(row["id"]), action_type, since=hour_since
                ),
                not bool((row.get("metadata") or {}).get("is_default")),
                int(row["id"]),
            )
        )
        return candidates[0] if candidates else None

    def _dispatch_preflight(
        self,
        action: dict[str, Any],
        opportunity: dict[str, Any] | None,
        connection: dict[str, Any] | None,
    ) -> str | None:
        """Re-run deterministic checks immediately before an external write."""

        if not connection:
            return "managed platform connection is missing"
        if opportunity and opportunity.get("status") in {"dismissed", "expired"}:
            return "opportunity is no longer actionable"
        payload = (opportunity or {}).get("payload") or {}
        expires_at = (opportunity or {}).get("expires_at")
        if expires_at and (_parse_time(expires_at) or datetime.min.replace(tzinfo=timezone.utc)) <= _now():
            return "opportunity expired before execution"
        action_type = ActionType(action["action_type"])
        effective = self.automation.get_effective_channel_state(
            action["platform_id"],
            connection_id=int(connection["id"]),
            action_type=action_type,
        )
        now = _now()
        hour_since = (now - timedelta(hours=1)).isoformat()
        day_since = (now - timedelta(days=1)).isoformat()
        hourly_usage = max(
            0,
            self.automation.count_action_usage(
                int(connection["id"]), action_type, since=hour_since
            )
            - 1,
        )
        daily_usage = max(
            0,
            self.automation.count_action_usage(
                int(connection["id"]), action_type, since=day_since
            )
            - 1,
        )
        final_text = str(action.get("final_text") or action.get("draft") or "")
        decision = self._policy_for_connection(
            int(connection["id"]), action_type
        ).evaluate(
            PolicyContext(
                platform_id=action["platform_id"],
                action_type=action_type,
                score=float((opportunity or {}).get("score") or 0),
                text=final_text,
                capability_available=self.registry.supports(
                    action["platform_id"],
                    PlatformCapability.OWNED_PUBLISH
                    if action_type == ActionType.OWNED_POST
                    else PlatformCapability(action_type.value),
                ),
                account_status=(
                    "active" if connection.get("status") == "connected" else "paused"
                ),
                is_initial_dm=bool(payload.get("is_initial_dm", True)),
                has_valid_conversation=bool(payload.get("has_valid_conversation", False)),
                expires_at=expires_at,
                **_policy_pause_flags(effective),
                duplicate=self._existing_action(
                    action["platform_id"],
                    action_type,
                    action["native_object_id"],
                    draft=final_text,
                    exclude_action_id=int(action["id"]),
                ),
                author_in_cooldown=self._author_in_cooldown(
                    action["platform_id"],
                    int(connection["id"]),
                    action_type,
                    str(payload.get("target_author_id") or ""),
                ),
                hourly_usage=hourly_usage,
                daily_usage=daily_usage,
                forbidden_terms=self._forbidden_terms(),
                approved_link_domains=self._approved_domains(),
            )
        )
        if decision.outcome == PolicyOutcome.REJECTED:
            return "; ".join(decision.reasons)
        return None

    def _policy_for_connection(
        self, connection_id: int, action_type: ActionType
    ) -> AutomationPolicy:
        quota = self.automation.get_account_quota(connection_id, action_type)
        if not quota:
            return self.policy
        limits = self.policy.limits
        if action_type == ActionType.COMMENT:
            limits = replace(
                limits,
                comment_hourly_limit=int(quota["hourly_limit"]),
                comment_daily_limit=int(quota["daily_limit"]),
            )
        elif action_type == ActionType.DM:
            limits = replace(
                limits,
                dm_hourly_limit=int(quota["hourly_limit"]),
                dm_daily_limit=int(quota["daily_limit"]),
            )
        else:
            limits = replace(
                limits,
                owned_post_daily_limit=int(quota["daily_limit"]),
            )
        return AutomationPolicy(limits)

    def _forbidden_terms(self) -> tuple[str, ...]:
        return tuple(self.automation.get_brand_config().get("forbidden_terms") or ())

    def _approved_domains(self) -> tuple[str, ...]:
        configured = set(self.settings.product_domain_allowlist())
        configured.update(self.automation.get_brand_config().get("approved_domains") or ())
        return tuple(sorted(configured))

class AutomationWorker:
    """Single-process worker for shared core jobs and scheduled actions."""

    def __init__(self, automation: AutomationStore, service: PlatformAutomationService) -> None:
        self.automation = automation
        self.service = service
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._heartbeat_thread: threading.Thread | None = None
        self._started_at: str | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        stale_before = (_now() - timedelta(minutes=15)).isoformat()
        self.automation.recover_stale_jobs(before=stale_before)
        self.automation.recover_stale_actions(
            before=stale_before
        )
        self._stop.clear()
        self._started_at = _now().isoformat()
        self._heartbeat("online")
        self._thread = threading.Thread(
            target=self._loop, name="platform-automation-worker", daemon=True
        )
        self._thread.start()
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop,
            name="platform-worker-heartbeat",
            daemon=True,
        )
        self._heartbeat_thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=5)
        if self._heartbeat_thread:
            self._heartbeat_thread.join(timeout=5)
        self._heartbeat("offline")

    def notify(self) -> None:
        self._wake.set()

    def run_once(self) -> bool:
        job = self.automation.claim_next_job("platform-job-worker")
        if job:
            self.service.run_job(job)
            return True
        if self.service.dispatch_action_once():
            return True
        return False

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                worked = self.run_once()
            except Exception:
                logger.exception("automation worker iteration failed")
                worked = True
            if not worked:
                self._wake.wait(1)
                self._wake.clear()

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(30):
            try:
                self._heartbeat("online")
            except Exception:
                logger.exception("worker heartbeat failed")

    def _heartbeat(self, status: str) -> None:
        self.automation.record_worker_heartbeat(
            self.service.settings.worker_id,
            host_label=socket.gethostname(),
            version="1.0.0",
            status=status,
            started_at=self._started_at,
        )


class PlatformAutomationScheduler:
    """Schedule each registered platform independently using shared core job types."""

    def __init__(
        self,
        service: PlatformAutomationService,
        worker: AutomationWorker,
        settings: Settings,
    ) -> None:
        self.service = service
        self.worker = worker
        self.settings = settings
        self.scheduler = BackgroundScheduler(timezone=settings.timezone)

    def start(self) -> None:
        for manifest in self.service.registry.list_manifests():
            platform_id = manifest.platform_id
            if self.service.registry.has_handler(platform_id, PlatformTaskName.DISCOVER):
                self.scheduler.add_job(
                    self._enqueue_discovery,
                    "interval",
                    hours=max(1, self.settings.discovery_interval_hours),
                    args=(platform_id,),
                    id=f"{platform_id}-discovery-refresh",
                    replace_existing=True,
                    coalesce=True,
                    max_instances=1,
                )
            if (
                self.settings.enable_signal_scan
                and self.service.registry.has_handler(
                    platform_id, PlatformTaskName.SCAN_SIGNALS
                )
                and self.service.registry.has_handler(
                    platform_id, PlatformTaskName.BUILD_OPPORTUNITIES
                )
            ):
                self.scheduler.add_job(
                    self._enqueue_signals,
                    "interval",
                    seconds=max(60, manifest.default_scan_interval_seconds),
                    args=(platform_id,),
                    id=f"{platform_id}-signal-refresh",
                    replace_existing=True,
                    coalesce=True,
                    max_instances=1,
                )
            write_capabilities = {
                PlatformCapability.COMMENT,
                PlatformCapability.DM,
                PlatformCapability.OWNED_PUBLISH,
            }
            if manifest.capabilities & write_capabilities and self.service.registry.has_handler(
                platform_id, PlatformTaskName.EXECUTE_ACTION
            ):
                self.scheduler.add_job(
                    self._enqueue_dispatch,
                    "interval",
                    seconds=max(10, self.settings.action_dispatch_seconds),
                    args=(platform_id,),
                    id=f"{platform_id}-action-dispatch",
                    replace_existing=True,
                    coalesce=True,
                    max_instances=1,
                )
            if self.service.registry.has_handler(
                platform_id, PlatformTaskName.REFRESH_OUTCOMES
            ):
                self.scheduler.add_job(
                    self._enqueue_outcomes,
                    "interval",
                    hours=1,
                    args=(platform_id,),
                    id=f"{platform_id}-outcome-refresh",
                    replace_existing=True,
                    coalesce=True,
                    max_instances=1,
                )
        if self.service.registry.list_manifests():
            self.scheduler.start()

    def stop(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)

    def _enqueue_discovery(self, platform_id: str) -> None:
        self.service.enqueue_discovery(platform_id, manual=False)
        self.worker.notify()

    def _enqueue_signals(self, platform_id: str) -> None:
        self.service.enqueue_signal_refresh(platform_id)
        self.worker.notify()

    def _enqueue_dispatch(self, platform_id: str) -> None:
        self.service.enqueue_action_dispatch(platform_id)
        self.worker.notify()

    def _enqueue_outcomes(self, platform_id: str) -> None:
        self.service.enqueue_outcome_refresh(platform_id)
        self.worker.notify()
