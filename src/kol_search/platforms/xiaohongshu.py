from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

from kol_search.platforms.kernel import (
    ActionExecutionResult,
    PlatformCapability,
    PlatformHealthResult,
    PlatformManifest,
    PlatformPlugin,
    PlatformTaskContext,
    PlatformTaskName,
    PlatformTaskResult,
)


class XiaohongshuUser(BaseModel):
    """Xiaohongshu-native user model."""

    external_id: str
    nickname: str
    native_id_resolved: bool = True
    bio: str | None = None
    followers_count: int = 0
    following_count: int = 0
    verified: bool = False
    protected: bool = False
    disabled: bool = False
    spam_risk: float = Field(default=0.0, ge=0.0, le=1.0)
    anomaly_signals: tuple[str, ...] = ()
    avatar_url: str | None = None
    profile_url: str | None = None
    source_provider: str = "opencli"
    captured_at: str | None = None


class XiaohongshuNoteMetrics(BaseModel):
    likes: int = 0
    comments: int = 0
    collects: int = 0
    views: int = 0


class XiaohongshuNote(BaseModel):
    """A note remains distinct from X tweets and future video objects."""

    external_id: str
    author_external_id: str
    author_native_id_resolved: bool = True
    author_nickname: str | None = None
    note_type: Literal["image", "video", "unknown"] = "unknown"
    title: str = ""
    body: str = ""
    published_at: str | None = None
    url: str | None = None
    metrics: XiaohongshuNoteMetrics = Field(default_factory=XiaohongshuNoteMetrics)
    relevance_score: float = Field(default=0.0, ge=0.0, le=1.0)
    source_provider: str = "opencli"
    captured_at: str | None = None


class XiaohongshuTrend(BaseModel):
    name: str
    rank: int = 0
    note_count: int = 0
    url: str | None = None


class XiaohongshuComment(BaseModel):
    """A note comment is a first-class Xiaohongshu object, not a generic reply."""

    external_id: str
    note_external_id: str
    author_external_id: str
    author_nickname: str | None = None
    author_protected: bool = False
    author_disabled: bool = False
    author_spam_risk: float = Field(default=0.0, ge=0.0, le=1.0)
    author_anomaly_signals: tuple[str, ...] = ()
    body: str = ""
    created_at: str | None = None
    likes: int = 0
    replies: int = 0
    source_provider: str = "opencli"
    captured_at: str | None = None


XIAOHONGSHU_MANIFEST = PlatformManifest(
    platform_id="xiaohongshu",
    name="小红书",
    version="1.0",
    capabilities=frozenset(
        {
            PlatformCapability.CONTENT_SEARCH,
            PlatformCapability.TIMELINE_FEED,
            PlatformCapability.NATIVE_TRENDS,
            PlatformCapability.COMMENT,
            PlatformCapability.DM,
        }
    ),
    default_scan_interval_seconds=30 * 60,
    safety_limits={
        "comment_hourly": 3,
        "comment_daily": 10,
        "dm_hourly": 2,
        "dm_daily": 5,
        "author_cooldown_days": 7,
    },
    workbench_path="/platforms/xiaohongshu",
    metadata={
        "content_types": ("note", "video_note", "comment"),
        "accent_color": "#ff2442",
        "short_name": "RED",
        "workspace_template": "platform_xiaohongshu_workspace.html",
    },
)


def _attr(source: object, name: str, default: Any = None) -> Any:
    if isinstance(source, Mapping):
        return source.get(name, default)
    return getattr(source, name, default)


def _count(value: object) -> int:
    """Normalize native counters without failing an otherwise valid scan."""

    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return max(0, int(value))
    text = str(value or "").strip().lower().replace(",", "")
    multiplier = 1
    for suffix, factor in (
        ("万", 10_000),
        ("w", 10_000),
        ("k", 1_000),
        ("亿", 100_000_000),
        ("m", 1_000_000),
    ):
        if text.endswith(suffix):
            multiplier = factor
            text = text[: -len(suffix)].strip()
            break
    try:
        return max(0, int(float(text) * multiplier))
    except (TypeError, ValueError, OverflowError):
        return 0


