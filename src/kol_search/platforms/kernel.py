from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from threading import RLock
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable


class PlatformCapability(StrEnum):
    """A feature that a platform module can expose to the shared core."""

    ACCOUNT_SEARCH = "account_search"
    CONTENT_SEARCH = "content_search"
    TIMELINE_FEED = "timeline/feed"
    # These aliases make platform-specific terminology ergonomic while preserving
    # one capability in policy and UI code.
    TIMELINE = "timeline/feed"
    FEED = "timeline/feed"
    RELATIONS = "relations"
    NATIVE_TRENDS = "native_trends"
    COMMENT = "comment"
    DM = "dm"
    OWNED_PUBLISH = "owned_publish"
    MEDIA_UPLOAD = "media_upload"
    ANALYTICS = "analytics"

    @classmethod
    def _missing_(cls, value: object) -> PlatformCapability | None:
        if value in {"timeline", "feed", "timeline_feed"}:
            return cls.TIMELINE_FEED
        return None


class PlatformTaskName(StrEnum):
    """Operations implemented inside a platform module."""

    DISCOVER = "discover"
    SCAN_SIGNALS = "scan_signals"
    BUILD_OPPORTUNITIES = "build_opportunities"
    EXECUTE_ACTION = "execute_action"
    REFRESH_OUTCOMES = "refresh_outcomes"


_PLATFORM_ID = re.compile(r"^[a-z][a-z0-9_-]*$")


def _frozen_mapping(values: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
    return MappingProxyType(dict(values or {}))


@dataclass(frozen=True, slots=True)
class PlatformManifest:
    """Static declaration used by scheduling, policy, CLI, and navigation."""

    platform_id: str
    name: str
    version: str
    capabilities: frozenset[PlatformCapability] = field(default_factory=frozenset)
    default_scan_interval_seconds: int = 30 * 60
    safety_limits: Mapping[str, int | float] = field(default_factory=dict)
    workbench_path: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        platform_id = self.platform_id.strip().lower()
        if not _PLATFORM_ID.fullmatch(platform_id):
            raise ValueError(
                "platform_id must start with a letter and contain only "
                "lowercase letters, numbers, underscores, or hyphens"
            )
        if not self.name.strip():
            raise ValueError("Platform name cannot be empty")
        if not self.version.strip():
            raise ValueError("Platform version cannot be empty")
        if self.default_scan_interval_seconds <= 0:
            raise ValueError("default_scan_interval_seconds must be positive")

        capabilities = frozenset(PlatformCapability(item) for item in self.capabilities)
        limits = dict(self.safety_limits)
        for key, value in limits.items():
            if not key or isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError("safety_limits must map non-empty names to numbers")
            if value < 0:
                raise ValueError(f"Safety limit {key!r} cannot be negative")

        object.__setattr__(self, "platform_id", platform_id)
        object.__setattr__(self, "name", self.name.strip())
        object.__setattr__(self, "version", self.version.strip())
        object.__setattr__(self, "capabilities", capabilities)
        object.__setattr__(self, "safety_limits", _frozen_mapping(limits))
        object.__setattr__(self, "metadata", _frozen_mapping(self.metadata))

    @property
    def display_name(self) -> str:
        return self.name

    def supports(self, *capabilities: PlatformCapability | str) -> bool:
        return all(PlatformCapability(value) in self.capabilities for value in capabilities)

    def as_dict(self) -> dict[str, Any]:
        return {
            "platform_id": self.platform_id,
            "name": self.name,
            "version": self.version,
            "capabilities": sorted(item.value for item in self.capabilities),
            "default_scan_interval_seconds": self.default_scan_interval_seconds,
            "safety_limits": dict(self.safety_limits),
            "workbench_path": self.workbench_path,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class PlatformTaskContext:
    """Dependencies and input for one platform operation.

    The shared core deliberately treats payload values and native objects as opaque.
    Platform code owns their validation and interpretation.
    """

    platform_id: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    settings: object | None = None
    services: Mapping[str, object] = field(default_factory=dict)
    task_id: str | int | None = None
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        platform_id = self.platform_id.strip().lower()
        if not _PLATFORM_ID.fullmatch(platform_id):
            raise ValueError(f"Invalid platform_id: {self.platform_id!r}")
        started_at = self.started_at
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=timezone.utc)
        object.__setattr__(self, "platform_id", platform_id)
        object.__setattr__(self, "payload", _frozen_mapping(self.payload))
        object.__setattr__(self, "services", _frozen_mapping(self.services))
        object.__setattr__(self, "started_at", started_at)


@dataclass(frozen=True, slots=True)
class PlatformTaskResult:
    """Platform-neutral envelope containing opaque platform-native records."""

    success: bool = True
    items_processed: int = 0
    items: tuple[object, ...] = ()
    cursor: str | None = None
    warnings: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)
    error: str | None = None

    def __post_init__(self) -> None:
        if self.items_processed < 0:
            raise ValueError("items_processed cannot be negative")
        object.__setattr__(self, "items", tuple(self.items))
        object.__setattr__(self, "warnings", tuple(str(item) for item in self.warnings))
        object.__setattr__(self, "metadata", _frozen_mapping(self.metadata))

    @property
    def processed_count(self) -> int:
        return self.items_processed

    @classmethod
    def completed(
        cls,
        items: Iterable[object] = (),
        *,
        cursor: str | None = None,
        warnings: Iterable[str] = (),
        metadata: Mapping[str, Any] | None = None,
    ) -> PlatformTaskResult:
        values = tuple(items)
        return cls(
            success=True,
            items_processed=len(values),
            items=values,
            cursor=cursor,
            warnings=tuple(warnings),
            metadata=metadata or {},
        )

    @classmethod
    def failed(
        cls,
        error: str,
        *,
        warnings: Iterable[str] = (),
        metadata: Mapping[str, Any] | None = None,
    ) -> PlatformTaskResult:
        return cls(
            success=False,
            error=error,
            warnings=tuple(warnings),
            metadata=metadata or {},
        )


