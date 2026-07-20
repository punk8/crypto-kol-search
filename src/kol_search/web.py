from __future__ import annotations

import csv
import hashlib
import hmac
import io
import json
import re
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated
from urllib.parse import quote

from fastapi import BackgroundTasks, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from kol_search.contacts import assert_public_url
from kol_search.db import Store
from kol_search.discovery.multiplatform import MultiPlatformTrendService
from kol_search.jobs import JobWorker, WeeklyScheduler
from kol_search.outbound import (
    OutboundConfirmationRequired,
    OutboundError,
    OutboundService,
    TargetNotMessageable,
    outbound_idempotency_key,
)
from kol_search.publishing import (
    ALLOWED_IMAGE_TYPES,
    MAX_IMAGE_BYTES,
    OwnedPublishingService,
    PublishingError,
    local_schedule_to_utc,
)
from kol_search.replies import ReplyPublisher
from kol_search.settings import PROJECT_ROOT, Settings, get_settings
from kol_search.twitter.base import TwitterBackendError
from kol_search.twitter.reply import (
    ReplyConfirmationRequiredError,
    create_twitter_reply_client,
)


PACKAGE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(PACKAGE_DIR / "templates"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    if settings.web_host not in {"127.0.0.1", "localhost", "::1"} and not (
        settings.admin_password and settings.session_secret
    ):
        raise RuntimeError(
            "非本机监听必须同时配置 KOL_ADMIN_PASSWORD 和 KOL_SESSION_SECRET"
        )
    store = Store(settings.db_path())
    worker = JobWorker(store, settings)
    scheduler = WeeklyScheduler(store, settings, worker)
    app.state.settings = settings
    app.state.store = store
    app.state.worker = worker
    app.state.scheduler = scheduler
    worker.start()
    scheduler.start()
    try:
        yield
    finally:
        scheduler.stop()
        worker.stop()
        store.close()


app = FastAPI(title="Crypto KOL Search", version="0.1.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(PACKAGE_DIR / "static")), name="static")


def _auth_secret(settings: Settings) -> bytes:
    material = settings.session_secret or settings.admin_password or "local-only"
    return material.encode("utf-8")


def _session_token(settings: Settings) -> str:
    return hmac.new(
        _auth_secret(settings), settings.admin_username.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def _actor(request: Request) -> str:
    return request.app.state.settings.admin_username


@app.middleware("http")
async def admin_security(request: Request, call_next):  # noqa: ANN001
    settings = get_settings()
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
    settings = get_settings()
    if not settings.admin_password:
        return RedirectResponse(url="/", status_code=303)
    valid = secrets.compare_digest(username, settings.admin_username) and secrets.compare_digest(
        password, settings.admin_password
    )
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


def _store(request: Request) -> Store:
    return request.app.state.store


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _publishing_redirect(*, message: str | None = None, error: str | None = None) -> RedirectResponse:
    params = []
    if message:
        params.append(f"message={quote(message[:300])}")
    if error:
        params.append(f"error={quote(error[:300])}")
    suffix = f"?{'&'.join(params)}" if params else ""
    return RedirectResponse(url=f"/publishing{suffix}", status_code=303)


async def _parse_owned_post_form(
    request: Request, *, existing: dict | None = None
) -> dict:
    form = await request.form()
    items = [str(value).strip() for value in form.getlist("items")]
    if not items or any(not value for value in items):
        raise ValueError("帖子或线程的每一段都必须填写内容")
    if len(items) > 25:
        raise ValueError("一个线程最多支持 25 段")

    integration_id = str(form.get("integration_id") or "").strip()
    mode = str(form.get("mode") or "now").strip()
    scheduled_at_local = str(form.get("scheduled_at_local") or "").strip() or None
    scheduled_at_utc = None
    if mode == "schedule":
        scheduled_at_utc = local_schedule_to_utc(scheduled_at_local or "")
        if datetime.fromisoformat(scheduled_at_utc) <= datetime.now(timezone.utc):
            raise ValueError("定时发布时间必须晚于当前时间")
    elif mode != "now":
        raise ValueError("发布模式无效")

    media: list[dict] = []
    media_root = _settings(request).publishing_media_path()
    media_root.mkdir(parents=True, exist_ok=True)
    existing_by_position: dict[int, list[dict]] = {}
    for row in (existing or {}).get("media", []):
        existing_by_position.setdefault(int(row["item_position"]), []).append(row)

    url_fields = [str(value) for value in form.getlist("media_urls")]
    suffixes = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/gif": ".gif",
        "image/webp": ".webp",
    }
    for position in range(len(items)):
        raw_urls = url_fields[position] if position < len(url_fields) else ""
        urls = [value.strip() for value in re.split(r"[\n,]", raw_urls) if value.strip()]
        files = [
            value
            for value in form.getlist(f"media_file_{position}")
            if getattr(value, "filename", "")
        ]
        if len(urls) + len(files) > 4:
            raise ValueError(f"第 {position + 1} 段最多支持 4 张图片")
        if not urls and not files:
            for row in existing_by_position.get(position, []):
                media.append(
                    {
                        "item_position": position,
                        "source_type": row["source_type"],
                        "source_value": row["source_value"],
                        "filename": row.get("filename"),
                        "content_type": row.get("content_type"),
                        "storage_path": row.get("storage_path"),
                    }
                )
        for url in urls:
            if not url.startswith("https://"):
                raise ValueError("远程图片必须使用公开 HTTPS URL")
            url = assert_public_url(url)
            media.append(
                {
                    "item_position": position,
                    "source_type": "url",
                    "source_value": url,
                }
            )
        for upload in files:
            content_type = str(getattr(upload, "content_type", "") or "").split(";", 1)[0]
            if content_type not in ALLOWED_IMAGE_TYPES:
                raise ValueError(f"不支持的图片类型：{content_type or '未知'}")
            content = await upload.read(MAX_IMAGE_BYTES + 1)
            if not content:
                raise ValueError("上传图片不能为空")
            if len(content) > MAX_IMAGE_BYTES:
                raise ValueError("单张图片不能超过 10MB")
            digest = hashlib.sha256(content).hexdigest()
            path = media_root / f"{digest}-{secrets.token_hex(4)}{suffixes[content_type]}"
            path.write_bytes(content)
            media.append(
                {
                    "item_position": position,
                    "source_type": "local",
                    "source_value": str(getattr(upload, "filename", path.name)),
                    "filename": str(getattr(upload, "filename", path.name)),
                    "content_type": content_type,
                    "storage_path": str(path),
                }
            )

    source_cluster_value = str(form.get("source_cluster_id") or "").strip()
    return {
        "integration_id": integration_id,
        "items": items,
        "media": media,
        "source_type": "trend" if source_cluster_value else "manual",
        "source_cluster_id": int(source_cluster_value) if source_cluster_value else None,
        "mode": mode,
        "timezone_name": "Asia/Shanghai",
        "scheduled_at_local": scheduled_at_local,
        "scheduled_at_utc": scheduled_at_utc,
        "who_can_reply": str(form.get("who_can_reply") or "everyone"),
        "made_with_ai": str(form.get("made_with_ai") or "") == "true",
    }


def _textarea_lines(value: str) -> list[str]:
    return [line.strip() for line in value.splitlines() if line.strip()]


def _backends(settings: Settings) -> list[dict]:
    output: list[dict] = []
    for name, label in (
        ("official", "X 官方 API"),
        ("twitterapi_io", "TwitterAPI.io"),
        ("third_party", "第三方 API"),
        ("twscrape", "twscrape（高风险）"),
        ("opencli", "OpenCLI（Chrome 登录态）"),
        ("mock", "Mock 测试数据"),
    ):
        if name == "mock" and not settings.enable_mock_backend:
            continue
        ready, reason = settings.backend_ready(name)
        output.append({"name": name, "label": label, "ready": ready, "reason": reason})
    return output


def _seed_catalog(account_type: str, language: str) -> list[dict[str, str]]:
    path = PROJECT_ROOT / "seeds" / "crypto_seed_library.csv"
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as file:
        rows = list(csv.DictReader(file))
    output: list[dict[str, str]] = []
    for row in rows:
        is_person = row.get("account_type") == "person"
        if account_type == "person" and not is_person:
            continue
        if account_type == "organization" and is_person:
            continue
        languages = (row.get("languages") or "").split("|")
        if language != "all" and language not in languages:
            continue
        output.append(row)
    return output


@app.get("/health", response_class=JSONResponse)
def health(request: Request) -> dict:
    return {"status": "ok", "database": str(_settings(request).db_path())}


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request) -> HTMLResponse:
    settings = _settings(request)
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "runs": _store(request).list_runs(30),
            "backends": _backends(settings),
            "default_backend": settings.twitter_backend,
            "ai_ready": bool(settings.openai_api_key),
            "timezone": settings.timezone,
            "weekly_day": settings.weekly_day,
            "weekly_hour": settings.weekly_hour,
            "weekly_enabled": settings.enable_weekly_refresh,
            "signal_enabled": settings.enable_signal_scan,
            "signal_interval": settings.signal_interval_minutes,
        },
    )


