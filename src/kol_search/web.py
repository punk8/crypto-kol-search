from __future__ import annotations

import hashlib
import hmac
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from kol_search.automation import (
    ActionStatus,
    AutomationStore,
    StateTransitionError,
)
from kol_search.automation.service import PlatformAutomationService
from kol_search.backend import discover_router, router as backend_router
from kol_search.backend.connectors import build_connector_registry
from kol_search.backend.discover_automation import parse_discovery_domains
from kol_search.backend.models import (
    AccountContentQuery,
    ContentItem,
    ContentSearchQuery,
    HotContentItem,
    ReplyDraftRequest,
    AccountItem,
    AccountSearchQuery,
    TrendQuery,
)
from kol_search.backend.kol_scoring import domain_terms
from kol_search.discover_gateway import DiscoverGateway, DiscoverGatewayError
from kol_search.discover_mock import build_mock_discover_context
from kol_search.platforms import (
    build_default_registry,
    install_registered_platform_schemas,
)
from kol_search.platform_modules.web_router import (
    ACTION_LABELS,
    CAPABILITY_LABELS,
    JOB_LABELS,
    platform_workspace_template as _platform_workspace_template,
)
from kol_search.settings import Settings, get_settings


PACKAGE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(PACKAGE_DIR / "templates"))


def _local_datetime(value: Any, timezone_name: str = "Asia/Shanghai") -> str:
    """Render stored ISO timestamps in the configured product timezone."""
    if not value:
        return "—"
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(ZoneInfo(timezone_name)).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, ZoneInfoNotFoundError):
        return str(value)


templates.env.filters["local_datetime"] = _local_datetime


@asynccontextmanager
async def lifespan(application: FastAPI):
    settings = get_settings()
    externally_served = settings.web_host not in {
        "127.0.0.1", "localhost", "::1"
    }
    if externally_served and not (
        settings.admin_password and settings.session_secret
    ):
        raise RuntimeError(
            "非本机监听必须同时配置 KOL_ADMIN_PASSWORD 和 KOL_SESSION_SECRET"
        )

    split_frontend = bool(settings.backend_url)
    if split_frontend:
        connector_registry = build_connector_registry(settings)
        discover_gateway = DiscoverGateway(settings, connector_registry)
        application.state.settings = settings
        application.state.automation = None
        application.state.platform_registry = None
        application.state.connector_registry = connector_registry
        application.state.discover_gateway = discover_gateway
        application.state.platform_service = None
        application.state.split_frontend = True
        try:
            yield
        finally:
            discover_gateway.close()
            connector_registry.close()
        return

    database_target = settings.database_target()
    automation = AutomationStore(database_target)
    automation.database.wait_ready()
    registry = build_default_registry(settings)
    connector_registry = build_connector_registry(settings)
    discover_gateway = DiscoverGateway(settings, connector_registry)
    install_registered_platform_schemas(database_target, registry)
    _install_platform_routers(application, registry)

    # PlatformAutomationService owns shared orchestration only. Platform-native
    # repositories resolve their database directly from Settings.
    platform_service = PlatformAutomationService(automation, registry, settings)
    platform_service.initialize_defaults()

    application.state.settings = settings
    application.state.automation = automation
    application.state.platform_registry = registry
    application.state.connector_registry = connector_registry
    application.state.discover_gateway = discover_gateway
    application.state.platform_service = platform_service
    application.state.split_frontend = False
    try:
        yield
    finally:
        discover_gateway.close()
        connector_registry.close()
        registry.close()
        automation.database.close()


app = FastAPI(title="Trend & KOL Discover", version="2.0.0", lifespan=lifespan)
app.include_router(backend_router)
app.include_router(discover_router)
app.mount("/static", StaticFiles(directory=str(PACKAGE_DIR / "static")), name="static")


def _install_platform_routers(application: FastAPI, registry: Any) -> None:
    """Mount each registered platform router once, in registry order."""

    mounted = getattr(application.state, "mounted_platform_routers", set())
    for plugin in registry.list_plugins():
        platform_id = plugin.manifest.platform_id
        if plugin.router_factory is None or platform_id in mounted:
            continue
        router = plugin.router_factory()
        if not hasattr(router, "routes"):
            raise TypeError(
                f"{platform_id} router_factory did not return an APIRouter"
            )
        application.include_router(router)
        mounted.add(platform_id)
    application.state.mounted_platform_routers = mounted


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _automation(request: Request) -> AutomationStore:
    automation = request.app.state.automation
    if automation is None:
        raise HTTPException(
            status_code=503, detail="This operation belongs to the backend service."
        )
    return automation


def _platform_service(request: Request) -> PlatformAutomationService:
    service = request.app.state.platform_service
    if service is None:
        raise HTTPException(
            status_code=503, detail="This operation belongs to the backend service."
        )
    return service


def _discover_gateway(request: Request) -> DiscoverGateway:
    return request.app.state.discover_gateway


def _actor(request: Request) -> str:
    return _settings(request).admin_username


def _auth_secret(settings: Settings) -> bytes:
    material = settings.session_secret or settings.admin_password or "local-only"
    return material.encode("utf-8")


def _session_token(settings: Settings) -> str:
    return hmac.new(
        _auth_secret(settings),
        settings.admin_username.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


@app.middleware("http")
async def admin_security(request: Request, call_next: Any) -> Response:
    settings = getattr(request.app.state, "settings", None) or get_settings()
    service_api_path = request.url.path.startswith("/api/discover/")
    split_frontend = bool(getattr(request.app.state, "split_frontend", False))
    if split_frontend and service_api_path:
        return JSONResponse({"detail": "Not found"}, status_code=404)
    if split_frontend and request.url.path.startswith(
        ("/agents", "/automation", "/settings", "/discover/")
    ):
        return RedirectResponse(url="/trends", status_code=303)
    if request.url.path.startswith("/agents"):
        return RedirectResponse(url="/", status_code=303)
    auth_enabled = bool(settings.admin_password)
    public_path = request.url.path in {"/health", "/login"} or request.url.path.startswith(
        "/static/"
    )
    if auth_enabled and not public_path and not service_api_path:
        supplied = request.cookies.get("kol_admin_session", "")
        if not secrets.compare_digest(supplied, _session_token(settings)):
            return RedirectResponse(url="/login", status_code=303)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            if origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
                return JSONResponse({"detail": "CSRF origin rejected"}, status_code=403)
    if (
        settings.web_read_only
        and not service_api_path
        and request.method not in {"GET", "HEAD", "OPTIONS"}
        and request.url.path not in {"/login", "/logout"}
    ):
        return JSONResponse({"detail": "Preview deployment is read-only"}, status_code=503)
    return await call_next(request)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request=request, name="login.html", context={})


@app.post("/login")
def login(
    request: Request,
    username: Annotated[str, Form()],
    password: Annotated[str, Form()],
) -> Response:
    settings = _settings(request)
    if not settings.admin_password:
        return RedirectResponse(url="/", status_code=303)
    valid = secrets.compare_digest(
        username, settings.admin_username
    ) and secrets.compare_digest(password, settings.admin_password)
    if not valid:
        raise HTTPException(status_code=401, detail="管理员账号或密码错误")
    response = RedirectResponse(url="/", status_code=303)
    response.set_cookie(
        "kol_admin_session",
        _session_token(settings),
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="strict",
        max_age=8 * 3600,
    )
    return response


