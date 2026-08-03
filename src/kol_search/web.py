from __future__ import annotations

import hashlib
import hmac
import secrets
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from kol_search.automation import (
    ActionStatus,
    AgentStore,
    AutomationStore,
    StateTransitionError,
)
from kol_search.agent_runtime import AgentRuntimeService
from kol_search.automation.agents import AgentConfig
from kol_search.automation.service import (
    AutomationWorker,
    PlatformAutomationScheduler,
    PlatformAutomationService,
)
from kol_search.platforms import (
    PlatformCapability,
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
    """Render stored ISO timestamps in the Agent's operating timezone."""
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


class _QueueNotifier:
    def notify(self) -> None:
        """Web-only runtimes persist work for the external worker to poll."""


class ReplyDraftRequest(BaseModel):
    """Platform-neutral input for an interactive, reviewable reply draft."""

    agent_id: int = Field(gt=0)
    object_id: str = Field(min_length=1, max_length=200)
    author_id: str = ""
    author_username: str = ""
    text: str = Field(min_length=1, max_length=4000)
    url: str | None = None
    trend: str = ""
    language: str | None = None
    metrics: dict[str, int] = Field(default_factory=dict)
    manual_context: str = Field(default="", max_length=2400)

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

    database_target = settings.database_target()
    automation = AutomationStore(database_target)
    automation.database.wait_ready()
    registry = build_default_registry(settings)
    install_registered_platform_schemas(database_target, registry)
    _install_platform_routers(application, registry)

    # PlatformAutomationService owns shared orchestration only. Platform-native
    # repositories resolve their database directly from Settings.
    platform_service = PlatformAutomationService(automation, registry, settings)
    agent_store = AgentStore(automation)
    agent_runtime: AgentRuntimeService | None = None
    if settings.runtime_mode == "web":
        platform_service.initialize_defaults()
        platform_worker: AutomationWorker | _QueueNotifier = _QueueNotifier()
        scheduler: PlatformAutomationScheduler | None = None
    else:
        platform_service.initialize_connections()
        platform_worker = AutomationWorker(automation, platform_service)
        agent_runtime = AgentRuntimeService(automation, registry, settings)
        scheduler = PlatformAutomationScheduler(
            platform_service, platform_worker, settings, agent_runtime
        )

    application.state.settings = settings
    application.state.automation = automation
    application.state.platform_registry = registry
    application.state.platform_service = platform_service
    application.state.platform_worker = platform_worker
    application.state.agent_store = agent_store
    application.state.agent_runtime = agent_runtime
    application.state.scheduler = scheduler

    if isinstance(platform_worker, AutomationWorker):
        platform_worker.start()
    if scheduler is not None:
        scheduler.start()
    try:
        yield
    finally:
        if scheduler is not None:
            scheduler.stop()
        if isinstance(platform_worker, AutomationWorker):
            platform_worker.stop()
        registry.close()
        automation.database.close()


app = FastAPI(title="KOL Growth OS", version="1.0.0", lifespan=lifespan)
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
    return request.app.state.automation


def _platform_service(request: Request) -> PlatformAutomationService:
    return request.app.state.platform_service


def _agent_store(request: Request) -> AgentStore:
    return request.app.state.agent_store


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
    auth_enabled = bool(settings.admin_password)
    public_path = request.url.path in {"/health", "/login"} or request.url.path.startswith(
        "/static/"
    )
    if auth_enabled and not public_path:
        supplied = request.cookies.get("kol_admin_session", "")
        if not secrets.compare_digest(supplied, _session_token(settings)):
            return RedirectResponse(url="/login", status_code=303)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            if origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
                return JSONResponse({"detail": "CSRF origin rejected"}, status_code=403)
    if (
        settings.web_read_only
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


@app.post("/api/v1/platforms/{platform_id}/reply-draft")
def generate_reply_draft(
    request: Request,
    platform_id: str,
    payload: ReplyDraftRequest,
) -> dict[str, Any]:
    """Generate a single reviewable reply without executing a platform write."""

    normalized_platform = platform_id.strip().lower()
    registry = request.app.state.platform_registry
    plugin = registry.get(normalized_platform)
    if plugin is None:
        raise HTTPException(status_code=404, detail="平台未注册")
    if not plugin.manifest.supports(PlatformCapability.COMMENT):
        raise HTTPException(status_code=422, detail="平台暂不支持评论生成")

    runtime = getattr(request.app.state, "agent_runtime", None)
    if runtime is None or not runtime.ready or runtime.models is None:
        raise HTTPException(
            status_code=503,
            detail="AI 回复生成服务未就绪；请确认请求由带模型配置的 worker 处理",
        )

    agent = _agent_store(request).get_agent(payload.agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent 不存在")
    if agent["platform_id"] != normalized_platform:
        raise HTTPException(status_code=422, detail="Agent 与目标平台不匹配")
    if agent["status"] == "archived":
        raise HTTPException(status_code=422, detail="已归档 Agent 不能生成回复")

    config = AgentConfig.model_validate(agent["config"])
    candidate = payload.model_dump(exclude={"agent_id", "manual_context"})
    try:
        generated = runtime.models.generate_reply(
            config,
            candidate,
            manual_context=payload.manual_context,
            platform_id=normalized_platform,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    return {
        "platform_id": normalized_platform,
        "agent_id": payload.agent_id,
        "object_id": generated.object_id,
        "safe": generated.safe,
        "score": generated.score,
        "reply": generated.reply,
        "rationale": generated.rationale,
        "safety_reason": generated.safety_reason,
        "reply_type": generated.reply_type,
        "follow_up_content_idea": generated.follow_up_content_idea,
        "target_url": payload.url,
        # The platform-specific connector can later replace this with a native
        # composer URL; returning the target now keeps manual review useful.
        "reply_url": payload.url,
    }


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
    worker = _worker_status(automation)
    if not worker["online"]:
        alerts.append(
            {
                "severity": "critical",
                "title": "Mac Worker 离线",
                "message": "云端任务会继续排队，但不会执行平台读取、私信或评论。",
                "platform_name": "Worker",
                "created_at": str(worker.get("last_seen_at") or "尚无心跳"),
                "href": "/health",
            }
        )

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
    discovery_url = next(
        (
            f"{row['workspace_url']}#discover"
            for row in platforms
            if row["enabled"]
            and any(
                capability in row["capabilities"]
                for capability in ("account_search", "content_search", "relations")
            )
        ),
        "/platforms",
    )
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
        "worker": worker,
    }




@app.get("/health", response_class=JSONResponse)
def health(request: Request) -> dict[str, object]:
    return {
        "status": "ok",
        "database": "postgresql" if _settings(request).database_url else "sqlite",
        "platforms": list(request.app.state.platform_registry.platform_ids),
        "automation": _automation(request).get_global_summary(),
        "worker": _worker_status(_automation(request)),
    }


@app.get("/", response_class=HTMLResponse)
@app.get("/platforms", response_class=HTMLResponse)
def platform_center(request: Request) -> HTMLResponse:
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
            blockers.append("OpenCLI 回复连接不可用")
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
    request.app.state.platform_worker.notify()
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
