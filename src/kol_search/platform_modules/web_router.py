from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from kol_search.automation import ActionType, AutomationStore
from kol_search.automation.service import PlatformAutomationService
from kol_search.platforms.kernel import PlatformCapability, PlatformManifest
from kol_search.settings import Settings


PACKAGE_DIR = Path(__file__).resolve().parents[1]
templates = Jinja2Templates(directory=str(PACKAGE_DIR / "templates"))

CAPABILITY_LABELS = {
    "account_search": "账号搜索",
    "content_search": "内容搜索",
    "timeline/feed": "时间线 / Feed",
    "relations": "关系发现",
    "native_trends": "原生趋势",
    "comment": "评论",
    "dm": "私信",
    "owned_publish": "自有发布",
    "media_upload": "媒体上传",
    "analytics": "效果分析",
}

JOB_LABELS = {
    "manual_discovery": "手动发现",
    "discovery_refresh": "持续发现",
    "signal_refresh": "信号扫描",
    "dispatch_actions": "动作调度",
    "refresh_outcomes": "结果回收",
}

JOB_STATUS_LABELS = {
    "queued": "等待执行",
    "running": "执行中",
    "succeeded": "已完成",
    "failed": "失败",
    "cancelled": "已取消",
}

JOB_PHASE_LABELS = {
    "queued": "已进入队列",
    "starting": "正在启动",
    "discovering": "正在读取平台数据",
    "projecting": "正在保存与评分",
    "scanning": "正在扫描信号",
    "building_opportunities": "正在生成机会",
    "dispatching": "正在调度动作",
    "refreshing": "正在回收结果",
    "done": "处理完成",
    "failed": "处理失败",
    "cancelled": "任务已取消",
}

ACTION_LABELS = {
    ActionType.COMMENT.value: "评论",
    ActionType.DM.value: "私信",
    ActionType.OWNED_POST.value: "自有发布",
}


def platform_workspace_template(manifest: PlatformManifest) -> str:
    """Resolve a platform-owned workspace template with a generic fallback."""

    configured = manifest.metadata.get("workspace_template")
    if isinstance(configured, str) and configured.strip():
        return configured.strip()
    return "platform_workspace.html"


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _automation(request: Request) -> AutomationStore:
    return request.app.state.automation


def _platform_service(request: Request) -> PlatformAutomationService:
    return request.app.state.platform_service


def _connection_status(value: str | None) -> str:
    return "disabled" if value == "paused" else (value or "disconnected")


def _local_day_start_utc(settings: Settings) -> str:
    local = datetime.now(ZoneInfo(settings.timezone))
    return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(
        timezone.utc
    ).isoformat()


def _workspace_path(manifest: PlatformManifest) -> str:
    return manifest.workbench_path or f"/platforms/{manifest.platform_id}"


def _recent_run_rows(
    jobs: list[dict[str, Any]], runs_path: str, *, limit: int = 10
) -> list[dict[str, Any]]:
    return [
        {
            **row,
            "label": JOB_LABELS.get(row["job_type"], row["job_type"]),
            "status_label": JOB_STATUS_LABELS.get(row["status"], row["status"]),
            "phase_label": JOB_PHASE_LABELS.get(
                str(row.get("phase") or ""), str(row.get("phase") or "")
            ),
            "detail_url": f"{runs_path}#job-{row['id']}",
        }
        for row in jobs[:limit]
    ]


def _workspace_feedback(request: Request, jobs: list[dict[str, Any]]) -> dict[str, str] | None:
    queued_kind = "discovery" if request.query_params.get("discovery") == "queued" else (
        "scan" if request.query_params.get("scan") == "queued" else None
    )
    if queued_kind is None:
        return None
    requested_id = request.query_params.get("job_id")
    job = next(
        (
            row
            for row in jobs
            if (not requested_id or str(row["id"]) == requested_id)
            and (
                row["job_type"] in {"manual_discovery", "discovery_refresh"}
                if queued_kind == "discovery"
                else row["job_type"] == "signal_refresh"
            )
        ),
        None,
    )
    label = "发现" if queued_kind == "discovery" else "信号扫描"
    if job is None:
        return {"title": f"{label}任务已提交", "message": "任务状态会在下方自动更新。"}
    return {
        "title": f"{label}任务 #{job['id']} 已提交",
        "message": "已进入后台队列；任务进度会在下方自动更新，期间可以继续浏览其他页面。",
    }