def _truthy_signal(source: object, raw: Mapping[str, Any], *names: str) -> bool:
    def enabled(value: object) -> bool:
        if isinstance(value, str):
            return value.strip().casefold() in {"1", "true", "yes", "y", "on"}
        return bool(value)

    return any(
        enabled(_attr(source, name, False)) or enabled(raw.get(name)) for name in names
    )


def _xiaohongshu_user_risk(
    source: object,
) -> tuple[bool, bool, float, tuple[str, ...]]:
    """Normalize RED-native visibility/state flags and conservative anomaly rules."""

    raw_value = _attr(source, "raw", {}) or {}
    raw = raw_value if isinstance(raw_value, Mapping) else {}
    status = str(_attr(source, "status", None) or raw.get("status") or "").casefold()
    protected = _truthy_signal(source, raw, "protected", "private", "is_private")
    disabled = _truthy_signal(
        source,
        raw,
        "disabled",
        "banned",
        "deactivated",
        "deleted",
    ) or status in {"disabled", "banned", "deactivated", "deleted", "unavailable"}
    signal_values = _attr(source, "anomaly_signals", None) or raw.get(
        "anomaly_signals", ()
    )
    signals = [str(value) for value in signal_values if str(value)]
    explicit = _attr(source, "spam_risk", None)
    if explicit is None:
        explicit = _attr(source, "spamRisk", None)
    if explicit is None:
        explicit = raw.get("spam_risk", raw.get("spamRisk", 0.0))
    try:
        risk = min(1.0, max(0.0, float(explicit or 0.0)))
    except (TypeError, ValueError, OverflowError):
        risk = 0.0

    followers = _count(_attr(source, "followers_count", 0))
    following = _count(_attr(source, "following_count", 0))
    bio = str(
        _attr(source, "description", None) or _attr(source, "bio", "") or ""
    ).casefold()
    if following >= 1_000 and following > max(50, followers * 20):
        signals.append("extreme_follow_ratio")
        risk += 0.45
    if any(
        phrase in bio
        for phrase in ("加微信", "私信返利", "稳赚", "带单", "免费空投", "guaranteed profit")
    ):
        signals.append("spam_phrase_in_bio")
        risk += 0.70
    if protected:
        signals.append("private_profile")
    if disabled:
        signals.append("provider_disabled")
    return protected, disabled, min(1.0, risk), tuple(dict.fromkeys(signals))


def _user(source: object) -> XiaohongshuUser:
    external_id = str(_attr(source, "external_id") or _attr(source, "id") or "")
    external_id = external_id.removeprefix("xiaohongshu:")
    nickname = str(_attr(source, "name") or _attr(source, "username") or "")
    protected, disabled, spam_risk, anomaly_signals = _xiaohongshu_user_risk(source)
    return XiaohongshuUser(
        external_id=external_id,
        nickname=nickname,
        native_id_resolved=bool(_attr(source, "native_id_resolved", True)),
        bio=_attr(source, "description"),
        followers_count=_count(_attr(source, "followers_count", 0)),
        following_count=_count(_attr(source, "following_count", 0)),
        verified=bool(_attr(source, "verified", False)),
        protected=protected,
        disabled=disabled,
        spam_risk=spam_risk,
        anomaly_signals=anomaly_signals,
        avatar_url=_attr(source, "profile_image_url"),
        profile_url=_attr(source, "url"),
        source_provider=str(_attr(source, "source_provider", "opencli") or "opencli"),
        captured_at=_attr(source, "captured_at"),
    )