@app.post("/logout")
def logout() -> Response:
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie("kol_admin_session")
    return response


def _short_name(platform_id: str, metadata: dict[str, Any] | None = None) -> str:
    return str((metadata or {}).get("short_name") or platform_id[:3].upper())


def _connection_status(value: str | None) -> str:
    return "disabled" if value == "paused" else (value or "disconnected")


def _local_day_start_utc(settings: Settings) -> str:
    local = datetime.now(ZoneInfo(settings.timezone))
    return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(
        timezone.utc
    ).isoformat()


def _worker_status(automation: AutomationStore) -> dict[str, object]:
    heartbeat = automation.latest_worker_heartbeat()
    if not heartbeat:
        return {"online": False, "status": "missing", "last_seen_at": None}
    try:
        last_seen = datetime.fromisoformat(
            str(heartbeat["last_seen_at"]).replace("Z", "+00:00")
        )
        online = (
            heartbeat.get("status") == "online"
            and datetime.now(timezone.utc) - last_seen <= timedelta(seconds=90)
        )
    except (KeyError, TypeError, ValueError):
        online = False
    return {
        "online": online,
        "status": heartbeat.get("status"),
        "worker_id": heartbeat.get("worker_id"),
        "host_label": heartbeat.get("host_label"),
        "last_seen_at": heartbeat.get("last_seen_at"),
    }


def _platform_center_context(request: Request) -> dict[str, object]:
    automation = _automation(request)
    service = _platform_service(request)
    registry = request.app.state.platform_registry
    settings = _settings(request)
    global_summary = automation.get_global_summary()
    stored_summaries = {
        row["platform_id"]: row
        for row in automation.get_platform_summaries(registry.platform_ids)
    }
    connections = automation.list_platform_connections()
    jobs = automation.list_jobs(limit=30)
    platforms: list[dict[str, object]] = []
    alerts: list[dict[str, str]] = []
    day_start = _local_day_start_utc(settings)

    for manifest in registry.list_manifests():
        platform_id = manifest.platform_id
        summary = stored_summaries.get(platform_id, {})
        reader = next(
            (
                row
                for row in connections
                if row["platform_id"] == platform_id
                and row["connection_key"] == "reader"
            ),
            None,
        )
        native = service.native_summary(platform_id)
        platform_jobs = [row for row in jobs if row["platform_id"] == platform_id]
        last_scan = next(
            (
                row.get("finished_at") or row.get("created_at")
                for row in platform_jobs
                if row["job_type"]
                in {"signal_refresh", "discovery_refresh", "manual_discovery"}
            ),
            None,
        )
        status = _connection_status((reader or {}).get("status"))
        message = (reader or {}).get("last_health_error") or (
            ((reader or {}).get("metadata") or {}).get("detail")
        )
        workspace_url = manifest.workbench_path or f"/platforms/{platform_id}"
        if status != "connected":
            alerts.append(
                {
                    "severity": "warning",
                    "title": "平台连接需要处理",
                    "message": str(message or "尚未验证平台连接"),
                    "platform_name": manifest.name,
                    "created_at": str((reader or {}).get("last_health_at") or "尚未检查"),
                    "href": workspace_url,
                }
            )
        platforms.append(
            {
                **manifest.as_dict(),
                "short_name": _short_name(platform_id, dict(manifest.metadata)),
                "enabled": platform_id in settings.platform_ids(),
                "connection_status": status,
                "connection_message": message,
                "workspace_url": workspace_url,
                "last_scan_at": last_scan,
                "metrics": {
                    "active_kols": int(native.get("active", 0)),
                    "candidate_kols": int(native.get("candidate", 0)),
                    "trends": int(native.get("trends", 0)),
                    "content": int(native.get("content", 0)),
                    "open_opportunities": int(summary.get("open_opportunity_count", 0)),
                    "succeeded_actions_today": automation.count_succeeded_actions(
                        platform_id,
                        since=day_start,
                        automatic_only=True,
                    ),
                    "paused_channels": int(summary.get("paused_channel_count", 0)),
                },
            }
        )

    for action in automation.list_actions(limit=30):
        if action["status"] not in {
            ActionStatus.FAILED.value,
            ActionStatus.CONFIRMATION_REQUIRED.value,
        }:
            continue
        alerts.append(
            {
                "severity": (
                    "critical"
                    if action["status"] == ActionStatus.FAILED.value
                    else "warning"
                ),
                "title": "自动动作需要确认",
                "message": action.get("error") or action["status"],
                "platform_name": action["platform_id"],
                "created_at": action["updated_at"],
                "href": f"/platforms/{action['platform_id']}#automation",
            }
        )

    recent_runs = [
        {
            **run,
            "platform_short_name": _short_name(run["platform_id"]),
            "label": JOB_LABELS.get(run["job_type"], run["job_type"]),
            "detail_url": f"/platforms/{run['platform_id']}/runs#job-{run['id']}",
        }
        for run in jobs[:12]
    ]
    queued = sum(1 for row in jobs if row["status"] == "queued")
    running = sum(1 for row in jobs if row["status"] == "running")
    failed = sum(1 for row in jobs if row["status"] == "failed")
    discovery_url = "/#kols"
    return {
        "urls": {"discovery": discovery_url, "safety": "/settings/brand#automation-controls"},
        "global_kill_switch": global_summary["global_kill_switch"],
        "capability_labels": CAPABILITY_LABELS,
        "summary": {
            "enabled_platforms": sum(1 for row in platforms if row["enabled"]),
            "total_platforms": len(platforms),
            "active_tasks": running,
            "queued_tasks": queued,
            "failed_tasks": failed,
            "open_opportunities": global_summary["open_opportunities"],
            "alert_count": len(alerts),
            "paused_channels": sum(
                int(row.get("paused_channel_count", 0))
                for row in stored_summaries.values()
            ),
        },
        "platforms": platforms,
        "alerts": alerts[:20],
        "recent_runs": recent_runs,
    }


def _compact_number(value: int | None) -> str:
    if value is None:
        return "—"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}K"
    return str(value)


def _relative_time(value: datetime | None) -> str:
    if value is None:
        return "time unavailable"
    current = datetime.now(timezone.utc)
    normalized = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    seconds = max(0, int((current - normalized.astimezone(timezone.utc)).total_seconds()))
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60} min ago"
    if seconds < 86400:
        return f"{seconds // 3600} h ago"
    return f"{seconds // 86400} d ago"


def _content_metric(item: ContentItem) -> str:
    parts: list[str] = []
    if item.metrics.views is not None:
        parts.append(f"{_compact_number(item.metrics.views)} views")
    if item.metrics.likes is not None:
        parts.append(f"{_compact_number(item.metrics.likes)} likes")
    if item.metrics.reposts is not None:
        parts.append(f"{_compact_number(item.metrics.reposts)} reposts")
    if item.metrics.replies is not None:
        parts.append(f"{_compact_number(item.metrics.replies)} replies")
    return " · ".join(parts[:3]) or "Public metrics unavailable"


