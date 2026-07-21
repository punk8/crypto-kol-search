from __future__ import annotations

import hashlib
import hmac
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from kol_search.automation import (
    ActionStatus,
    AutomationStore,
    StateTransitionError,
)
from kol_search.automation.service import (
    AutomationWorker,
    PlatformAutomationScheduler,
    PlatformAutomationService,
)
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

@asynccontextmanager
async def lifespan(application: FastAPI):
    settings = get_settings()
    if settings.web_host not in {"127.0.0.1", "localhost", "::1"} and not (
        settings.admin_password and settings.session_secret
    ):
        raise RuntimeError(
            "非本机监听必须同时配置 KOL_ADMIN_PASSWORD 和 KOL_SESSION_SECRET"
        )

    automation = AutomationStore(settings.db_path())
    registry = build_default_registry(settings)
    install_registered_platform_schemas(settings.db_path(), registry)
    _install_platform_routers(application, registry)

    # PlatformAutomationService owns shared orchestration only. Platform-native
    # repositories resolve their database directly from Settings.
    platform_service = PlatformAutomationService(automation, registry, settings)
    platform_service.initialize_connections()
    platform_worker = AutomationWorker(automation, platform_service)
    scheduler = PlatformAutomationScheduler(platform_service, platform_worker, settings)

    application.state.settings = settings
    application.state.automation = automation
    application.state.platform_registry = registry
    application.state.platform_service = platform_service
    application.state.platform_worker = platform_worker
    application.state.scheduler = scheduler

    platform_worker.start()
    scheduler.start()
    try:
        yield
    finally:
        scheduler.stop()
        platform_worker.stop()
        registry.close()


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
    }




@app.get("/health", response_class=JSONResponse)
def health(request: Request) -> dict[str, object]:
    return {
        "status": "ok",
        "database": str(_settings(request).db_path()),
        "platforms": list(request.app.state.platform_registry.platform_ids),
        "automation": _automation(request).get_global_summary(),
    }


@app.get("/", response_class=HTMLResponse)
@app.get("/platforms", response_class=HTMLResponse)
def platform_center(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="platform_center.html",
        context=_platform_center_context(request),
    )




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