@app.get("/settings/brand", response_class=HTMLResponse)
def brand_settings(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="brand_settings.html",
        context={"brand": _store(request).get_brand_profile()},
    )


@app.post("/settings/brand")
def update_brand_settings(
    request: Request,
    brand_name: Annotated[str, Form()],
    x_handle: Annotated[str, Form()],
    xiaohongshu_handle: Annotated[str, Form()] = "",
    product_url: Annotated[str, Form()] = "",
    description: Annotated[str, Form()] = "",
    audience: Annotated[str, Form()] = "",
    tone: Annotated[str, Form()] = "professional, concise, conversational",
    allowed_claims: Annotated[str, Form()] = "",
    forbidden_terms: Annotated[str, Form()] = "",
) -> RedirectResponse:
    if not brand_name.strip() or not x_handle.strip().lstrip("@"):
        raise HTTPException(status_code=422, detail="品牌名称和 X handle 不能为空")
    _store(request).save_brand_profile(
        brand_name=brand_name,
        x_handle=x_handle,
        xiaohongshu_handle=xiaohongshu_handle,
        product_url=product_url,
        description=description,
        audience=audience,
        tone=tone,
        allowed_claims=_textarea_lines(allowed_claims),
        forbidden_terms=_textarea_lines(forbidden_terms),
    )
    return RedirectResponse(url="/settings/brand?saved=1", status_code=303)