def _live_hot_item(
    item: ContentItem,
    *,
    source: dict[str, object],
    following: bool,
) -> dict[str, object]:
    username = item.author.username or item.author.display_name or item.author.native_id
    author = item.author.display_name or username
    return {
        "id": item.native_id,
        "platform": "x",
        "type": "Post",
        "author": author,
        "handle": f"@{username}" if username else "Unknown account",
        "excerpt": item.text or "Public X post",
        "published": _relative_time(item.published_at),
        "retrieved": _relative_time(item.observed_at),
        "engagement": _content_metric(item),
        "velocity": "Live",
        "score": "—",
        "spark": (),
        "reason": "watchlist" if following else "live",
        "reason_label": "Watched KOL content" if following else "Live X discovery",
        "trend": "Watched account" if following else "Public X search",
        "following": following,
        "source": source,
        "url": item.canonical_url,
        "provider": item.provider,
        "can_reply": True,
    }


def _ranked_hot_item(
    item: HotContentItem,
    *,
    source: dict[str, object],
) -> dict[str, object]:
    content = item.content
    username = content.author.username or content.author.display_name or content.author.native_id
    author = content.author.display_name or username
    velocity = (
        f"+{_compact_number(round(item.velocity_per_hour))}/h"
        if item.velocity_per_hour > 0
        else "Stable"
    )
    return {
        "id": content.native_id,
        "platform": "x",
        "type": "Post",
        "author": author,
        "handle": f"@{username}" if username else "Unknown account",
        "excerpt": content.text or "Public X post",
        "published": _relative_time(content.published_at),
        "retrieved": _relative_time(content.observed_at),
        "engagement": _content_metric(content),
        "velocity": velocity,
        "score": f"{item.hot_score:.1f}",
        "spark": (20, 34, 48, 66, 82) if item.velocity_per_hour > 0 else (),
        "reason": "velocity" if item.velocity_per_hour > 0 else "engagement",
        "reason_label": item.reason,
        "trend": ", ".join(item.matched_domains) or "Relevant public content",
        "following": item.source == "following",
        "source": source,
        "url": content.canonical_url,
        "provider": content.provider,
        "can_reply": True,
    }


def _live_watch_item(
    item: ContentItem,
    *,
    source: dict[str, object],
) -> dict[str, object]:
    username = item.author.username or item.author.display_name or item.author.native_id
    author = item.author.display_name or username
    title = (item.text or "Public X post").strip().replace("\n", " ")
    if len(title) > 120:
        title = title[:117].rstrip() + "…"
    return {
        "id": item.native_id,
        "platform": "x",
        "type": "Post",
        "author": author,
        "handle": f"@{username}" if username else "Unknown account",
        "title": title,
        "body": item.text or "",
        "published": f"Published {_relative_time(item.published_at)}",
        "retrieved": f"Retrieved {_relative_time(item.observed_at)}",
        "trend": None,
        "metric": _content_metric(item),
        "visual": "discussion",
        "is_breakout": False,
        "breakout_score": "—",
        "velocity": "—",
        "velocity_points": (),
        "source": source,
        "url": item.canonical_url,
        "provider": item.provider,
    }


def _live_kol_item(
    item: AccountItem,
    *,
    domain: str,
    tone: str,
) -> dict[str, object]:
    display_name = item.display_name or item.username
    topics = tuple(value.replace("_", " ").title() for value in domain_terms(domain)[:3])
    recent = [
        {
            "title": " ".join(content.text.split())[:140] or "Public X post",
            "meta": f"{_relative_time(content.published_at)} · {_content_metric(content)}",
            "kind": "Post",
            "url": content.canonical_url,
        }
        for content in item.recent_content[:3]
    ]
    evidence = tuple(
        (
            value.summary,
            "High" if value.kind == "domain_content" else "Medium",
        )
        for value in item.evidence[:5]
    )
    newest = next(
        (content.published_at for content in item.recent_content if content.published_at),
        None,
    )
    return {
        "id": item.native_id,
        "native_id": item.native_id,
        "name": display_name,
        "handle": f"@{item.username}",
        "topics": topics or (domain,),
        "score": item.score,
        "confidence": item.confidence,
        "activity": _relative_time(newest),
        "initials": "".join(part[0] for part in display_name.split())[:2].upper() or "X",
        "tone": tone,
        "description": item.description or f"Public X account discovered for {domain}.",
        "score_label": "X Score",
        "comparison_note": "Scores are comparable only within X and this domain",
        "watchlist_label": "Add to X Watchlist",
        "metric_labels": ("Followers", "Following", "Posts", "Listed"),
        "metric_values": (
            _compact_number(item.followers_count),
            _compact_number(item.following_count),
            _compact_number(item.post_count),
            _compact_number(item.listed_count),
        ),
        "content_label": "Recent X content",
        "components": tuple(
            (component.label, component.score) for component in item.score_components
        ),
        "evidence": evidence
        or (("Stable X native account ID resolved", "Medium"),),
        "recent": recent,
        "last_synced": _relative_time(item.observed_at),
        "provider": item.provider,
        "profile_url": item.profile_url,
        "verified": item.verified,
        "score_version": item.score_version,
        "evidence_count": len(item.evidence),
    }


def _x_watch_handles(request: Request) -> tuple[str, ...]:
    configured = [
        value.strip().lstrip("@")
        for value in _settings(request).x_watch_handles.split(",")
        if value.strip()
    ]
    stored: list[str] = []
    if _settings(request).backend_url:
        try:
            response = _discover_gateway(request).tracked_accounts(
                "x", status="active", limit=50
            )
        except DiscoverGatewayError:
            response = None
        if response is not None:
            stored.extend(item.username for item in response.items)
        return tuple(dict.fromkeys([*configured, *stored]))
    try:
        rows = _platform_service(request).platform_kols("x", limit=50)
    except (KeyError, RuntimeError):
        rows = []
    for row in rows:
        if str(row.get("status") or "candidate") not in {"seed", "active"}:
            continue
        handle = str(row.get("handle") or "").strip().lstrip("@")
        if handle:
            stored.append(handle)
    return tuple(dict.fromkeys([*configured, *stored]))


def _mark_x_live(context: dict[str, Any], provider: str) -> None:
    """Project the active X provider into shared navigation state."""

    status = f"Live · {provider}"
    context["data_mode"] = f"X via {provider}"
    context["x_live_provider"] = provider
    for item in context.get("platforms", []):
        if item.get("id") == "x":
            item["status"] = status
    platform_map = context.get("platform_map", {})
    if "x" in platform_map:
        platform_map["x"]["status"] = status