def _note(source: object) -> XiaohongshuNote:
    text = str(_attr(source, "text") or "")
    lines = text.splitlines()
    title = lines[0].strip() if lines else ""
    body = "\n".join(lines[1:]).strip() if len(lines) > 1 else ""
    raw = _attr(source, "raw", {}) or {}
    raw_item = raw.get("opencli", {}) if isinstance(raw, Mapping) else {}
    native_type = str(
        raw_item.get("type") or raw_item.get("note_type") or "unknown"
    ).lower()
    note_type: Literal["image", "video", "unknown"]
    if "video" in native_type or "视频" in native_type:
        note_type = "video"
    elif native_type in {"image", "images", "normal", "图文"}:
        note_type = "image"
    else:
        note_type = "unknown"
    external_id = str(_attr(source, "external_id") or _attr(source, "id") or "")
    author_id = str(_attr(source, "author_id") or "")
    return XiaohongshuNote(
        external_id=external_id.removeprefix("xiaohongshu:"),
        author_external_id=author_id.removeprefix("xiaohongshu:"),
        author_native_id_resolved=bool(
            _attr(source, "author_native_id_resolved", True)
        ),
        author_nickname=_attr(source, "author_username"),
        note_type=note_type,
        title=title,
        body=body,
        published_at=_attr(source, "created_at"),
        url=_attr(source, "url"),
        metrics=XiaohongshuNoteMetrics(
            likes=_count(_attr(source, "like_count", 0)),
            comments=_count(_attr(source, "reply_count", 0)),
            collects=_count(_attr(source, "bookmark_count", 0)),
            views=_count(_attr(source, "view_count", 0)),
        ),
        source_provider=str(_attr(source, "source_provider", "opencli") or "opencli"),
        captured_at=_attr(source, "captured_at"),
    )


def _trend(source: object) -> XiaohongshuTrend:
    return XiaohongshuTrend(
        name=str(_attr(source, "name") or ""),
        rank=_count(_attr(source, "rank", 0)),
        note_count=_count(_attr(source, "post_count", 0)),
        url=_attr(source, "url"),
    )


def _comment(source: Mapping[str, Any], note_external_id: str) -> XiaohongshuComment:
    user = source.get("user") if isinstance(source.get("user"), Mapping) else {}
    author_id = str(
        source.get("user_id")
        or source.get("userId")
        or source.get("author_id")
        or user.get("id")
        or user.get("userId")
        or "unknown"
    )
    body = str(source.get("content") or source.get("text") or source.get("body") or "")
    external_id = str(source.get("comment_id") or source.get("id") or "")
    if not external_id:
        material = json.dumps(source, ensure_ascii=False, sort_keys=True, default=str)
        external_id = hashlib.sha256(
            f"{note_external_id}\x1f{material}".encode("utf-8")
        ).hexdigest()[:24]
    risk_source = dict(user)
    for key in (
        "protected",
        "private",
        "is_private",
        "disabled",
        "banned",
        "deactivated",
        "deleted",
        "status",
        "spam_risk",
        "spamRisk",
        "anomaly_signals",
    ):
        if key in source:
            risk_source[key] = source[key]
    protected, disabled, spam_risk, anomaly_signals = _xiaohongshu_user_risk(
        risk_source
    )
    return XiaohongshuComment(
        external_id=external_id,
        note_external_id=note_external_id,
        author_external_id=author_id,
        author_nickname=str(
            source.get("nickname")
            or source.get("author")
            or user.get("nickname")
            or ""
        )
        or None,
        author_protected=protected,
        author_disabled=disabled,
        author_spam_risk=spam_risk,
        author_anomaly_signals=anomaly_signals,
        body=body,
        created_at=_optional_string(
            source.get("created_at") or source.get("time") or source.get("createTime")
        ),
        likes=_count(source.get("likes") or source.get("like_count")),
        replies=_count(source.get("replies") or source.get("reply_count")),
        source_provider="opencli",
    )