@dataclass(frozen=True, slots=True)
class ActionExecutionResult:
    """The minimum receipt every platform write adapter must return."""

    success: bool
    external_id: str | None = None
    receipt_url: str | None = None
    raw_receipt: object | None = None
    confirmed: bool = False
    error: str | None = None

    @property
    def confirmation_required(self) -> bool:
        return self.success and not self.confirmed

    @classmethod
    def failed(cls, error: str, *, raw_receipt: object | None = None) -> ActionExecutionResult:
        return cls(success=False, error=error, raw_receipt=raw_receipt, confirmed=False)


@dataclass(frozen=True, slots=True)
class PlatformOutcomeUpdate:
    """A read-only receipt reconciliation result for one shared action."""

    action_id: int
    result: ActionExecutionResult

    def __post_init__(self) -> None:
        if self.action_id <= 0:
            raise ValueError("action_id must be positive")
        if not isinstance(self.result, ActionExecutionResult):
            raise TypeError("result must be an ActionExecutionResult")


@dataclass(frozen=True, slots=True)
class PlatformHealthResult:
    platform_id: str
    ready: bool
    account: str | None = None
    detail: str | None = None


class PlatformSuggestedAction(StrEnum):
    """A platform proposal, before shared policy turns it into a core action."""

    COMMENT = "comment"
    DM = "dm"
    OWNED_PUBLISH = "owned_publish"


@dataclass(frozen=True, slots=True)
class NativeObjectReference:
    """Stable reference to an object that remains owned by a platform module."""

    platform_id: str
    object_type: str
    object_id: str

    def __post_init__(self) -> None:
        platform_id = self.platform_id.strip().lower()
        object_type = self.object_type.strip().lower()
        object_id = self.object_id.strip()
        if not _PLATFORM_ID.fullmatch(platform_id):
            raise ValueError(f"Invalid platform_id: {self.platform_id!r}")
        if not object_type:
            raise ValueError("object_type cannot be empty")
        if not object_id:
            raise ValueError("object_id cannot be empty")
        object.__setattr__(self, "platform_id", platform_id)
        object.__setattr__(self, "object_type", object_type)
        object.__setattr__(self, "object_id", object_id)


@dataclass(frozen=True, slots=True)
class PlatformActionProposal:
    """A platform-native action suggestion that still requires shared policy."""

    action_type: PlatformSuggestedAction
    draft: str
    score: float
    expires_at: str | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        action_type = PlatformSuggestedAction(self.action_type)
        if not 0 <= float(self.score) <= 100:
            raise ValueError("action proposal score must be between 0 and 100")
        object.__setattr__(self, "action_type", action_type)
        object.__setattr__(self, "draft", self.draft.strip())
        object.__setattr__(self, "score", float(self.score))
        object.__setattr__(self, "payload", _frozen_mapping(self.payload))