@app.get("/radar/people", response_class=HTMLResponse)
def people_radar(
    request: Request,
    status: str = Query(default="pending"),
    language: str = Query(default="all"),
) -> HTMLResponse:
    store = _store(request)
    store.expire_reply_opportunities()
    settings = _settings(request)
    runs = [run for run in store.list_runs(30) if run.get("kind") == "signal_scan"]
    backends = _backends(settings)
    return templates.TemplateResponse(
        request=request,
        name="people_radar.html",
        context={
            "opportunities": store.list_reply_opportunities(
                status=status, language=language, limit=settings.signal_reply_limit
            ),
            "brand": store.get_brand_profile(),
            "status": status,
            "language": language,
            "runs": runs[:5],
            "backends": backends,
            "opencli_ready": any(
                item["name"] == "opencli" and item["ready"] for item in backends
            ),
            "default_backend": settings.twitter_backend,
            "ai_ready": bool(settings.openai_api_key),
            "signal_enabled": settings.enable_signal_scan,
            "signal_interval": settings.signal_interval_minutes,
            "managed_accounts": [
                account
                for account in store.list_managed_accounts()
                if account["status"] == "active" and "engagement" in account["roles"]
            ],
        },
    )


@app.post("/reply-opportunities/{opportunity_id}")
def update_reply_opportunity(
    request: Request,
    opportunity_id: int,
    status: Annotated[str | None, Form()] = None,
    draft: Annotated[str | None, Form()] = None,
    reply_url: Annotated[str | None, Form()] = None,
) -> RedirectResponse:
    clean_url = (reply_url or "").strip() or None
    if status == "replied" and (
        not clean_url
        or not re.match(r"^https://(?:x\.com|twitter\.com)/[^/]+/status/\d+", clean_url)
    ):
        raise HTTPException(status_code=422, detail="请填写有效的 X 回复链接")
    try:
        _store(request).update_reply_opportunity(
            opportunity_id,
            status=status,
            draft=draft,
            reply_url=clean_url,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="回复机会不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return RedirectResponse(url="/radar/people", status_code=303)


@app.post("/reply-opportunities/{opportunity_id}/publish")
def publish_reply_opportunity(
    request: Request,
    opportunity_id: int,
    draft: Annotated[str, Form()],
    backend: Annotated[str, Form()] = "opencli",
    managed_account_id: Annotated[int | None, Form()] = None,
) -> RedirectResponse:
    if managed_account_id is not None:
        opportunity = _store(request).get_reply_opportunity(opportunity_id)
        if not opportunity:
            raise HTTPException(status_code=404, detail="回复机会不存在")
        try:
            action_id = _store(request).create_outbound_action(
                kind="comment",
                platform=opportunity.get("platform") or "x",
                managed_account_id=managed_account_id,
                target_external_id=opportunity.get("post_external_id") or opportunity["post_id"],
                target_url=opportunity.get("post_url"),
                target_author_id=opportunity.get("author_id") or opportunity.get("author_username"),
                draft=draft,
                idempotency_key=outbound_idempotency_key(
                    platform=opportunity.get("platform") or "x",
                    kind="comment",
                    target_external_id=opportunity.get("post_external_id")
                    or opportunity["post_id"],
                    text=draft,
                ),
                actor=_actor(request),
            )
            _store(request).update_reply_opportunity(
                opportunity_id, status="pending", draft=draft
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return RedirectResponse(url=f"/operations?action={action_id}", status_code=303)
    settings = _settings(request)
    ready, reason = settings.backend_ready(backend)
    if not ready:
        raise HTTPException(status_code=503, detail=reason or "回复后端未就绪")
    brand = _store(request).get_brand_profile()
    actor_handle = (brand.get("x_handle") or "").strip().lstrip("@")
    if not actor_handle:
        raise HTTPException(status_code=422, detail="请先配置品牌 X handle")
    try:
        client = create_twitter_reply_client(backend, settings)
    except TwitterBackendError as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from exc
    try:
        ReplyPublisher(_store(request), client, actor_handle).publish(
            opportunity_id,
            text=draft,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="回复机会不存在") from exc
    except ReplyConfirmationRequiredError:
        return RedirectResponse(
            url="/radar/people?status=confirmation_required&send=unconfirmed",
            status_code=303,
        )
    except (ValueError, TwitterBackendError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    finally:
        client.close()
    return RedirectResponse(url="/radar/people?status=replied&send=ok", status_code=303)


@app.get("/radar/topics", response_class=HTMLResponse)
def topic_radar(
    request: Request,
    background_tasks: BackgroundTasks,
    status: str = Query(default="pending"),
    lifecycle: str = Query(default="all"),
    direction: str = Query(default="all"),
    platform: str = Query(default="all"),
    refresh: bool = Query(default=False),
    force: bool = Query(default=False),
) -> HTMLResponse:
    settings = _settings(request)
    service = MultiPlatformTrendService(_store(request), settings)
    refresh_result: dict | None = service.cached()
    if refresh:
        if force or refresh_result is None:
            background_tasks.add_task(
                service.refresh, force=force, x_backend=settings.twitter_backend
            )
            refresh_result = refresh_result or {
                "captured_at": "刷新任务已排队",
                "cached": True,
                "warnings": [],
            }
    clusters = _store(request).list_topic_clusters(
        status=status,
        lifecycle=lifecycle,
        direction=direction,
        platform=platform,
        limit=max(settings.signal_topic_limit, 50),
    )
    if refresh_result and refresh_result.get("cluster_ids"):
        latest_cluster_ids = set(refresh_result["cluster_ids"])
        clusters = [cluster for cluster in clusters if cluster["id"] in latest_cluster_ids]
    return templates.TemplateResponse(
        request=request,
        name="topic_radar.html",
        context={
            "clusters": clusters,
            "brand": _store(request).get_brand_profile(),
            "status": status,
            "lifecycle": lifecycle,
            "direction": direction,
            "platform": platform,
            "categories": _store(request).list_trend_categories(),
            "refresh_result": refresh_result,
        },
    )


@app.get("/radar/topics/{cluster_id}", response_class=HTMLResponse)
def topic_detail(request: Request, cluster_id: int) -> HTMLResponse:
    cluster = _store(request).get_topic_cluster(cluster_id)
    if not cluster:
        raise HTTPException(status_code=404, detail="趋势话题不存在")
    posts_by_platform: dict[str, list[dict]] = {}
    for post in cluster.get("posts", []):
        posts_by_platform.setdefault(post.get("platform") or "x", []).append(post)
    return templates.TemplateResponse(
        request=request,
        name="topic_detail.html",
        context={"cluster": cluster, "posts_by_platform": posts_by_platform},
    )


@app.post("/topic-clusters/{cluster_id}")
def update_topic_cluster(
    request: Request,
    cluster_id: int,
    status: Annotated[str | None, Form()] = None,
    outline: Annotated[str | None, Form()] = None,
    draft: Annotated[str | None, Form()] = None,
    published_url: Annotated[str | None, Form()] = None,
) -> RedirectResponse:
    clean_url = (published_url or "").strip() or None
    if status == "published" and clean_url and not re.match(
        r"^https://(?:x\.com|twitter\.com)/[^/]+/status/\d+", clean_url
    ):
        raise HTTPException(status_code=422, detail="发布链接必须是有效的 X 帖子链接")
    try:
        _store(request).update_topic_cluster(
            cluster_id,
            status=status,
            outline=outline,
            draft=draft,
            published_url=clean_url,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="趋势话题不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return RedirectResponse(url="/radar/topics", status_code=303)


@app.get("/publishing", response_class=HTMLResponse)
def publishing_workspace(
    request: Request,
    source_cluster_id: int | None = Query(default=None),
    edit: int | None = Query(default=None),
    status: str = Query(default="all"),
    message: str | None = Query(default=None),
    error: str | None = Query(default=None),
) -> HTMLResponse:
    store = _store(request)
    settings = _settings(request)
    sync_error: str | None = None
    if settings.postiz_api_key:
        service = OwnedPublishingService(store, settings)
        try:
            service.sync_integrations(actor=_actor(request))
        except (PublishingError, ValueError) as exc:
            sync_error = str(exc)
        finally:
            service.close()
    edit_post = store.get_owned_post(edit) if edit is not None else None
    if edit is not None and not edit_post:
        raise HTTPException(status_code=404, detail="主动发布草稿不存在")
    source_cluster = None
    prefill_items = [""]
    if edit_post:
        prefill_items = [item["content"] for item in edit_post["items"]]
        if edit_post.get("source_cluster_id"):
            source_cluster = store.get_topic_cluster(int(edit_post["source_cluster_id"]))
    elif source_cluster_id is not None:
        source_cluster = store.get_topic_cluster(source_cluster_id)
        if not source_cluster:
            raise HTTPException(status_code=404, detail="趋势话题不存在")
        seed = str(source_cluster.get("draft") or source_cluster.get("outline") or "").strip()
        prefill_items = [seed or str(source_cluster.get("title") or "")]
    return templates.TemplateResponse(
        request=request,
        name="publishing.html",
        context={
            "postiz_ready": bool(settings.postiz_api_key),
            "postiz_api_url": settings.postiz_api_url,
            "integrations": store.list_postiz_integrations(),
            "posts": store.list_owned_posts(status=status),
            "audit_events": [
                event
                for event in store.list_audit_events(100)
                if event["object_type"] in {"owned_post", "postiz", "postiz_integration"}
            ][:30],
            "status": status,
            "message": message,
            "error": error or sync_error,
            "edit_post": edit_post,
            "source_cluster": source_cluster,
            "prefill_items": prefill_items,
        },
    )


@app.post("/publishing/integrations/sync")
def sync_publishing_integrations(request: Request) -> RedirectResponse:
    service = OwnedPublishingService(_store(request), _settings(request))
    try:
        integrations = service.sync_integrations(force=True, actor=_actor(request))
    except (PublishingError, ValueError) as exc:
        return _publishing_redirect(error=str(exc))
    finally:
        service.close()
    return _publishing_redirect(message=f"已同步 {len(integrations)} 个 X integration")


@app.post("/publishing/integrations/{integration_id}/default")
def set_default_publishing_integration(
    request: Request, integration_id: str
) -> RedirectResponse:
    try:
        _store(request).set_default_postiz_integration(integration_id, actor=_actor(request))
    except ValueError as exc:
        return _publishing_redirect(error=str(exc))
    return _publishing_redirect(message="默认 X 账号已更新")


@app.post("/publishing/posts")
async def create_owned_post(request: Request) -> RedirectResponse:
    try:
        values = await _parse_owned_post_form(request)
        post_id = _store(request).create_owned_post(**values, actor=_actor(request))
    except (ValueError, OSError) as exc:
        return _publishing_redirect(error=str(exc))
    return RedirectResponse(url=f"/publishing?message={quote(f'草稿 #{post_id} 已保存')}", status_code=303)


@app.post("/publishing/posts/{post_id}/edit")
async def edit_owned_post(request: Request, post_id: int) -> RedirectResponse:
    existing = _store(request).get_owned_post(post_id)
    if not existing:
        raise HTTPException(status_code=404, detail="主动发布草稿不存在")
    try:
        values = await _parse_owned_post_form(request, existing=existing)
        values.pop("source_type")
        values.pop("source_cluster_id")
        _store(request).update_owned_post_draft(post_id, **values, actor=_actor(request))
    except (ValueError, OSError) as exc:
        return _publishing_redirect(error=str(exc))
    return _publishing_redirect(message=f"草稿 #{post_id} 已更新")


@app.post("/publishing/posts/{post_id}/approve")
def approve_owned_post(request: Request, post_id: int) -> RedirectResponse:
    service = OwnedPublishingService(_store(request), _settings(request))
    try:
        service.approve(post_id, actor=_actor(request))
    except (KeyError, ValueError, PublishingError) as exc:
        return _publishing_redirect(error=str(exc))
    return _publishing_redirect(message=f"帖子 #{post_id} 已审批，尚未提交")


@app.post("/publishing/posts/{post_id}/submit")
def submit_owned_post(request: Request, post_id: int) -> RedirectResponse:
    service = OwnedPublishingService(_store(request), _settings(request))
    try:
        post = service.submit(post_id, actor=_actor(request))
    except (KeyError, ValueError, PublishingError) as exc:
        return _publishing_redirect(error=str(exc))
    finally:
        service.close()
    label = "已发布" if post["status"] == "published" else f"状态已更新为 {post['status']}"
    return _publishing_redirect(message=f"帖子 #{post_id} {label}")


@app.post("/publishing/posts/{post_id}/reconcile")
def reconcile_owned_post(request: Request, post_id: int) -> RedirectResponse:
    service = OwnedPublishingService(_store(request), _settings(request))
    try:
        post = service.reconcile(post_id, actor=_actor(request))
    except (KeyError, ValueError, PublishingError) as exc:
        return _publishing_redirect(error=str(exc))
    finally:
        service.close()
    return _publishing_redirect(message=f"帖子 #{post_id} 当前状态：{post['status']}")


def _scheduled_post_action(
    request: Request, post_id: int, action: str
) -> RedirectResponse:
    service = OwnedPublishingService(_store(request), _settings(request))
    try:
        getattr(service, action)(post_id, actor=_actor(request))
    except (KeyError, ValueError, PublishingError) as exc:
        return _publishing_redirect(error=str(exc))
    finally:
        service.close()
    labels = {"pause": "已暂停", "resume": "已恢复排期", "delete": "已取消并删除"}
    return _publishing_redirect(message=f"帖子 #{post_id} {labels[action]}")


@app.post("/publishing/posts/{post_id}/pause")
def pause_owned_post(request: Request, post_id: int) -> RedirectResponse:
    return _scheduled_post_action(request, post_id, "pause")


@app.post("/publishing/posts/{post_id}/resume")
def resume_owned_post(request: Request, post_id: int) -> RedirectResponse:
    return _scheduled_post_action(request, post_id, "resume")


@app.post("/publishing/posts/{post_id}/delete")
def delete_owned_post(request: Request, post_id: int) -> RedirectResponse:
    return _scheduled_post_action(request, post_id, "delete")


@app.get("/operations", response_class=HTMLResponse)
def operations(
    request: Request,
    status: str = Query(default="all"),
    kind: str = Query(default="all"),
    error: str | None = Query(default=None),
    candidate_account_id: str | None = Query(default=None),
) -> HTMLResponse:
    store = _store(request)
    candidate = store.get_account(candidate_account_id) if candidate_account_id else None
    suggested_dm = ""
    if candidate:
        brand = store.get_brand_profile()
        topics = []
        try:
            topics = json.loads(candidate.get("topics_json") or "[]")
        except json.JSONDecodeError:
            topics = []
        subject = "、".join(topics[:2]) or "相关领域"
        suggested_dm = (
            f"你好，我是{brand.get('brand_name') or '项目'}官方团队。"
            f"看到你近期关于{subject}的分享，想就可能的合作与产品使用场景和你交流。"
        )
        if brand.get("product_url"):
            suggested_dm += f" 产品信息：{brand['product_url']}。"
        suggested_dm += "如果你不希望继续收到消息，请直接告诉我，我们会立即停止联系。"
    return templates.TemplateResponse(
        request=request,
        name="operations.html",
        context={
            "accounts": store.list_managed_accounts(),
            "actions": store.list_outbound_actions(status=status, kind=kind),
            "audit_events": store.list_audit_events(30),
            "kill_switch": store.get_control("global_kill_switch", "false") == "true",
            "live_write_enabled": _settings(request).live_write_enabled,
            "status": status,
            "kind": kind,
            "error": error,
            "candidate": candidate,
            "suggested_dm": suggested_dm,
        },
    )


@app.post("/managed-accounts")
def create_managed_account(
    request: Request,
    platform: Annotated[str, Form()],
    external_account_id: Annotated[str, Form()],
    username: Annotated[str, Form()],
    browser_profile: Annotated[str, Form()],
    roles: Annotated[str, Form()] = "engagement",
) -> RedirectResponse:
    settings = _settings(request)
    try:
        _store(request).upsert_managed_account(
            platform=platform,
            external_account_id=external_account_id.strip(),
            username=username.strip(),
            browser_profile=browser_profile.strip(),
            roles=[role.strip() for role in roles.split(",") if role.strip()],
            comment_daily_limit=settings.comment_daily_limit,
            comment_hourly_limit=settings.comment_hourly_limit,
            dm_daily_limit=settings.dm_daily_limit,
            dm_hourly_limit=settings.dm_hourly_limit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return RedirectResponse(url="/operations?account=saved", status_code=303)


@app.post("/managed-accounts/{account_id}/status")
def update_managed_account_status(
    request: Request,
    account_id: int,
    status: Annotated[str, Form()],
) -> RedirectResponse:
    try:
        _store(request).set_managed_account_status(
            account_id, status, actor=_actor(request), reason="管理员操作"
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="授权账号不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return RedirectResponse(url="/operations", status_code=303)


@app.post("/controls/kill-switch")
def update_kill_switch(
    request: Request,
    enabled: Annotated[str, Form()] = "true",
) -> RedirectResponse:
    _store(request).set_control(
        "global_kill_switch",
        "true" if enabled.lower() == "true" else "false",
        actor=_actor(request),
    )
    return RedirectResponse(url="/operations", status_code=303)


@app.post("/outbound-actions")
def create_outbound_action(
    request: Request,
    kind: Annotated[str, Form()],
    platform: Annotated[str, Form()],
    managed_account_id: Annotated[int, Form()],
    target_external_id: Annotated[str, Form()],
    target_url: Annotated[str, Form()],
    draft: Annotated[str, Form()],
    target_author_id: Annotated[str, Form()] = "",
    conversation_id: Annotated[str, Form()] = "",
) -> RedirectResponse:
    try:
        action_id = _store(request).create_outbound_action(
            kind=kind,
            platform=platform,
            managed_account_id=managed_account_id,
            target_external_id=target_external_id.strip(),
            target_url=target_url.strip(),
            target_author_id=target_author_id.strip() or target_external_id.strip(),
            conversation_id=conversation_id.strip() or None,
            draft=draft,
            idempotency_key=outbound_idempotency_key(
                platform=platform,
                kind=kind,
                target_external_id=target_external_id.strip(),
                text=draft,
            ),
            actor=_actor(request),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return RedirectResponse(url=f"/operations?action={action_id}", status_code=303)


@app.post("/outbound-actions/{action_id}/approve")
def approve_outbound_action(
    request: Request,
    action_id: int,
    final_text: Annotated[str, Form()],
) -> RedirectResponse:
    try:
        _store(request).approve_outbound_action(
            action_id, final_text=final_text, actor=_actor(request)
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="发送任务不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return RedirectResponse(url="/operations?approved=1", status_code=303)


@app.post("/outbound-actions/{action_id}/execute")
def execute_outbound_action(request: Request, action_id: int) -> RedirectResponse:
    try:
        OutboundService(_store(request), _settings(request)).execute(
            action_id, actor=_actor(request)
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="发送任务不存在") from exc
    except (OutboundConfirmationRequired, TargetNotMessageable, OutboundError) as exc:
        return RedirectResponse(
            url=f"/operations?error={quote(str(exc)[:300])}", status_code=303
        )
    return RedirectResponse(url="/operations?sent=1", status_code=303)


@app.post("/outbound-actions/{action_id}/cancel")
def cancel_outbound_action(request: Request, action_id: int) -> RedirectResponse:
    action = _store(request).get_outbound_action(action_id)
    if not action:
        raise HTTPException(status_code=404, detail="发送任务不存在")
    if action["status"] not in {"draft", "approved", "failed"}:
        raise HTTPException(status_code=409, detail="当前状态不能取消")
    try:
        _store(request).finish_outbound_action(
            action_id, status="cancelled", actor=_actor(request), error="管理员取消"
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="发送任务不存在") from exc
    return RedirectResponse(url="/operations", status_code=303)


@app.post("/do-not-contact")
def add_do_not_contact(
    request: Request,
    platform: Annotated[str, Form()],
    target_external_id: Annotated[str, Form()],
    reason: Annotated[str, Form()] = "拒绝继续联系",
) -> RedirectResponse:
    _store(request).add_do_not_contact(
        platform=platform,
        target_external_id=target_external_id.strip(),
        reason=reason.strip(),
        actor=_actor(request),
    )
    return RedirectResponse(url="/operations", status_code=303)


@app.post("/signal-scans")
def create_signal_scan(
    request: Request,
    backend: Annotated[str, Form()] = "",
    use_ai: Annotated[bool, Form()] = False,
) -> RedirectResponse:
    settings = _settings(request)
    selected_backend = backend.strip() or settings.twitter_backend
    ready, reason = settings.backend_ready(selected_backend)
    if not ready:
        raise HTTPException(status_code=400, detail=reason or "后端未配置")
    brand = _store(request).get_brand_profile()
    if not brand.get("brand_name") or not brand.get("x_handle"):
        raise HTTPException(status_code=422, detail="请先完成品牌配置")
    if _store(request).has_active_run("signal_scan"):
        return RedirectResponse(url="/radar/people?active=1", status_code=303)
    _store(request).create_run(
        query="X KOL and topic signal scan",
        backend=selected_backend,
        kind="signal_scan",
        language="all",
        account_type="person",
        min_followers=0,
        result_limit=settings.signal_reply_limit + settings.signal_topic_limit,
        use_ai=use_ai and bool(settings.openai_api_key),
        model=settings.openai_model,
        config={"manual": True, "interval_minutes": settings.signal_interval_minutes},
    )
    request.app.state.worker.notify()
    return RedirectResponse(url="/radar/people?started=1", status_code=303)


@app.post("/runs")
def create_run(
    request: Request,
    query: Annotated[str, Form()] = "crypto",
    kind: Annotated[str, Form()] = "theme",
    language: Annotated[str, Form()] = "all",
    account_type: Annotated[str, Form()] = "all",
    min_followers: Annotated[int, Form()] = 1000,
    result_limit: Annotated[int, Form()] = 100,
    backend: Annotated[str, Form()] = "",
    use_ai: Annotated[bool, Form()] = False,
    seed_set_id: Annotated[int | None, Form()] = None,
    platforms: Annotated[str, Form()] = "x,xiaohongshu",
) -> RedirectResponse:
    settings = _settings(request)
    selected_backend = backend.strip() or settings.twitter_backend
    ready, reason = settings.backend_ready(selected_backend)
    if not ready:
        raise HTTPException(status_code=400, detail=reason or "后端未配置")
    if kind not in {
        "theme", "multiplatform", "global", "seed_build", "seed_expand", "signal_scan"
    }:
        raise HTTPException(status_code=422, detail="Invalid run kind")
    if not query.strip() and kind in {"theme", "multiplatform"}:
        raise HTTPException(status_code=422, detail="主题不能为空")
    if kind == "seed_expand" and not seed_set_id:
        raise HTTPException(status_code=422, detail="扩散任务必须选择基础种子集")
    run_id = _store(request).create_run(
        query=query.strip() or "global crypto library",
        backend=selected_backend,
        kind=kind,
        language=language,
        account_type=account_type,
        min_followers=max(0, min_followers),
        result_limit=max(1, min(100, result_limit)),
        use_ai=use_ai,
        model=settings.openai_model,
        config={
            "max_user_queries": settings.max_user_queries,
            "max_post_queries": settings.max_post_queries,
            "max_candidates": settings.max_candidates,
            "max_contact_accounts": settings.max_contact_accounts,
            "seed_set_id": seed_set_id,
            "hard_budget_usd": 5.0,
            "platforms": [
                item for item in (value.strip() for value in platforms.split(","))
                if item in {"x", "xiaohongshu"}
            ] or ["x", "xiaohongshu"],
        },
    )
    request.app.state.worker.notify()
    return RedirectResponse(url=f"/runs/{run_id}", status_code=303)


@app.get("/runs/{run_id}", response_class=HTMLResponse)
def run_detail(
    request: Request,
    run_id: int,
    account_type: str = Query(default="person"),
) -> HTMLResponse:
    run = _store(request).get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="任务不存在")
    if run.get("kind") == "signal_scan":
        return RedirectResponse(url="/radar/people", status_code=303)
    results = _store(request).get_run_results(run_id, account_type)
    template = "run_fragment.html" if request.headers.get("HX-Request") else "run.html"
    return templates.TemplateResponse(
        request=request,
        name=template,
        context={"run": run, "results": results, "account_type": account_type},
    )


@app.post("/runs/{run_id}/retry")
def retry_run(request: Request, run_id: int) -> RedirectResponse:
    try:
        new_id = _store(request).retry_run(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    request.app.state.worker.notify()
    return RedirectResponse(url=f"/runs/{new_id}", status_code=303)


@app.get("/library", response_class=HTMLResponse)
def library(
    request: Request,
    account_type: str = Query(default="person"),
    language: str = Query(default="all"),
    min_followers: int = Query(default=0, ge=0),
    contact_status: str = Query(default="all"),
) -> HTMLResponse:
    accounts = _store(request).list_library(
        account_type=account_type,
        language=language,
        min_followers=min_followers,
        contact_status=contact_status,
    )
    return templates.TemplateResponse(
        request=request,
        name="library.html",
        context={
            "accounts": accounts,
            "seed_catalog": _seed_catalog(account_type, language),
            "account_type": account_type,
            "language": language,
            "min_followers": min_followers,
            "contact_status": contact_status,
            "seed_sets": _store(request).list_seed_sets(),
        },
    )


@app.get("/seed-sets/{seed_set_id}", response_class=HTMLResponse)
def seed_set_detail(request: Request, seed_set_id: int) -> HTMLResponse:
    seed_set = _store(request).get_seed_set(seed_set_id)
    if not seed_set:
        raise HTTPException(status_code=404, detail="种子集不存在")
    members = _store(request).list_seed_members(seed_set_id)
    actual_topics: dict[str, int] = {}
    for member in members:
        if member["review_status"] == "rejected":
            continue
        topic = member["primary_topic"]
        actual_topics[topic] = actual_topics.get(topic, 0) + 1
    topic_gaps = {
        topic: max(0, expected - actual_topics.get(topic, 0))
        for topic, expected in (seed_set.get("quotas", {}).get("topics") or {}).items()
    }
    return templates.TemplateResponse(
        request=request,
        name="seed_set.html",
        context={"seed_set": seed_set, "members": members, "topic_gaps": topic_gaps},
    )


@app.post("/seed-sets/{seed_set_id}/members/{account_id}/status")
def update_seed_member(
    request: Request,
    seed_set_id: int,
    account_id: str,
    status: Annotated[str, Form()],
) -> RedirectResponse:
    try:
        _store(request).update_seed_member_status(seed_set_id, account_id, status)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="种子成员不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return RedirectResponse(url=f"/seed-sets/{seed_set_id}", status_code=303)


@app.get("/exports/seed-sets/{seed_set_id}.{format}")
def export_seed_set(request: Request, seed_set_id: int, format: str) -> Response:
    seed_set = _store(request).get_seed_set(seed_set_id)
    if not seed_set:
        raise HTTPException(status_code=404, detail="种子集不存在")
    members = _store(request).list_seed_members(seed_set_id)
    contacts = _store(request).contacts_for_accounts([member["account_id"] for member in members])
    rows = []
    for member in members:
        item = dict(member)
        item["contacts"] = contacts.get(member["account_id"], [])
        rows.append(item)
    if format == "json":
        return JSONResponse(
            {"seed_set": seed_set, "members": rows},
            headers={"Content-Disposition": f'attachment; filename="seed-set-{seed_set_id}.json"'},
        )
    if format != "csv":
        raise HTTPException(status_code=404, detail="仅支持 csv 或 json")
    buffer = io.StringIO()
    fields = [
        "rank", "role", "review_status", "account_id", "username", "name",
        "primary_topic", "language_bucket", "account_type_bucket", "institution_kind",
        "followers_count", "score", "network_seed_count", "common_seed_count",
        "discovery_paths", "contact_type", "contact_value", "contact_status",
        "contact_source_url", "contact_evidence",
    ]
    writer = csv.DictWriter(buffer, fieldnames=fields)
    writer.writeheader()
    for item in rows:
        for contact in item["contacts"] or [None]:
            writer.writerow({
                **{key: item.get(key) for key in fields if not key.startswith("contact_")},
                "contact_type": contact.get("contact_type") if contact else "",
                "contact_value": contact.get("value") if contact else "",
                "contact_status": contact.get("status") if contact else "",
                "contact_source_url": contact.get("source_url") if contact else "",
                "contact_evidence": contact.get("evidence") if contact else "",
            })
    return Response(
        buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="seed-set-{seed_set_id}.csv"'},
    )


@app.get("/accounts/{account_id}", response_class=HTMLResponse)
def account_detail(request: Request, account_id: str) -> HTMLResponse:
    account = _store(request).get_account(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="账号不存在")
    for key in ("languages_json", "topics_json", "entities_json"):
        try:
            account[key.removesuffix("_json")] = json.loads(account.get(key) or "[]")
        except json.JSONDecodeError:
            account[key.removesuffix("_json")] = []
    for score in account["scores"]:
        try:
            score["payload"] = json.loads(score.get("payload_json") or "{}")
        except json.JSONDecodeError:
            score["payload"] = {}
    return templates.TemplateResponse(
        request=request,
        name="account.html",
        context={"account": account},
    )


@app.post("/contacts/{contact_id}/status")
def update_contact(
    request: Request,
    contact_id: int,
    status: Annotated[str, Form()],
    account_id: Annotated[str, Form()],
) -> RedirectResponse:
    try:
        _store(request).update_contact_status(contact_id, status)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="联系方式不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return RedirectResponse(url=f"/accounts/{account_id}", status_code=303)


@app.get("/exports/{run_id}.{format}")
def export_run(request: Request, run_id: int, format: str) -> Response:
    run = _store(request).get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="任务不存在")
    rows = _store(request).get_run_results(run_id, "all")
    contacts = _store(request).contacts_for_accounts([row["account_id"] for row in rows])
    structured = []
    for row in rows:
        item = dict(row)
        item.pop("payload_json", None)
        item["languages"] = json.loads(item.pop("languages_json") or "[]")
        item["topics"] = json.loads(item.pop("topics_json") or "[]")
        item["contacts"] = contacts.get(row["account_id"], [])
        structured.append(item)
    if format == "json":
        return JSONResponse(
            {"run": {key: value for key, value in run.items() if not key.endswith("_json")}, "results": structured},
            headers={"Content-Disposition": f'attachment; filename="kol-run-{run_id}.json"'},
        )
    if format != "csv":
        raise HTTPException(status_code=404, detail="仅支持 csv 或 json")
    buffer = io.StringIO()
    fields = [
        "rank", "username", "name", "account_type", "followers_count", "score_total",
        "domain_score", "languages", "topics", "contact_type", "contact_value",
        "contact_status", "contact_source_url", "contact_evidence",
        "contact_stale",
    ]
    writer = csv.DictWriter(buffer, fieldnames=fields)
    writer.writeheader()
    for item in structured:
        item_contacts = item["contacts"] or [None]
        for contact in item_contacts:
            writer.writerow({
                "rank": item.get("rank"),
                "username": item.get("username"),
                "name": item.get("name"),
                "account_type": item.get("account_type"),
                "followers_count": item.get("followers_count"),
                "score_total": item.get("score_total"),
                "domain_score": item.get("domain_score"),
                "languages": ";".join(item.get("languages") or []),
                "topics": ";".join(item.get("topics") or []),
                "contact_type": contact.get("contact_type") if contact else "",
                "contact_value": contact.get("value") if contact else "",
                "contact_status": contact.get("status") if contact else "",
                "contact_source_url": contact.get("source_url") if contact else "",
                "contact_evidence": contact.get("evidence") if contact else "",
                "contact_stale": contact.get("stale") if contact else "",
            })
    return Response(
        buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="kol-run-{run_id}.csv"'},
    )