def _enrich_live_trends(request: Request, context: dict[str, Any]) -> None:
    category = str(context.get("selected_category") or "all")
    response = _discover_gateway(request).trends(
        "x",
        TrendQuery(
            category=None if category == "all" else category,
            locale="global",
            limit=10,
        ),
    )
    source = dict(context["platform_map"]["x"])
    source["signals"] = [
        {
            "rank": item.rank or index,
            "name": item.name,
            "velocity": (
                f"{_compact_number(item.post_count)} posts"
                if item.post_count is not None
                else "Native rank"
            ),
            "url": item.url,
        }
        for index, item in enumerate(response.items, 1)
    ]
    context["native_signals"] = [
        source,
    ]
    themes = [
        {
            "id": item.native_key,
            "name": item.name,
            "description": item.context or "Observed in X's public trend timeline.",
            "category": item.category or (category if category != "all" else "x"),
            "state": "new",
            "state_label": "Live",
            "coverage": ("x",),
            "evidence": (
                f"{_compact_number(item.post_count)} posts"
                if item.post_count is not None
                else (f"Rank #{item.rank}" if item.rank is not None else "Observed")
            ),
            "first_seen": item.observed_at.strftime("%b %d, %H:%M"),
            "last_observed": "Just now",
            "velocity": (),
        }
        for item in response.items
    ]
    context["trend_themes"] = themes
    if themes:
        requested = context.get("requested_trend")
        context["selected_trend"] = next(
            (item for item in themes if item["id"] == requested), themes[0]
        )
        context["trend_evidence"] = [
            {
                "platform": "x",
                "time": "Now",
                "title": item.name,
                "source": f"X native trend · {response.meta.provider}",
                "source_meta": context["platform_map"]["x"],
            }
            for item in response.items[:5]
        ]
    _mark_x_live(context, response.meta.provider)
    context["x_live_warnings"] = response.meta.warnings


def _enrich_live_hot_content(request: Request, context: dict[str, Any]) -> None:
    selected_platform = str(context.get("selected_platform") or "all")
    if selected_platform not in {"x", "all"}:
        return
    source_mode = str(context.get("content_source") or "discover")
    source = context["platform_map"]["x"]
    live_items: list[dict[str, object]] = []
    provider: str | None = None
    warnings: list[str] = []

    ranked = _discover_gateway(request).hot_content(
        "x",
        source=source_mode,
        domain=(str(context.get("selected_hot_domain") or "") or None),
        limit=30,
    )
    if ranked.items:
        live_items = [_ranked_hot_item(item, source=source) for item in ranked.items]
        provider = ranked.meta.provider
        warnings.extend(ranked.meta.warnings)
        handles = _x_watch_handles(request)
        context["x_watch_handles"] = handles
        context["hot_content"] = live_items
        context["hot_summary"] = (
            ("Ranked posts", str(len(live_items)), "Noise-filtered current view"),
            (
                "Accelerating",
                str(sum(item.velocity_per_hour > 0 for item in ranked.items)),
                "Positive observed velocity",
            ),
            ("Watched accounts", str(len(handles)), "Auto and manual enrollment"),
        )
        context["hot_sort_label"] = "Ranked by velocity, engagement, recency and relevance"
        context["hot_ranking_note"] = (
            "Noise-filtered using repeat metric snapshots and domain relevance."
        )
        context["x_live_count"] = len(live_items)
        _mark_x_live(context, provider)
        context["x_live_warnings"] = tuple(dict.fromkeys(warnings))
        return

    if source_mode == "following":
        handles = _x_watch_handles(request)
        context["x_watch_handles"] = handles
        for handle in handles[:10]:
            try:
                response = _discover_gateway(request).account_content(
                    "x",
                    AccountContentQuery(
                        account_ref=handle,
                        limit=max(1, min(_settings(request).x_watch_posts_per_account, 20)),
                    ),
                )
            except DiscoverGatewayError:
                warnings.append(f"@{handle} unavailable")
                continue
            provider = provider or response.meta.provider
            warnings.extend(response.meta.warnings)
            live_items.extend(
                _live_hot_item(item, source=source, following=True)
                for item in response.items
            )
        if not handles:
            context["x_live_empty_reason"] = (
                "No X accounts are enrolled yet. Add KOLs to the X watchlist or configure "
                "KOL_X_WATCH_HANDLES."
            )
    else:
        settings = _settings(request)
        response = _discover_gateway(request).search_content(
            "x",
            ContentSearchQuery(
                query=settings.x_hot_content_query,
                sort="top",
                limit=max(1, min(settings.x_hot_content_limit, 100)),
            ),
        )
        provider = response.meta.provider
        warnings.extend(response.meta.warnings)
        live_items = [
            _live_hot_item(item, source=source, following=False)
            for item in response.items
        ]

    deduped = list({str(item["id"]): item for item in live_items}.values())
    current = list(context.get("hot_content", []))
    context["hot_content"] = deduped if selected_platform == "x" else [*deduped, *current]
    items = context["hot_content"]
    context["hot_summary"] = (
        ("Live posts", str(len(items)), "Current filtered view"),
        ("X observations", str(len(deduped)), "Normalized public content"),
        (
            "Watched accounts",
            str(len(context.get("x_watch_handles", ()))),
            "Platform-specific KOLs",
        ),
    )
    context["hot_sort_label"] = "Ranked by live public engagement"
    context["hot_ranking_note"] = (
        "Live X posts use current public metrics; velocity requires stored metric snapshots."
    )
    context["x_live_count"] = len(deduped)
    if provider:
        _mark_x_live(context, provider)
    context["x_live_warnings"] = tuple(dict.fromkeys(warnings))


def _enrich_live_watchlist(request: Request, context: dict[str, Any]) -> None:
    selected_platform = str(context.get("selected_platform") or "all")
    if selected_platform not in {"x", "all"}:
        return
    handles = _x_watch_handles(request)
    context["x_watch_handles"] = handles
    source = context["platform_map"]["x"]
    live: list[dict[str, object]] = []
    provider: str | None = None
    warnings: list[str] = []
    for handle in handles[:10]:
        try:
            response = _discover_gateway(request).account_content(
                "x",
                AccountContentQuery(
                    account_ref=handle,
                    limit=max(1, min(_settings(request).x_watch_posts_per_account, 20)),
                ),
            )
        except DiscoverGatewayError:
            warnings.append(f"@{handle} unavailable")
            continue
        provider = provider or response.meta.provider
        warnings.extend(response.meta.warnings)
        live.extend(_live_watch_item(item, source=source) for item in response.items)
    live = list({str(item["id"]): item for item in live}.values())
    if context.get("watch_mode") == "breakout":
        live = [item for item in live if item["is_breakout"]]
    current = list(context.get("content_items", []))
    context["content_items"] = live if selected_platform == "x" else [*live, *current]
    context["watchlist_summary"] = (
        ("Tracked X KOLs", str(len(handles)), "Platform accounts"),
        ("Live X posts", str(len(live)), "Current retrieval"),
        ("All content", str(len(context["content_items"])), "Current filtered view"),
        ("Provider", provider or "Not connected", "Backend connector"),
    )
    if not handles:
        context["x_live_empty_reason"] = (
            "No X accounts are enrolled yet. Add KOLs to the X watchlist or configure "
            "KOL_X_WATCH_HANDLES."
        )
    if provider:
        _mark_x_live(context, provider)
        context["source_health"] = tuple(
            (
                platform_id,
                name,
                "Just now" if platform_id == "x" else synced,
                "healthy" if platform_id == "x" else status,
            )
            for platform_id, name, synced, status in context.get("source_health", ())
        )
    context["x_live_warnings"] = tuple(dict.fromkeys(warnings))