@dataclass(frozen=True, slots=True)
class PlatformOpportunityProposal:
    """Platform-ranked opportunity ready for the shared lifecycle repository."""

    opportunity_type: str
    native_object: NativeObjectReference
    priority: int
    score: float
    title: str
    evidence: tuple[Mapping[str, Any], ...] = ()
    payload: Mapping[str, Any] = field(default_factory=dict)
    expires_at: str | None = None
    score_reasons: tuple[str, ...] = ()
    suggested_actions: tuple[PlatformActionProposal, ...] = ()

    def __post_init__(self) -> None:
        opportunity_type = self.opportunity_type.strip().lower()
        if not opportunity_type:
            raise ValueError("opportunity_type cannot be empty")
        if not 0 <= int(self.priority) <= 100:
            raise ValueError("opportunity priority must be between 0 and 100")
        if not 0 <= float(self.score) <= 100:
            raise ValueError("opportunity score must be between 0 and 100")
        evidence = tuple(_frozen_mapping(value) for value in self.evidence)
        object.__setattr__(self, "opportunity_type", opportunity_type)
        object.__setattr__(self, "priority", int(self.priority))
        object.__setattr__(self, "score", float(self.score))
        object.__setattr__(self, "title", self.title.strip())
        object.__setattr__(self, "evidence", evidence)
        object.__setattr__(self, "payload", _frozen_mapping(self.payload))
        object.__setattr__(
            self, "score_reasons", tuple(str(value) for value in self.score_reasons)
        )
        object.__setattr__(self, "suggested_actions", tuple(self.suggested_actions))


@dataclass(frozen=True, slots=True)
class PlatformProjectionResult:
    """Result of projecting opaque task output into a platform-owned repository."""

    native_counts: Mapping[str, int] = field(default_factory=dict)
    opportunities: tuple[PlatformOpportunityProposal, ...] = ()
    warnings: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        counts = {str(key): int(value) for key, value in self.native_counts.items()}
        if any(not key or value < 0 for key, value in counts.items()):
            raise ValueError(
                "native_counts must contain non-empty keys and non-negative values"
            )
        object.__setattr__(self, "native_counts", _frozen_mapping(counts))
        object.__setattr__(self, "opportunities", tuple(self.opportunities))
        object.__setattr__(self, "warnings", tuple(str(value) for value in self.warnings))
        object.__setattr__(self, "metadata", _frozen_mapping(self.metadata))

    @property
    def items_processed(self) -> int:
        return sum(self.native_counts.values())


@runtime_checkable
class PlatformTaskHandler(Protocol):
    def __call__(self, context: PlatformTaskContext) -> PlatformTaskResult: ...


@runtime_checkable
class PlatformActionHandler(Protocol):
    def __call__(self, context: PlatformTaskContext) -> ActionExecutionResult: ...


@runtime_checkable
class PlatformHealthCheck(Protocol):
    def __call__(self) -> PlatformHealthResult: ...


@runtime_checkable
class PlatformSchemaInstaller(Protocol):
    def __call__(self, connection: sqlite3.Connection) -> None: ...