class _XiaohongshuRuntime:
    def __init__(
        self,
        settings: object,
        provider_factory: Callable[[], object] | None,
    ) -> None:
        self.settings = settings
        self._provider_factory = provider_factory
        self._provider: object | None = None

    def provider(self) -> object:
        if self._provider is None:
            if self._provider_factory is not None:
                self._provider = self._provider_factory()
            else:
                from kol_search.platforms.opencli import OpenCliXiaohongshuReader

                self._provider = OpenCliXiaohongshuReader(
                    command=str(getattr(self.settings, "opencli_command", "opencli")),
                    profile=str(
                        getattr(
                            self.settings,
                            "xiaohongshu_opencli_profile",
                            "ddd",
                        )
                    ),
                    timeout=float(
                        getattr(self.settings, "opencli_timeout_seconds", 90.0)
                    ),
                )
        return self._provider

    def close(self) -> None:
        if self._provider is not None:
            close = getattr(self._provider, "close", None)
            if callable(close):
                close()
            self._provider = None

    def health(self) -> PlatformHealthResult:
        result = self.provider().health_check()  # type: ignore[attr-defined]
        return PlatformHealthResult(
            platform_id="xiaohongshu",
            ready=bool(_attr(result, "ready", False)),
            account=_attr(result, "account"),
            detail=_attr(result, "detail"),
        )

    def discover(self, context: PlatformTaskContext) -> PlatformTaskResult:
        provider = self.provider()
        limit = max(1, min(int(context.payload.get("limit", 20)), 100))
        queries = _strings(context.payload.get("queries") or context.payload.get("query") or ())
        seed_accounts = context.payload.get("seed_accounts") or ()
        native_ids = (
            str(item.get("id") or item.get("external_id") or "")
            for item in seed_accounts
            if isinstance(item, Mapping)
        )
        seed_ids = _strings(
            context.payload.get("seed_external_ids")
            or context.payload.get("seeds")
            or native_ids
        )
        if not queries and not seed_ids:
            raise ValueError("Xiaohongshu discovery requires query/queries or seed accounts")
        items: list[object] = []
        warnings: list[str] = []
        attempted = 0
        succeeded = 0
        for query in queries:
            attempted += 1
            try:
                items.extend(
                    _note(item)
                    for item in provider.search_posts(query, limit)  # type: ignore[attr-defined]
                )
                succeeded += 1
            except Exception as exc:
                warnings.append(f"content search {query!r} failed: {exc}")
        for external_id in seed_ids:
            attempted += 1
            try:
                account = provider.get_account(external_id)  # type: ignore[attr-defined]
                if account is not None:
                    items.append(_user(account))
                items.extend(
                    _note(item)
                    for item in provider.get_account_posts(external_id, limit)  # type: ignore[attr-defined]
                )
                succeeded += 1
            except Exception as exc:
                warnings.append(f"seed {external_id!r} failed: {exc}")
        metadata = {
            "provider": str(getattr(provider, "provider", "unknown")),
            "attempted_calls": attempted,
            "succeeded_calls": succeeded,
        }
        if attempted and not succeeded:
            return PlatformTaskResult.failed(
                "all Xiaohongshu discovery provider calls failed",
                warnings=tuple(dict.fromkeys(warnings)),
                metadata=metadata,
            )
        return PlatformTaskResult.completed(
            items,
            warnings=tuple(dict.fromkeys(warnings)),
            metadata=metadata,
        )

    def scan_signals(self, context: PlatformTaskContext) -> PlatformTaskResult:
        provider = self.provider()
        limit = max(1, min(int(context.payload.get("limit", 20)), 100))
        account_cursors = _decode_scan_cursor(
            _optional_string(context.payload.get("cursor"))
        )
        account_ids = _strings(
            context.payload.get("account_external_ids")
            or context.payload.get("accounts")
            or ()
        )
        items: list[object] = []
        comment_watch_notes: list[XiaohongshuNote] = []
        warnings: list[str] = []
        attempted = 0
        succeeded = 0
        for external_id in account_ids:
            attempted += 1
            try:
                notes = [
                    _note(item)
                    for item in provider.get_account_posts(external_id, limit)  # type: ignore[attr-defined]
                ]
                # Comments have their own lifecycle. Keep recently observed notes in
                # the watch set even when their publication timestamp predates the
                # per-account content cursor, otherwise new comments on an existing
                # note would never be collected after the first scan.
                comment_watch_notes.extend(note for note in notes if note.url)
                previous = account_cursors.get(external_id) or account_cursors.get("*")
                items.extend(
                    note
                    for note in notes
                    if not previous or _is_after_cursor(note.published_at, previous)
                )
                latest = _latest_scan_cursor(
                    previous,
                    (note.published_at for note in notes),
                )
                if latest:
                    account_cursors[external_id] = latest
                succeeded += 1
            except Exception as exc:
                warnings.append(f"feed {external_id!r} failed: {exc}")
        if bool(context.payload.get("include_trends", True)):
            attempted += 1
            try:
                items.extend(_trend(item) for item in provider.get_trends(limit))  # type: ignore[attr-defined]
                succeeded += 1
            except Exception as exc:
                warnings.append(f"native trends failed: {exc}")
        if bool(context.payload.get("include_comments", False)):
            comment_limit = max(
                1, min(int(context.payload.get("comment_limit", 20)), 50)
            )
            thread_limit = max(
                0, min(int(context.payload.get("comment_threads_limit", 3)), 10)
            )
            watched_notes = list(
                {
                    note.external_id: note
                    for note in comment_watch_notes
                    if note.external_id
                }.values()
            )
            for note in watched_notes[:thread_limit]:
                attempted += 1
                try:
                    rows = provider.get_comments(note.url, comment_limit)  # type: ignore[attr-defined]
                    items.extend(
                        _comment(row, note.external_id)
                        for row in rows
                        if isinstance(row, Mapping)
                    )
                    succeeded += 1
                except Exception as exc:
                    warnings.append(f"comments for {note.external_id!r} failed: {exc}")
        metadata = {
            "provider": str(getattr(provider, "provider", "unknown")),
            "attempted_calls": attempted,
            "succeeded_calls": succeeded,
        }
        if attempted and not succeeded:
            return PlatformTaskResult.failed(
                "all Xiaohongshu signal provider calls failed",
                warnings=tuple(dict.fromkeys(warnings)),
                metadata=metadata,
            )
        return PlatformTaskResult.completed(
            items,
            cursor=_encode_scan_cursor(account_cursors),
            warnings=tuple(dict.fromkeys(warnings)),
            metadata=metadata,
        )

    def refresh_outcomes(self, context: PlatformTaskContext) -> PlatformTaskResult:
        from kol_search.platform_modules.browser_actions import (
            refresh_xiaohongshu_browser_outcomes,
        )

        if context.settings is None:
            context = replace(context, settings=self.settings)
        return refresh_xiaohongshu_browser_outcomes(context)

    def execute_action(self, context: PlatformTaskContext) -> ActionExecutionResult:
        try:
            from kol_search.platform_modules.browser_actions import (
                execute_xiaohongshu_browser_action,
            )

            if context.settings is None:
                context = replace(context, settings=self.settings)
            return execute_xiaohongshu_browser_action(context)
        except Exception as exc:
            return ActionExecutionResult.failed(str(exc))