def _enrich_live_kols(
    request: Request,
    context: dict[str, Any],
    *,
    domain: str,
    kol_id: str | None,
) -> None:
    if context.get("selected_platform") != "x":
        return
    settings = _settings(request)
    response = _discover_gateway(request).search_accounts(
        "x",
        AccountSearchQuery(
            query=domain,
            limit=max(1, min(settings.x_kol_discovery_limit, 50)),
            sample_size=max(5, min(settings.x_kol_candidate_sample_size, 100)),
            recent_posts_per_account=max(
                3, min(settings.x_kol_recent_posts_per_account, 10)
            ),
            min_engagement=max(0, min(settings.x_kol_min_engagement, 100_000)),
        ),
    )
    tones = ("violet", "blue", "mint", "amber", "rose", "slate")
    candidates = [
        _live_kol_item(item, domain=domain, tone=tones[index % len(tones)])
        for index, item in enumerate(response.items)
    ]
    selected = next(
        (item for item in candidates if item["id"] == kol_id),
        candidates[0] if candidates else None,
    )
    context.update(
        {
            "kol_domain": domain,
            "kols": candidates,
            "selected_kol": selected,
            "source_requires_live_data": not bool(candidates),
            "kol_result_total": len(candidates),
            "x_live_warnings": response.meta.warnings,
        }
    )
    _mark_x_live(context, response.meta.provider)
    if not candidates:
        context["x_live_empty_reason"] = (
            f"No evidence-backed X accounts were found for “{domain}”. "
            "Try a broader domain or lower the minimum engagement setting."
        )


def _discover_context(
    request: Request,
    *,
    view: str,
    platform: str | None = None,
    category: str = "all",
    kol_id: str | None = None,
    trend_id: str | None = None,
    content_source: str = "discover",
    watch_mode: str = "latest",
    domain: str | None = None,
) -> dict[str, object]:
    """Build the X-only product view while retaining future platform adapters."""

    context = build_mock_discover_context(
        view=view,
        platform=platform,
        category=category,
        kol_id=kol_id,
        trend_id=trend_id,
        content_source=content_source,
        watch_mode=watch_mode,
    )
    context.update(
        {
            "notice": request.query_params.get("notice"),
            "error": request.query_params.get("error"),
            "split_frontend": bool(getattr(request.app.state, "split_frontend", False)),
            "discovery_domains": parse_discovery_domains(_settings(request)),
            "selected_hot_domain": request.query_params.get("domain") or "",
        }
    )
    try:
        if view == "trends":
            _enrich_live_trends(request, context)
        elif view == "hot_content":
            _enrich_live_hot_content(request, context)
        elif view == "watchlist":
            _enrich_live_watchlist(request, context)
        elif view == "kols" and context.get("selected_platform") == "x":
            requested_domain = (domain or _settings(request).x_kol_discovery_domain).strip()
            configured_domain = next(
                (
                    item
                    for item in parse_discovery_domains(_settings(request))
                    if item.key == requested_domain
                ),
                None,
            )
            selected_domain = configured_domain.query if configured_domain else requested_domain
            context["selected_kol_domain"] = (
                configured_domain.key if configured_domain else "custom"
            )
            _enrich_live_kols(
                request,
                context,
                domain=selected_domain or "AI agents",
                kol_id=kol_id,
            )
    except DiscoverGatewayError as exc:
        context["x_live_error"] = str(exc)
    try:
        usage = _discover_gateway(request).provider_usage("x")
        context["x_provider_usage"] = usage.items[0] if usage.items else None
    except (DiscoverGatewayError, RuntimeError, KeyError):
        context["x_provider_usage"] = None
    return context


@app.get("/", response_class=HTMLResponse)
def discover_home(
    request: Request,
    category: str = "all",
) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="discover.html",
        context=_discover_context(request, view="trends", category=category),
    )


@app.get("/trends", response_class=HTMLResponse)
def trend_radar(
    request: Request,
    category: str = "all",
    trend: str | None = None,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="discover.html",
        context=_discover_context(
            request,
            view="trends",
            category=category,
            trend_id=trend,
        ),
    )


@app.get("/hot-content", response_class=HTMLResponse)
def hot_content(
    request: Request,
    platform: str = "x",
    source: str = "discover",
    domain: str | None = None,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="discover.html",
        context=_discover_context(
            request,
            view="hot_content",
            platform=platform,
            content_source=source,
        ),
    )


@app.post("/hot-content/reply-draft")
def generate_hot_content_reply(
    request: Request,
    platform: Annotated[str, Form()] = "x",
    content_id: Annotated[str, Form()] = "",
    tone: Annotated[str, Form()] = "thoughtful",
    language: Annotated[str, Form()] = "auto",
) -> JSONResponse:
    if platform != "x" or not content_id.strip():
        return JSONResponse({"detail": "Invalid X content reference."}, status_code=400)
    try:
        payload = ReplyDraftRequest(tone=tone, language=language)
        result = _discover_gateway(request).reply_draft(platform, content_id, payload)
    except (DiscoverGatewayError, ValueError) as exc:
        return JSONResponse({"detail": str(exc)}, status_code=503)
    return JSONResponse(result.model_dump(mode="json"))


@app.get("/kols", response_class=HTMLResponse)
def kol_discover(
    request: Request,
    platform: str = "x",
    kol: str | None = None,
    domain: str | None = None,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="discover.html",
        context=_discover_context(
            request,
            view="kols",
            platform=platform,
            kol_id=kol,
            domain=domain,
        ),
    )


@app.get("/watchlist", response_class=HTMLResponse)
def kol_watchlist(
    request: Request,
    platform: str = "x",
    mode: str = "latest",
) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="discover.html",
        context=_discover_context(
            request,
            view="watchlist",
            platform=platform,
            watch_mode=mode,
        ),
    )


@app.post("/kols/watchlist")
def update_kol_watchlist(
    request: Request,
    platform: Annotated[str, Form()],
    native_id: Annotated[str, Form()],
    domain: Annotated[str, Form()] = "",
) -> RedirectResponse:
    try:
        _discover_gateway(request).set_account_tracking(
            platform, native_id, enabled=True
        )
    except DiscoverGatewayError as exc:
        return RedirectResponse(
            url=f"/kols?{urlencode({'platform': platform, 'domain': domain, 'error': str(exc)})}",
            status_code=303,
        )
    return RedirectResponse(
        url=f"/watchlist?{urlencode({'platform': platform, 'notice': 'KOL added to watchlist'})}",
        status_code=303,
    )


@app.post("/discover/trends")
def discover_trends(
    request: Request,
    platform_id: Annotated[str, Form()] = "x",
    limit: Annotated[int, Form()] = 20,
) -> RedirectResponse:
    try:
        job_id = _platform_service(request).enqueue_trend_discovery(
            platform_id, limit=limit
        )
        job = _platform_service(request).execute_job_now(job_id)
    except (KeyError, ValueError, RuntimeError) as exc:
        return RedirectResponse(
            url=f"/trends?{urlencode({'error': str(exc)})}#trends", status_code=303
        )
    result = job.get("result") or {}
    count = int(result.get("trends") or 0)
    return RedirectResponse(
        url=f"/trends?{urlencode({'notice': f'已发现 {count} 个原生趋势'})}#trends",
        status_code=303,
    )