def platform_workspace_context(
    request: Request, manifest: PlatformManifest
) -> dict[str, object]:
    """Build the shared shell context from one platform's native projection."""

    platform_id = manifest.platform_id
    workspace_path = _workspace_path(manifest)
    runs_path = f"{workspace_path}/runs"
    automation = _automation(request)
    service = _platform_service(request)
    registry = request.app.state.platform_registry
    connections = automation.list_platform_connections(platform_id=platform_id)
    paused_control_keys = {
        row["control_key"] for row in automation.list_channel_controls(state="paused")
    }
    reader = next((row for row in connections if row["connection_key"] == "reader"), None)
    status = _connection_status((reader or {}).get("status"))
    native = service.native_summary(platform_id)
    summary = next(
        iter(automation.get_platform_summaries([platform_id])),
        {
            "open_opportunity_count": 0,
            "review_action_count": 0,
            "succeeded_action_count": 0,
        },
    )
    opportunities = automation.list_opportunities(platform_id=platform_id, limit=20)
    actions = automation.list_actions(platform_id=platform_id, limit=20)
    jobs = automation.list_jobs(platform_id=platform_id, limit=20)

    kols: list[dict[str, object]] = []
    for row in service.platform_kols(platform_id, limit=20):
        display = (
            row.get("display_name")
            or row.get("nickname")
            or row.get("handle")
            or row["id"]
        )
        handle = row.get("handle") or row.get("nickname") or row["id"]
        kols.append(
            {
                "detail_url": f"{workspace_path}#library",
                "status_url": (
                    f"{workspace_path}/kols/"
                    f"{quote(str(row['id']), safe='')}/status"
                ),
                "initials": str(display)[:2].upper(),
                "display_name": display,
                "handle": handle,
                "native_id": row["id"],
                "score": float(row.get("score") or 0),
                "status": row["status"],
                "reasons": list(row.get("reasons") or []),
            }
        )

    opportunity_rows: list[dict[str, object]] = []
    for row in opportunities:
        payload = row.get("payload") or {}
        reasons: list[str] = []
        for evidence in row.get("evidence") or []:
            if isinstance(evidence, str):
                reasons.append(evidence)
            elif isinstance(evidence, dict):
                reasons.append(
                    str(evidence.get("reason") or evidence.get("kind") or evidence)
                )
        opportunity_rows.append(
            {
                "priority": row["priority"],
                "kind_label": {
                    "reply": "互动",
                    "topic": "选题",
                    "outreach": "私信",
                }.get(row["opportunity_type"], row["opportunity_type"]),
                "title": row["title"],
                "summary": str(payload.get("text") or payload.get("draft") or "")[:240],
                "status": row["status"],
                "reasons": reasons,
                "native_object_label": row["native_object_type"],
                "native_url": payload.get("target_url"),
                "created_at": row["created_at"],
                "review_url": f"{workspace_path}#automation",
                "review_label": "查看动作",
                "dismiss_url": f"/automation/opportunities/{row['id']}/dismiss",
                "can_dismiss": row["status"] in {"open", "planned"},
            }
        )

    action_rows: list[dict[str, object]] = []
    for row in actions[:10]:
        decisions = automation.list_policy_decisions(action_id=int(row["id"]))
        decision = decisions[0] if decisions else {}
        action_rows.append(
            {
                "detail_url": f"{workspace_path}#automation",
                "approval_url": f"/automation/actions/{row['id']}/approve",
                "cancel_url": f"/automation/actions/{row['id']}/cancel",
                "resolve_url": f"/automation/actions/{row['id']}/resolve",
                "kind_label": ACTION_LABELS.get(row["action_type"], row["action_type"]),
                "target_label": (
                    f"{row['native_object_type']} · {row['native_object_id']}"
                ),
                "updated_at": row["updated_at"],
                "status": row["status"],
                "draft": row.get("final_text") or row.get("draft") or "",
                "policy_outcome": decision.get("outcome"),
                "policy_explanation": decision.get("explanation"),
                "external_id": row.get("external_id"),
                "receipt_url": row.get("receipt_url"),
                "error": row.get("error"),
            }
        )

    alerts = [
        {
            "severity": "critical" if row["status"] == "failed" else "warning",
            "title": "动作需要人工处理",
            "message": row.get("error") or row["status"],
            "href": "#automation",
        }
        for row in actions
        if row["status"] in {"failed", "confirmation_required"}
    ]
    if status != "connected":
        alerts.insert(
            0,
            {
                "severity": "warning",
                "title": "只读连接不可用",
                "message": str(
                    (reader or {}).get("last_health_error")
                    or ((reader or {}).get("metadata") or {}).get("detail")
                    or "请检查平台 HTTP API 凭据"
                ),
                "href": "#automation",
            },
        )

    recent_runs = _recent_run_rows(jobs, runs_path)
    active_discovery = next(
        (
            row
            for row in jobs
            if row["job_type"] in {"manual_discovery", "discovery_refresh"}
            and row["status"] in {"queued", "running"}
        ),
        None,
    )
    read_capabilities = {
        PlatformCapability.ACCOUNT_SEARCH.value,
        PlatformCapability.CONTENT_SEARCH.value,
        PlatformCapability.TIMELINE_FEED.value,
        PlatformCapability.RELATIONS.value,
        PlatformCapability.NATIVE_TRENDS.value,
        PlatformCapability.ANALYTICS.value,
    }
    capabilities = sorted(
        item.value for item in manifest.capabilities if item.value in read_capabilities
    )
    adapter = registry.resolve_automation_adapter(platform_id)
    workspace_feed_builder = getattr(adapter, "workspace_feed", None)
    native_workspace = (
        workspace_feed_builder(limit=20) if callable(workspace_feed_builder) else {}
    )
    return {
        "platform": {
            **manifest.as_dict(),
            "short_name": str(manifest.metadata.get("short_name") or platform_id[:3].upper()),
            "accent_color": str(manifest.metadata.get("accent_color") or "#52606d"),
            "description": "读取原生趋势、内容与账号证据，并维护平台内 KOL 评分。",
            "connection_status": status,
            "connection_message": (reader or {}).get("last_health_error"),
            "kill_switch": f"platform:{platform_id}" in paused_control_keys,
            "scan_interval_label": (
                f"每 {manifest.default_scan_interval_seconds // 60} 分钟"
            ),
            "safety_summary": "当前产品仅开放发现与只读研究能力。",
            "capabilities": capabilities,
            "can_scan": registry.has_handler(platform_id, "scan_signals"),
            "automatic_kol_management": not _settings(request).review_queue_enabled,
        },
        "urls": {
            "center": "/",
            "safety": "/settings/brand#automation-controls",
            "scan": f"{workspace_path}/scan",
            "opportunities": "#inbox",
            "discover": f"{workspace_path}/discover",
            "dm": f"{workspace_path}/dm",
            "conversation": f"{workspace_path}/conversations/validity",
            "seeds": f"{workspace_path}/seeds",
            "kols": "#library",
            "accounts": "#connections",
            "actions": "#automation",
            "runs": runs_path,
            "runs_fragment": f"{workspace_path}/runs-fragment",
        },
        "metrics": {
            "active_kols": int(native.get("active", 0)),
            "review_kols": int(native.get("review", 0)),
            "candidate_kols": int(native.get("candidate", 0)),
            "trends": int(native.get("trends", 0)),
            "content": int(native.get("content", 0)),
            "open_opportunities": int(summary.get("open_opportunity_count", 0)),
            "planned_opportunities": sum(
                1 for row in opportunities if row["status"] == "planned"
            ),
            "succeeded_actions_today": automation.count_succeeded_actions(
                platform_id,
                since=_local_day_start_utc(_settings(request)),
                automatic_only=True,
            ),
            "review_actions": int(summary.get("review_action_count", 0)),
            "next_scan_at": "由调度器自动安排",
        },
        "discovery": {"default_query": "加密货币/RWA", "default_limit": 20},
        "all_capabilities": [
            {"id": capability.value, "label": CAPABILITY_LABELS[capability.value]}
            for capability in PlatformCapability
        ],
        "alerts": alerts,
        "opportunities": opportunity_rows,
        "kols": kols,
        "recent_actions": action_rows,
        "recent_runs": recent_runs,
        "has_active_runs": any(
            row["status"] in {"queued", "running"} for row in recent_runs
        ),
        "active_discovery": active_discovery,
        "feedback": _workspace_feedback(request, jobs),
        "native_workspace": native_workspace,
    }