def _strings(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value.strip(),) if value.strip() else ()
    if isinstance(value, Iterable):
        return tuple(str(item).strip() for item in value if str(item).strip())
    return ()


def _optional_string(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _decode_scan_cursor(value: str | None) -> dict[str, str]:
    if not value:
        return {}
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return {"*": value}
    if not isinstance(payload, Mapping):
        return {"*": value}
    accounts = payload.get("accounts")
    if not isinstance(accounts, Mapping):
        return {}
    return {
        str(account_id): str(timestamp)
        for account_id, timestamp in accounts.items()
        if str(account_id).strip() and str(timestamp).strip()
    }


def _encode_scan_cursor(values: Mapping[str, str]) -> str | None:
    accounts = {key: value for key, value in values.items() if key != "*"}
    if not accounts:
        return values.get("*")
    return json.dumps(
        {"version": 1, "accounts": accounts},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _is_after_cursor(value: str | None, cursor: str) -> bool:
    if not value:
        return True
    try:
        observed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        previous = datetime.fromisoformat(cursor.replace("Z", "+00:00"))
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=timezone.utc)
        if previous.tzinfo is None:
            previous = previous.replace(tzinfo=timezone.utc)
        return observed > previous
    except ValueError:
        return value > cursor


def _latest_scan_cursor(
    previous: str | None,
    observed_values: Iterable[str | None],
) -> str | None:
    """Advance a content cursor monotonically despite unordered provider rows."""

    latest = previous
    for value in observed_values:
        if not value:
            continue
        if latest is None or _is_after_cursor(value, latest):
            latest = value
    return latest


def xiaohongshu_manifest(settings: object | None = None) -> PlatformManifest:
    if settings is None:
        return XIAOHONGSHU_MANIFEST
    interval_minutes = int(
        getattr(settings, "xiaohongshu_signal_interval_minutes", None)
        or getattr(settings, "signal_interval_minutes", 30)
    )
    limits = {
        "comment_hourly": int(getattr(settings, "comment_hourly_limit", 3)),
        "comment_daily": int(getattr(settings, "comment_daily_limit", 10)),
        "dm_hourly": int(getattr(settings, "dm_hourly_limit", 2)),
        "dm_daily": int(getattr(settings, "dm_daily_limit", 5)),
        "author_cooldown_days": int(getattr(settings, "author_cooldown_days", 7)),
    }
    return replace(
        XIAOHONGSHU_MANIFEST,
        default_scan_interval_seconds=max(60, interval_minutes * 60),
        safety_limits=limits,
    )


def _xiaohongshu_router_factory(manifest: PlatformManifest) -> object:
    from kol_search.platform_modules.web_router import (
        create_platform_workspace_router,
    )

    return create_platform_workspace_router(manifest)


def create_xiaohongshu_plugin(
    settings: object,
    *,
    provider_factory: Callable[[], object] | None = None,
) -> PlatformPlugin:
    """Create a Xiaohongshu plugin with its own lazy OpenCLI provider."""

    from kol_search.platform_modules.adapter_utils import build_opportunity_task
    from kol_search.platform_modules.xiaohongshu_adapter import (
        XiaohongshuAutomationAdapter,
    )
    from kol_search.platform_modules.xiaohongshu_native import (
        install_xiaohongshu_schema,
    )

    manifest = xiaohongshu_manifest(settings)
    runtime = _XiaohongshuRuntime(settings, provider_factory)
    adapter = XiaohongshuAutomationAdapter(settings)
    return PlatformPlugin(
        manifest=manifest,
        handlers={
            PlatformTaskName.DISCOVER: runtime.discover,
            PlatformTaskName.SCAN_SIGNALS: runtime.scan_signals,
            PlatformTaskName.BUILD_OPPORTUNITIES: (
                lambda context: build_opportunity_task(adapter, context)
            ),
            PlatformTaskName.EXECUTE_ACTION: runtime.execute_action,
            PlatformTaskName.REFRESH_OUTCOMES: runtime.refresh_outcomes,
        },
        native_models={
            "user": XiaohongshuUser,
            "note": XiaohongshuNote,
            "comment": XiaohongshuComment,
            "trend": XiaohongshuTrend,
        },
        automation_adapter=adapter,
        schema_installer=install_xiaohongshu_schema,
        router_factory=lambda: _xiaohongshu_router_factory(manifest),
        health_check=runtime.health,
        close=runtime.close,
    )


__all__ = [
    "XIAOHONGSHU_MANIFEST",
    "XiaohongshuNote",
    "XiaohongshuNoteMetrics",
    "XiaohongshuComment",
    "XiaohongshuTrend",
    "XiaohongshuUser",
    "create_xiaohongshu_plugin",
    "xiaohongshu_manifest",
]