@app.post("/discover/kols")
def discover_kols(
    request: Request,
    query: Annotated[str, Form()],
    platform_id: Annotated[str, Form()] = "x",
    source: Annotated[str, Form()] = "content",
    limit: Annotated[int, Form()] = 20,
) -> RedirectResponse:
    try:
        job_id = _platform_service(request).enqueue_discovery(
            platform_id,
            query=query,
            source=source,
            limit=limit,
            manual=True,
        )
        job = _platform_service(request).execute_job_now(job_id)
    except (KeyError, ValueError, RuntimeError) as exc:
        return RedirectResponse(
            url=(
                f"/kols?{urlencode({'platform': platform_id, 'error': str(exc)})}#kols"
            ),
            status_code=303,
        )
    count = len(_platform_service(request).platform_kols(platform_id, limit=500))
    return RedirectResponse(
        url=(
            f"/kols?{urlencode({'platform': platform_id, 'notice': f'本次发现并评估了 {count} 个账号'})}#kols"
        ),
        status_code=303,
    )




@app.get("/health", response_class=JSONResponse)
def health(request: Request) -> dict[str, object]:
    if getattr(request.app.state, "split_frontend", False):
        return {
            "status": "ok",
            "service": "trend-kol-discover-frontend",
            "mode": "remote-backend",
            "execution": "request-scoped",
        }
    return {
        "status": "ok",
        "database": "postgresql" if _settings(request).database_url else "sqlite",
        "platforms": list(request.app.state.platform_registry.platform_ids),
        "automation": _automation(request).get_global_summary(),
        "execution": "request-scoped",
    }


@app.get("/platforms", response_class=HTMLResponse)
def platform_center(request: Request) -> Response:
    if getattr(request.app.state, "split_frontend", False):
        return RedirectResponse(url="/trends", status_code=303)
    return templates.TemplateResponse(
        request=request,
        name="platform_center.html",
        context=_platform_center_context(request),
    )


def _split_values(value: Any) -> list[str]:
    return list(
        dict.fromkeys(
            item.strip()
            for item in str(value or "").replace(",", "\n").splitlines()
            if item.strip()
        )
    )


def _agent_config_form(current: dict[str, Any], form: Any) -> dict[str, Any]:
    output = dict(current)
    strings = {
        "role", "audience", "tone", "primary_language", "system_prompt", "model",
        "reasoning_effort", "active_start", "active_end", "timezone",
    }
    lists = {
        "expertise", "keywords", "priority_accounts", "excluded_accounts",
        "approved_domains", "original_post_times",
    }
    integers = {
        "max_output_tokens", "request_timeout_seconds", "model_retries",
        "scan_interval_minutes", "candidates_per_scan", "replies_per_scan",
        "post_freshness_hours", "reply_hourly_limit", "reply_daily_limit",
        "original_posts_daily", "author_cooldown_days", "conversation_turn_limit",
        "failure_pause_threshold", "reply_delay_min_minutes", "reply_delay_max_minutes",
    }
    floats = {"temperature", "score_threshold"}
    for key in strings:
        if key in form:
            output[key] = str(form.get(key) or "").strip()
    for key in lists:
        if key in form:
            output[key] = _split_values(form.get(key))
    for key in integers:
        if key in form:
            output[key] = int(str(form.get(key) or "0"))
    for key in floats:
        if key in form:
            output[key] = float(str(form.get(key) or "0"))
    if "inherit_global_operations_present" in form:
        output["inherit_global_operations"] = str(
            form.get("inherit_global_operations") or ""
        ).lower() in {"1", "true", "on"}
    return output


@app.get("/agents", response_class=HTMLResponse)
def agent_center(request: Request, archived: bool = False) -> HTMLResponse:
    store = _agent_store(request)
    connections = _automation(request).list_platform_connections(enabled_only=True)
    bound = {
        int(agent[key])
        for agent in store.list_agents(include_archived=True)
        for key in ("reply_connection_id", "publish_connection_id")
    }
    reply_connections = [
        row for row in connections
        if int(row["id"]) not in bound and "comment" in row.get("capabilities", ())
    ]
    publish_connections = [
        row for row in connections
        if int(row["id"]) not in bound and "owned_publish" in row.get("capabilities", ())
    ]
    runtime = getattr(request.app.state, "agent_runtime", None)
    runtime_status = store.latest_runtime_status() or {}
    return templates.TemplateResponse(
        request=request,
        name="agents.html",
        context={
            "agents": store.list_agents(include_archived=archived),
            "reply_connections": reply_connections,
            "publish_connections": publish_connections,
            "defaults": store.get_defaults(),
            "show_archived": archived,
            "runtime_ready": bool(
                (runtime and runtime.ready) or runtime_status.get("model_ready")
            ),
            "runtime_status": runtime_status,
            "worker": _worker_status(_automation(request)),
        },
    )