def create_platform_workspace_router(manifest: PlatformManifest) -> APIRouter:
    """Create static workspace routes for one explicitly registered platform."""

    platform_id = manifest.platform_id
    workspace_path = _workspace_path(manifest)
    router = APIRouter(prefix=workspace_path, tags=[f"platform:{platform_id}"])

    @router.get("", response_class=HTMLResponse)
    def workspace(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request,
            name=platform_workspace_template(manifest),
            context=platform_workspace_context(request, manifest),
        )

    @router.get("/module")
    def module_info() -> dict[str, object]:
        return manifest.as_dict()

    @router.get("/feed/{feed_name}", response_class=JSONResponse)
    def workspace_feed_page(
        request: Request,
        feed_name: str,
        offset: int = 0,
        limit: int = 20,
    ) -> dict[str, object]:
        adapter = request.app.state.platform_registry.resolve_automation_adapter(
            platform_id
        )
        page_builder = getattr(adapter, "workspace_feed_page", None)
        if not callable(page_builder):
            raise HTTPException(status_code=404, detail="平台未提供内容动态分页")
        try:
            return page_builder(feed_name, offset=offset, limit=limit)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get("/runs", response_class=HTMLResponse)
    def runs_page(request: Request) -> HTMLResponse:
        runs_path = f"{workspace_path}/runs"
        jobs = _automation(request).list_jobs(platform_id=platform_id, limit=100)
        recent_runs = _recent_run_rows(jobs, runs_path, limit=100)
        status_counts = {
            status: sum(1 for row in jobs if row["status"] == status)
            for status in ("queued", "running", "succeeded", "failed")
        }
        return templates.TemplateResponse(
            request=request,
            name="platform_runs.html",
            context={
                "platform": {
                    **manifest.as_dict(),
                    "short_name": str(
                        manifest.metadata.get("short_name")
                        or platform_id[:3].upper()
                    ),
                    "accent_color": str(
                        manifest.metadata.get("accent_color") or "#52606d"
                    ),
                },
                "urls": {
                    "center": "/",
                    "workspace": workspace_path,
                    "runs": runs_path,
                    "runs_fragment": f"{workspace_path}/runs-fragment",
                },
                "recent_runs": recent_runs,
                "has_active_runs": any(
                    row["status"] in {"queued", "running"}
                    for row in recent_runs
                ),
                "status_counts": status_counts,
                "feedback": _workspace_feedback(request, jobs),
            },
        )

    @router.get("/runs-fragment", response_class=HTMLResponse)
    def runs_fragment(request: Request) -> HTMLResponse:
        jobs = _automation(request).list_jobs(platform_id=platform_id, limit=100)
        runs_path = f"{workspace_path}/runs"
        recent_runs = _recent_run_rows(jobs, runs_path, limit=100)
        has_active_runs = any(
            row["status"] in {"queued", "running"} for row in recent_runs
        )
        response = templates.TemplateResponse(
            request=request,
            name="platform_runs_fragment.html",
            context={
                "recent_runs": recent_runs,
                "has_active_runs": has_active_runs,
                "urls": {
                    "runs": runs_path,
                    "runs_fragment": f"{workspace_path}/runs-fragment",
                },
            },
        )
        if request.headers.get("HX-Request") == "true" and not has_active_runs:
            response.headers["HX-Refresh"] = "true"
        return response

    @router.post("/discover")
    def discover(
        request: Request,
        query: Annotated[str, Form()] = "",
        limit: Annotated[int, Form()] = 20,
        source: Annotated[str, Form()] = "content",
    ) -> RedirectResponse:
        try:
            job_id = _platform_service(request).enqueue_discovery(
                platform_id,
                query=query,
                limit=limit,
                manual=True,
                source=source,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="平台未注册") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        try:
            _platform_service(request).execute_job_now(job_id)
        except RuntimeError:
            pass
        return RedirectResponse(
            url=f"{workspace_path}?discovery=completed&job_id={job_id}#library",
            status_code=303,
        )

    @router.post("/scan")
    def scan(request: Request) -> RedirectResponse:
        try:
            job_id = _platform_service(request).enqueue_signal_refresh(
                platform_id, manual=True
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="平台未注册") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        try:
            _platform_service(request).execute_job_now(job_id)
        except RuntimeError:
            pass
        return RedirectResponse(
            url=f"{workspace_path}?scan=completed&job_id={job_id}#inbox",
            status_code=303,
        )

    @router.post("/dm")
    def propose_dm(
        request: Request,
        native_id: Annotated[str, Form()],
        draft: Annotated[str, Form()],
        conversation_id: Annotated[str | None, Form()] = None,
        has_valid_conversation: Annotated[str, Form()] = "false",
    ) -> RedirectResponse:
        try:
            _platform_service(request).propose_dm(
                platform_id,
                native_id,
                draft,
                conversation_id=(conversation_id or "").strip() or None,
                has_valid_conversation=has_valid_conversation.lower() == "true",
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="平台或 KOL 不存在") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return RedirectResponse(url=f"{workspace_path}#automation", status_code=303)

    @router.post("/conversations/validity")
    def set_conversation_validity(
        request: Request,
        native_id: Annotated[str, Form()],
        conversation_id: Annotated[str, Form()],
        evidence: Annotated[str, Form()] = "",
        valid: Annotated[str, Form()] = "true",
    ) -> RedirectResponse:
        try:
            _platform_service(request).set_conversation_validity(
                platform_id,
                native_id,
                conversation_id,
                valid=valid.lower() == "true",
                evidence=evidence,
                actor=_settings(request).admin_username,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="平台或 KOL 不存在") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return RedirectResponse(url=f"{workspace_path}#library", status_code=303)

    @router.post("/health")
    def refresh_health(request: Request) -> RedirectResponse:
        _platform_service(request).refresh_health()
        return RedirectResponse(url=workspace_path, status_code=303)

    @router.post("/kols/{native_id}/status")
    def update_kol_status(
        request: Request,
        native_id: str,
        status: Annotated[str, Form()],
    ) -> RedirectResponse:
        try:
            _platform_service(request).set_kol_status(platform_id, native_id, status)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="KOL 不存在") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return RedirectResponse(url=f"{workspace_path}#library", status_code=303)

    @router.post("/seeds")
    def add_seed(
        request: Request,
        native_id: Annotated[str, Form()],
        display_name: Annotated[str | None, Form()] = None,
    ) -> RedirectResponse:
        try:
            _platform_service(request).add_seed(
                platform_id,
                native_id,
                display_name=(display_name or "").strip() or None,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="平台未注册") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return RedirectResponse(url=f"{workspace_path}#library", status_code=303)

    @router.post("/connections")
    async def add_connection(request: Request) -> RedirectResponse:
        form = await request.form()
        kind = str(form.get("connection_kind") or "postiz").strip()
        display_name = str(form.get("display_name") or "").strip()
        external_id = str(form.get("external_account_id") or "").strip()
        username = str(form.get("username") or "").strip().lstrip("@")
        integration_id = str(form.get("integration_id") or "").strip()
        is_default = str(form.get("is_default") or "").lower() in {"1", "true", "on"}
        try:
            if kind == "postiz":
                if manifest.metadata.get("publishing_bridge") != "postiz":
                    raise ValueError(f"平台 {platform_id} 未启用 Postiz 发布桥接")
                if not integration_id or not external_id or not username:
                    raise ValueError("Postiz connection 需要 integration ID、账号 ID 和用户名")
                identity = integration_id
                capabilities = [PlatformCapability.OWNED_PUBLISH.value]
                metadata = {
                    "kind": "postiz",
                    "integration_id": integration_id,
                    "username": username,
                    "external_account_id": external_id,
                    "is_default": is_default,
                }
                connection_status = (
                    "connected" if _settings(request).postiz_api_key else "disconnected"
                )
            else:
                raise ValueError("仅支持基于 HTTP API 的 Postiz 连接")

            key_hash = hashlib.sha256(
                f"{platform_id}:{kind}:{identity}".encode("utf-8")
            ).hexdigest()[:16]
            _platform_service(request).register_connection(
                platform_id,
                connection_key=f"{kind}-{key_hash}",
                display_name=display_name or username or integration_id,
                capabilities=capabilities,
                metadata=metadata,
                status=connection_status,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return RedirectResponse(
            url="/settings/brand?connection=saved#platform-connections",
            status_code=303,
        )

    return router


__all__ = [
    "CAPABILITY_LABELS",
    "JOB_LABELS",
    "create_platform_workspace_router",
    "platform_workspace_context",
    "platform_workspace_template",
]