@runtime_checkable
class PlatformAutomationAdapter(Protocol):
    """Platform-owned projection, scoring, promotion, and KOL query boundary."""

    platform_id: str

    def project_discovery(
        self,
        items: Iterable[object],
        *,
        payload: Mapping[str, Any],
    ) -> PlatformProjectionResult: ...

    def project_signals(
        self,
        items: Iterable[object],
        *,
        payload: Mapping[str, Any],
    ) -> PlatformProjectionResult: ...

    def list_kols(
        self, *, status: str = "all", limit: int = 100
    ) -> list[dict[str, Any]]: ...

    def get_kol(self, native_id: str) -> dict[str, Any] | None: ...

    def list_scan_targets(
        self,
        *,
        statuses: Iterable[str],
        after: str | None,
        limit: int,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Return a stable rotating batch and the next native-ID cursor."""
        ...

    def summary(self) -> Mapping[str, int]: ...

    def set_kol_status(self, native_id: str, status: str) -> None: ...

    def add_seed(self, native_id: str, display_name: str | None = None) -> None: ...


Handler = PlatformTaskHandler | PlatformActionHandler


@dataclass(frozen=True, slots=True)
class PlatformPlugin:
    """One explicitly registered platform implementation and its extension points."""

    manifest: PlatformManifest
    handlers: Mapping[PlatformTaskName | str, Handler] = field(default_factory=dict)
    native_models: Mapping[str, type[Any]] = field(default_factory=dict)
    automation_adapter: PlatformAutomationAdapter | None = None
    schema_installer: PlatformSchemaInstaller | None = None
    router_factory: Callable[[], object] | None = None
    health_check: PlatformHealthCheck | None = None
    close: Callable[[], None] | None = None

    def __post_init__(self) -> None:
        normalized: dict[PlatformTaskName, Handler] = {}
        for name, handler in self.handlers.items():
            task_name = PlatformTaskName(name)
            if not callable(handler):
                raise TypeError(f"Handler for {task_name.value!r} is not callable")
            normalized[task_name] = handler
        models = dict(self.native_models)
        if any(not name or not isinstance(model, type) for name, model in models.items()):
            raise TypeError("native_models must map non-empty names to model classes")
        if self.automation_adapter is not None:
            if not isinstance(self.automation_adapter, PlatformAutomationAdapter):
                raise TypeError("automation_adapter does not implement PlatformAutomationAdapter")
            if self.automation_adapter.platform_id != self.manifest.platform_id:
                raise ValueError(
                    "automation_adapter platform_id must match the plugin manifest"
                )
        if self.schema_installer is not None and not callable(self.schema_installer):
            raise TypeError("schema_installer must be callable")
        object.__setattr__(self, "handlers", MappingProxyType(normalized))
        object.__setattr__(self, "native_models", MappingProxyType(models))

    def handler_for(self, task_name: PlatformTaskName | str) -> Handler | None:
        return self.handlers.get(PlatformTaskName(task_name))


class PlatformNotRegisteredError(KeyError):
    pass


class PlatformHandlerNotRegisteredError(LookupError):
    pass


class PlatformAutomationAdapterNotRegisteredError(LookupError):
    pass


class PlatformRegistry:
    """Thread-safe, explicit registry; there is intentionally no global singleton."""

    def __init__(self, plugins: Iterable[PlatformPlugin] = ()) -> None:
        self._plugins: dict[str, PlatformPlugin] = {}
        self._lock = RLock()
        for plugin in plugins:
            self.register(plugin)

    def register(self, plugin: PlatformPlugin, *, replace: bool = False) -> PlatformPlugin:
        platform_id = plugin.manifest.platform_id
        with self._lock:
            if platform_id in self._plugins and not replace:
                raise ValueError(f"Platform {platform_id!r} is already registered")
            self._plugins[platform_id] = plugin
        return plugin

    def unregister(self, platform_id: str) -> PlatformPlugin:
        with self._lock:
            try:
                return self._plugins.pop(platform_id)
            except KeyError as exc:
                raise PlatformNotRegisteredError(platform_id) from exc

    def get(self, platform_id: str) -> PlatformPlugin | None:
        with self._lock:
            return self._plugins.get(platform_id.strip().lower())

    def require(self, platform_id: str) -> PlatformPlugin:
        plugin = self.get(platform_id)
        if plugin is None:
            raise PlatformNotRegisteredError(platform_id)
        return plugin

    def list_plugins(self) -> tuple[PlatformPlugin, ...]:
        with self._lock:
            return tuple(self._plugins.values())

    def list_manifests(self) -> tuple[PlatformManifest, ...]:
        return tuple(plugin.manifest for plugin in self.list_plugins())

    @property
    def platform_ids(self) -> tuple[str, ...]:
        return tuple(manifest.platform_id for manifest in self.list_manifests())

    def supports(self, platform_id: str, capability: PlatformCapability | str) -> bool:
        return self.require(platform_id).manifest.supports(capability)

    def has_handler(
        self, platform_id: str, task_name: PlatformTaskName | str
    ) -> bool:
        return self.require(platform_id).handler_for(task_name) is not None

    def resolve_handler(
        self, platform_id: str, task_name: PlatformTaskName | str
    ) -> Handler:
        normalized = PlatformTaskName(task_name)
        handler = self.require(platform_id).handler_for(normalized)
        if handler is None:
            raise PlatformHandlerNotRegisteredError(
                f"Platform {platform_id!r} does not implement {normalized.value!r}"
            )
        return handler

    def resolve_automation_adapter(self, platform_id: str) -> PlatformAutomationAdapter:
        adapter = self.require(platform_id).automation_adapter
        if adapter is None:
            raise PlatformAutomationAdapterNotRegisteredError(
                f"Platform {platform_id!r} does not provide an automation adapter"
            )
        return adapter

    def dispatch(
        self,
        platform_id: str,
        task_name: PlatformTaskName | str,
        context: PlatformTaskContext | None = None,
        *,
        payload: Mapping[str, Any] | None = None,
        settings: object | None = None,
        services: Mapping[str, object] | None = None,
        task_id: str | int | None = None,
    ) -> PlatformTaskResult | ActionExecutionResult:
        """Run one handler exactly once; write retries are never performed here."""

        normalized_id = platform_id.strip().lower()
        normalized_task = PlatformTaskName(task_name)
        if context is not None and any(
            value is not None for value in (payload, settings, services, task_id)
        ):
            raise ValueError("Pass either context or context keyword arguments, not both")
        if context is None:
            context = PlatformTaskContext(
                platform_id=normalized_id,
                payload=payload or {},
                settings=settings,
                services=services or {},
                task_id=task_id,
            )
        elif context.platform_id != normalized_id:
            raise ValueError(
                f"Context is for {context.platform_id!r}, not requested platform {normalized_id!r}"
            )

        result = self.resolve_handler(normalized_id, normalized_task)(context)
        expected = (
            ActionExecutionResult
            if normalized_task is PlatformTaskName.EXECUTE_ACTION
            else PlatformTaskResult
        )
        if not isinstance(result, expected):
            raise TypeError(
                f"{normalized_id}.{normalized_task.value} returned {type(result).__name__}; "
                f"expected {expected.__name__}"
            )
        return result

    def health(self) -> tuple[PlatformHealthResult, ...]:
        """Check every platform independently so one provider cannot hide the others."""

        results: list[PlatformHealthResult] = []
        for plugin in self.list_plugins():
            if plugin.health_check is None:
                results.append(
                    PlatformHealthResult(
                        platform_id=plugin.manifest.platform_id,
                        ready=False,
                        detail="health check is not configured",
                    )
                )
                continue
            try:
                result = plugin.health_check()
            except Exception as exc:
                result = PlatformHealthResult(
                    platform_id=plugin.manifest.platform_id,
                    ready=False,
                    detail=str(exc),
                )
            results.append(result)
        return tuple(results)

    def install_schemas(
        self, connection: sqlite3.Connection, *, strict: bool = True
    ) -> tuple[str, ...]:
        installed: list[str] = []
        errors: list[tuple[str, Exception]] = []
        for plugin in self.list_plugins():
            if plugin.schema_installer is None:
                continue
            try:
                plugin.schema_installer(connection)
                installed.append(plugin.manifest.platform_id)
            except Exception as exc:
                errors.append((plugin.manifest.platform_id, exc))
                if strict:
                    raise
        if errors and not strict:
            # Callers that need details can initialize each selected plugin directly;
            # this method deliberately keeps its successful return shape simple.
            return tuple(installed)
        return tuple(installed)

    def close(self) -> None:
        first_error: Exception | None = None
        for plugin in reversed(self.list_plugins()):
            if plugin.close is None:
                continue
            try:
                plugin.close()
            except Exception as exc:
                first_error = first_error or exc
        if first_error is not None:
            raise first_error

    def __contains__(self, platform_id: object) -> bool:
        return isinstance(platform_id, str) and self.get(platform_id) is not None

    def __len__(self) -> int:
        with self._lock:
            return len(self._plugins)

    def __iter__(self):  # type: ignore[no-untyped-def]
        return iter(self.list_plugins())


__all__ = [
    "ActionExecutionResult",
    "NativeObjectReference",
    "PlatformActionHandler",
    "PlatformActionProposal",
    "PlatformAutomationAdapter",
    "PlatformAutomationAdapterNotRegisteredError",
    "PlatformCapability",
    "PlatformHandlerNotRegisteredError",
    "PlatformHealthCheck",
    "PlatformHealthResult",
    "PlatformManifest",
    "PlatformNotRegisteredError",
    "PlatformOpportunityProposal",
    "PlatformOutcomeUpdate",
    "PlatformPlugin",
    "PlatformProjectionResult",
    "PlatformRegistry",
    "PlatformSchemaInstaller",
    "PlatformTaskContext",
    "PlatformTaskHandler",
    "PlatformTaskName",
    "PlatformSuggestedAction",
]