@app.post("/agents")
async def create_agent(request: Request) -> RedirectResponse:
    form = await request.form()
    defaults = _agent_store(request).get_defaults()
    config = _agent_config_form(defaults, form)
    try:
        agent_id = _agent_store(request).create_agent(
            name=str(form.get("name") or "").strip(),
            platform_id=str(form.get("platform_id") or "x"),
            native_account_id=str(form.get("native_account_id") or "").strip(),
            native_username=str(form.get("native_username") or "").strip(),
            reply_connection_id=int(str(form.get("reply_connection_id") or "0")),
            publish_connection_id=int(str(form.get("publish_connection_id") or "0")),
            config=config,
            actor=_actor(request),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return RedirectResponse(url=f"/agents/{agent_id}?tab=config", status_code=303)


@app.post("/agents/defaults")
async def update_agent_defaults(request: Request) -> RedirectResponse:
    store = _agent_store(request)
    form = await request.form()
    try:
        store.save_defaults(_agent_config_form(store.get_defaults(), form))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return RedirectResponse(url="/agents#agent-defaults", status_code=303)


@app.get("/agents/{agent_id}", response_class=HTMLResponse)
def agent_detail(
    request: Request,
    agent_id: int,
    tab: str = "activity",
) -> HTMLResponse:
    store = _agent_store(request)
    agent = store.get_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent 不存在")
    if tab not in {"activity", "status", "config"}:
        tab = "activity"
    config = agent["config"]
    now = datetime.now(timezone.utc)
    day_start = now.astimezone(ZoneInfo(str(config["timezone"]))).replace(
        hour=0, minute=0, second=0, microsecond=0
    ).astimezone(timezone.utc).isoformat()
    hour_start = (now - timedelta(hours=1)).isoformat()
    with ThreadPoolExecutor(max_workers=5) as executor:
        counts_future = executor.submit(
            store.usage_counts,
            agent_id,
            day_start=day_start,
            hour_start=hour_start,
        )
        timeline_future = (
            executor.submit(store.timeline, agent_id, limit=30)
            if tab == "activity"
            else None
        )
        events_future = (
            executor.submit(store.recent_events, agent_id, limit=30)
            if tab == "status"
            else None
        )
        performance_future = executor.submit(store.metric_summary, agent_id)
        runtime_future = executor.submit(store.latest_runtime_status)
        worker_future = executor.submit(_worker_status, _automation(request))
        counts = counts_future.result()
        activity = timeline_future.result() if timeline_future else []
        events = events_future.result() if events_future else []
        performance = performance_future.result()
        runtime_status = runtime_future.result() or {}
        worker = worker_future.result()
    replies_today = counts["replies_today"]
    replies_hour = counts["replies_hour"]
    posts_today = counts["posts_today"]
    runtime = getattr(request.app.state, "agent_runtime", None)
    return templates.TemplateResponse(
        request=request,
        name="agent_detail.html",
        context={
            "agent": agent,
            "config": config,
            "tab": tab,
            "activity": activity,
            "events": events,
            "has_more": len(activity) == 30,
            "usage": {
                "replies_today": replies_today,
                "reply_daily_remaining": max(0, int(config["reply_daily_limit"]) - replies_today),
                "reply_hour_remaining": max(0, int(config["reply_hourly_limit"]) - replies_hour),
                "posts_today": posts_today,
                "post_remaining": max(0, int(config["original_posts_daily"]) - posts_today),
            },
            "performance": performance,
            "worker": worker,
            "runtime_ready": bool(
                (runtime and runtime.ready) or runtime_status.get("model_ready")
            ),
            "runtime_status": runtime_status,
        },
    )


@app.get("/agents/{agent_id}/activity", response_class=HTMLResponse)
def agent_activity_fragment(
    request: Request,
    agent_id: int,
    before_created_at: str | None = None,
    before_id: int | None = None,
) -> HTMLResponse:
    if not _agent_store(request).get_agent(agent_id):
        raise HTTPException(status_code=404, detail="Agent 不存在")
    activity = _agent_store(request).timeline(
        agent_id,
        before_created_at=before_created_at,
        before_id=before_id,
        limit=30,
    )
    return templates.TemplateResponse(
        request=request,
        name="agent_activity_fragment.html",
        context={"agent_id": agent_id, "activity": activity, "has_more": len(activity) == 30},
    )


@app.post("/agents/{agent_id}/status")
async def update_agent_status(request: Request, agent_id: int) -> RedirectResponse:
    form = await request.form()
    requested = str(form.get("status") or "")
    mapping = {
        "enable": "running",
        "pause": "paused",
        "archive": "archived",
        "restore": "paused",
    }
    if requested not in mapping:
        raise HTTPException(status_code=422, detail="未知 Agent 状态操作")
    if requested == "enable":
        store = _agent_store(request)
        agent = store.get_agent(agent_id)
        runtime_status = store.latest_runtime_status() or {}
        worker = _worker_status(_automation(request))
        blockers: list[str] = []
        if not worker["online"]:
            blockers.append("Mac Worker 离线")
        if not runtime_status.get("model_ready"):
            blockers.append("Mac 模型配置不可用")
        if not runtime_status.get("write_ready"):
            blockers.append("Mac Worker 未启用真实写入")
        if agent and agent.get("reply_connection_status") != "connected":
            blockers.append("回复连接不可用")
        if agent and agent.get("publish_connection_status") != "connected":
            blockers.append("Postiz 发帖连接不可用")
        if agent and agent["config"]["model"] not in runtime_status.get("allowed_models", ()):
            blockers.append("Agent 模型不在 Mac 允许列表中")
        if blockers:
            raise HTTPException(status_code=422, detail="；".join(blockers))
    try:
        _agent_store(request).set_status(
            agent_id,
            mapping[requested],  # type: ignore[arg-type]
            actor=_actor(request),
            reason=str(form.get("reason") or "").strip(),
        )
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return RedirectResponse(url=f"/agents/{agent_id}", status_code=303)


@app.post("/agents/{agent_id}/commands/{command_type}")
def queue_agent_command(
    request: Request,
    agent_id: int,
    command_type: str,
) -> RedirectResponse:
    try:
        _agent_store(request).queue_command(agent_id, command_type, actor=_actor(request))
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    request.app.state.platform_worker.notify()
    return RedirectResponse(url=f"/agents/{agent_id}?tab=status", status_code=303)


@app.post("/agents/{agent_id}/config")
async def update_agent_config(request: Request, agent_id: int) -> RedirectResponse:
    store = _agent_store(request)
    agent = store.get_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent 不存在")
    form = await request.form()
    try:
        store.save_config(
            agent_id,
            _agent_config_form(agent["config"], form),
            actor=_actor(request),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return RedirectResponse(url=f"/agents/{agent_id}?tab=config", status_code=303)




@app.post("/automation/controls/kill-switch")
def update_automation_kill_switch(
    request: Request,
    enabled: Annotated[str, Form()] = "true",
    scope: Annotated[str | None, Form()] = None,
    platform_id: Annotated[str | None, Form()] = None,
    connection_id: Annotated[int | None, Form()] = None,
    action_type: Annotated[str | None, Form()] = None,
) -> RedirectResponse:
    paused = enabled.lower() == "true"
    resolved_scope = scope or ("platform" if platform_id else "global")
    try:
        if resolved_scope == "global":
            _automation(request).set_kill_switch(
                "global",
                paused,
                reason="管理员操作",
                updated_by=_actor(request),
            )
            target = "/settings/brand#automation-controls"
        elif resolved_scope == "platform":
            if not platform_id or platform_id not in request.app.state.platform_registry:
                raise HTTPException(status_code=404, detail="平台未注册")
            _automation(request).set_kill_switch(
                "platform",
                paused,
                platform_id=platform_id,
                reason="管理员操作",
                updated_by=_actor(request),
            )
            target = "/settings/brand#automation-controls"
        elif resolved_scope == "account":
            if not platform_id or connection_id is None:
                raise ValueError("账号级控制需要 platform_id 和 connection_id")
            if platform_id not in request.app.state.platform_registry:
                raise HTTPException(status_code=404, detail="平台未注册")
            _automation(request).set_kill_switch(
                "account",
                paused,
                platform_id=platform_id,
                connection_id=connection_id,
                reason="管理员操作",
                updated_by=_actor(request),
            )
            target = "/settings/brand#automation-controls"
        elif resolved_scope == "action_type":
            if not action_type:
                raise ValueError("动作类型控制需要 action_type")
            if platform_id and platform_id not in request.app.state.platform_registry:
                raise HTTPException(status_code=404, detail="平台未注册")
            _automation(request).set_kill_switch(
                "action_type",
                paused,
                action_type=action_type,
                reason="管理员操作",
                updated_by=_actor(request),
            )
            target = "/settings/brand#automation-controls"
        else:
            raise ValueError(f"未知 Kill Switch 级别：{resolved_scope}")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return RedirectResponse(url=target, status_code=303)


@app.post("/automation/actions/{action_id}/approve")
def approve_automation_action(
    request: Request,
    action_id: int,
    final_text: Annotated[str | None, Form()] = None,
) -> RedirectResponse:
    action = _automation(request).get_action(action_id)
    if not action:
        raise HTTPException(status_code=404, detail="动作不存在")
    try:
        _platform_service(request).approve_action(
            action_id,
            actor=_actor(request),
            final_text=final_text,
        )
    except (KeyError, ValueError, StateTransitionError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return RedirectResponse(
        url=f"/platforms/{action['platform_id']}#automation", status_code=303
    )


@app.post("/automation/opportunities/{opportunity_id}/dismiss")
def dismiss_automation_opportunity(
    request: Request, opportunity_id: int
) -> RedirectResponse:
    opportunity = _automation(request).get_opportunity(opportunity_id)
    if not opportunity:
        raise HTTPException(status_code=404, detail="机会不存在")
    try:
        _automation(request).transition_opportunity(
            opportunity_id,
            "dismissed",
            actor=_actor(request),
        )
    except StateTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return RedirectResponse(
        url=f"/platforms/{opportunity['platform_id']}#inbox", status_code=303
    )


@app.post("/automation/actions/{action_id}/cancel")
def cancel_automation_action(request: Request, action_id: int) -> RedirectResponse:
    action = _automation(request).get_action(action_id)
    if not action:
        raise HTTPException(status_code=404, detail="动作不存在")
    try:
        _automation(request).transition_action(
            action_id,
            ActionStatus.CANCELLED,
            actor=_actor(request),
            error="管理员取消",
        )
    except StateTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return RedirectResponse(
        url=f"/platforms/{action['platform_id']}#automation", status_code=303
    )


@app.post("/automation/actions/{action_id}/resolve")
def resolve_automation_action(
    request: Request,
    action_id: int,
    resolution: Annotated[str, Form()],
    receipt_url: Annotated[str | None, Form()] = None,
    note: Annotated[str | None, Form()] = None,
) -> RedirectResponse:
    action = _automation(request).get_action(action_id)
    if not action:
        raise HTTPException(status_code=404, detail="动作不存在")
    if action["status"] != ActionStatus.CONFIRMATION_REQUIRED.value:
        raise HTTPException(status_code=409, detail="只有待确认动作可以人工结案")
    try:
        if resolution == ActionStatus.CANCELLED.value:
            _automation(request).transition_action(
                action_id,
                ActionStatus.CANCELLED,
                actor=_actor(request),
                error=(note or "管理员取消").strip(),
            )
        elif resolution in {
            ActionStatus.SUCCEEDED.value,
            ActionStatus.FAILED.value,
        }:
            succeeded = resolution == ActionStatus.SUCCEEDED.value
            _automation(request).record_action_result(
                action_id,
                success=succeeded,
                confirmed=succeeded,
                receipt_url=(receipt_url or "").strip() or None,
                error=None if succeeded else (note or "人工确认写入失败").strip(),
                actor=_actor(request),
            )
        else:
            raise ValueError("无效的结案状态")
    except (ValueError, StateTransitionError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return RedirectResponse(
        url=f"/platforms/{action['platform_id']}#automation", status_code=303
    )


def _textarea_lines(value: str) -> list[str]:
    return [line.strip() for line in value.splitlines() if line.strip()]


@app.get("/settings/brand", response_class=HTMLResponse)
def brand_settings(request: Request) -> HTMLResponse:
    automation = _automation(request)
    brand = automation.get_brand_config()
    handles = brand.get("platform_handles") or {}
    manifests = []
    paused_control_keys = {
        row["control_key"] for row in automation.list_channel_controls(state="paused")
    }
    supported_action_types: set[str] = set()
    for manifest in request.app.state.platform_registry.list_manifests():
        manifest_row = manifest.as_dict()
        capabilities = set(manifest_row.get("capabilities") or [])
        for capability, action_type in (
            ("comment", "comment"),
            ("dm", "dm"),
            ("owned_publish", "owned_post"),
        ):
            if capability in capabilities:
                supported_action_types.add(action_type)
        accounts = []
        for connection in automation.list_platform_connections(
            platform_id=manifest.platform_id
        ):
            metadata = connection.get("metadata") or {}
            if metadata.get("kind") not in {"managed_account", "postiz"}:
                continue
            accounts.append(
                {
                    "connection_id": connection["id"],
                    "display_name": connection.get("display_name")
                    or metadata.get("username")
                    or metadata.get("external_account_id")
                    or metadata.get("integration_id")
                    or f"账号 #{connection['id']}",
                    "roles": [
                        CAPABILITY_LABELS.get(role, role)
                        for role in connection.get("capabilities") or []
                    ],
                    "kind": metadata.get("kind"),
                    "kind_label": (
                        "Browser" if metadata.get("kind") == "managed_account" else "Postiz"
                    ),
                    "identifier": metadata.get("external_account_id")
                    or metadata.get("integration_id"),
                    "browser_profile": metadata.get("browser_profile"),
                    "is_default": bool(metadata.get("is_default")),
                    "status": connection.get("status"),
                    "paused": (
                        f"account:{manifest.platform_id}:{connection['id']}"
                        in paused_control_keys
                    ),
                }
            )
        manifests.append(
            {
                **manifest_row,
                "paused": f"platform:{manifest.platform_id}" in paused_control_keys,
                "accounts": accounts,
            }
        )
    return templates.TemplateResponse(
        request=request,
        name="brand_settings.html",
        context={
            "brand": brand,
            "platforms": manifests,
            "platform_manifests": manifests,
            "platform_handles": handles,
            "automation_controls": {
                "global_paused": "global" in paused_control_keys,
                "action_types": [
                    {
                        "action_type": action_type,
                        "label": ACTION_LABELS[action_type],
                        "paused": f"action_type:{action_type}" in paused_control_keys,
                    }
                    for action_type in ("comment", "dm", "owned_post")
                    if action_type in supported_action_types
                ],
                "platforms": manifests,
            },
        },
    )


@app.post("/settings/brand")
async def update_brand_settings(request: Request) -> RedirectResponse:
    form = await request.form()
    brand_name = str(form.get("brand_name") or "")
    if not brand_name.strip():
        raise HTTPException(status_code=422, detail="品牌名称不能为空")
    platform_handles = {
        manifest.platform_id: str(
            form.get(f"platform_handle_{manifest.platform_id}") or ""
        )
        .strip()
        .lstrip("@")
        for manifest in request.app.state.platform_registry.list_manifests()
    }
    _automation(request).save_brand_config(
        brand_name=brand_name,
        description=str(form.get("description") or ""),
        audience=str(form.get("audience") or ""),
        tone=str(
            form.get("tone") or "professional, concise, conversational"
        ),
        product_url=str(form.get("product_url") or ""),
        platform_handles=platform_handles,
        allowed_claims=_textarea_lines(str(form.get("allowed_claims") or "")),
        forbidden_terms=_textarea_lines(str(form.get("forbidden_terms") or "")),
        approved_domains=_textarea_lines(str(form.get("approved_domains") or "")),
    )
    return RedirectResponse(url="/settings/brand?saved=1", status_code=303)
