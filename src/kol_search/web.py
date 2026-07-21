from __future__ import annotations

import csv
import io
import json
import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from kol_search.db import Store
from kol_search.jobs import JobWorker, WeeklyScheduler
from kol_search.postiz import (
    PostizClient,
    PostizError,
    PostizSubmissionUncertain,
    safe_postiz_release_url,
    sanitize_postiz_error,
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


def _store(request: Request) -> Store:
    return request.app.state.store


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _textarea_lines(value: str) -> list[str]:
    return [line.strip() for line in value.splitlines() if line.strip()]


def _postiz_client(settings: Settings) -> PostizClient:
    if not settings.postiz_ready():
        raise PostizError("Postiz is not configured")
    return PostizClient(
        settings.postiz_base_url,
        settings.postiz_api_key or "",
        timeout=settings.postiz_timeout_seconds,
    )


def _postiz_publish_at(value: str, timezone_name: str) -> str:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("Please provide a valid scheduled time") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(timezone_name))
    parsed = parsed.astimezone(timezone.utc)
    if parsed <= datetime.now(timezone.utc):
        raise ValueError("Scheduled time must be in the future")
    return parsed.isoformat()


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
) -> RedirectResponse:
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
    status: str = Query(default="pending"),
    lifecycle: str = Query(default="all"),
) -> HTMLResponse:
    settings = _settings(request)
    clusters = _store(request).list_topic_clusters(
        status=status, lifecycle=lifecycle, limit=settings.signal_topic_limit
    )
    return templates.TemplateResponse(
        request=request,
        name="topic_radar.html",
        context={
            "clusters": clusters,
            "brand": _store(request).get_brand_profile(),
            "status": status,
            "lifecycle": lifecycle,
        },
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
def content_publishing(
    request: Request,
    topic_id: int | None = Query(default=None),
) -> HTMLResponse:
    store = _store(request)
    settings = _settings(request)
    topic = store.get_topic_cluster(topic_id) if topic_id else None
    topic_publication = (
        store.get_postiz_publication_for_topic(topic_id) if topic_id else None
    )
    integrations = []
    postiz_error = None
    if settings.postiz_ready():
        client = _postiz_client(settings)
        try:
            integrations = [item for item in client.list_integrations() if not item.disabled]
        except PostizError as exc:
            postiz_error = str(exc)
        finally:
            client.close()
    return templates.TemplateResponse(
        request=request,
        name="publishing.html",
        context={
            "publications": store.list_postiz_publications(),
            "topic": topic,
            "topic_publication": topic_publication,
            "integrations": integrations,
            "postiz_ready": settings.postiz_ready(),
            "postiz_error": postiz_error,
            "timezone": settings.timezone,
        },
    )


@app.post("/publishing")
def create_content_publication(
    request: Request,
    topic_cluster_id: Annotated[int, Form()],
    integration_id: Annotated[str, Form()],
    content_text: Annotated[str, Form()],
    publish_mode: Annotated[str, Form()] = "now",
    scheduled_at: Annotated[str, Form()] = "",
) -> RedirectResponse:
    store = _store(request)
    settings = _settings(request)
    topic = store.get_topic_cluster(topic_cluster_id)
    if not topic:
        raise HTTPException(status_code=404, detail="Topic Radar item not found")
    if topic["editorial_status"] != "adopted":
        raise HTTPException(status_code=422, detail="Adopt the Topic Radar item before publishing")
    content = content_text.strip()
    if not content:
        raise HTTPException(status_code=422, detail="Publication text cannot be empty")
    if publish_mode not in {"now", "schedule"}:
        raise HTTPException(status_code=422, detail="Invalid publish mode")
    if not settings.postiz_ready():
        raise HTTPException(status_code=503, detail="Postiz is not configured")
    try:
        publish_at = (
            _postiz_publish_at(scheduled_at, settings.timezone)
            if publish_mode == "schedule"
            else datetime.now(timezone.utc).isoformat()
        )
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    client = _postiz_client(settings)
    try:
        integration = next(
            (
                item
                for item in client.list_integrations()
                if item.id == integration_id and not item.disabled
            ),
            None,
        )
        if not integration:
            raise HTTPException(status_code=422, detail="Select an active Postiz integration")
        try:
            publication_id = store.create_postiz_publication(
                topic_cluster_id=topic_cluster_id,
                integration_id=integration.id,
                integration_name=integration.name,
                integration_identifier=integration.identifier,
                content_text=content,
                publish_mode=publish_mode,
                scheduled_at=publish_at if publish_mode == "schedule" else None,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        store.claim_postiz_submission(publication_id)
        try:
            result = client.create_post(
                integration=integration,
                content=content,
                publish_mode=publish_mode,
                publish_at=publish_at,
            )
        except PostizSubmissionUncertain as exc:
            store.finish_postiz_submission(
                publication_id,
                status="confirmation_required",
                sanitized_error=sanitize_postiz_error(exc),
            )
        except PostizError as exc:
            store.finish_postiz_submission(
                publication_id,
                status="failed",
                sanitized_error=sanitize_postiz_error(exc),
            )
        else:
            store.finish_postiz_submission(
                publication_id,
                status="scheduled" if publish_mode == "schedule" else "submitted",
                postiz_post_id=result.post_id,
            )
    except PostizError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    finally:
        client.close()
    return RedirectResponse(url=f"/publishing?created={publication_id}", status_code=303)


@app.post("/publishing/{publication_id}/refresh")
def refresh_content_publication(request: Request, publication_id: int) -> RedirectResponse:
    store = _store(request)
    settings = _settings(request)
    publication = store.get_postiz_publication(publication_id)
    if not publication:
        raise HTTPException(status_code=404, detail="Postiz publication not found")
    if not publication.get("postiz_post_id"):
        raise HTTPException(status_code=422, detail="Publication has no confirmed Postiz post ID")
    if not settings.postiz_ready():
        raise HTTPException(status_code=503, detail="Postiz is not configured")
    anchor_text = publication.get("scheduled_at") or publication.get("submitted_at")
    anchor = datetime.fromisoformat(anchor_text).astimezone(timezone.utc)
    now = datetime.now(timezone.utc)
    client = _postiz_client(settings)
    try:
        item = client.get_post(
            publication["postiz_post_id"],
            start_date=(anchor - timedelta(days=2)).isoformat(),
            end_date=(max(anchor, now) + timedelta(days=2)).isoformat(),
        )
    except PostizError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    finally:
        client.close()
    if not item:
        store.update_postiz_status(
            publication_id,
            sanitized_error="Postiz did not return this post in the publication window",
        )
        return RedirectResponse(url="/publishing?refresh=missing", status_code=303)

    state = str(item.get("state") or "").upper()
    raw_release_url = str(item.get("releaseURL") or "").strip() or None
    release_url = safe_postiz_release_url(raw_release_url)
    if state == "PUBLISHED" and release_url:
        store.update_postiz_status(
            publication_id,
            status="published",
            postiz_state=state,
            published_url=release_url,
        )
        store.update_topic_cluster(
            int(publication["topic_cluster_id"]),
            status="published",
            published_url=release_url,
        )
    elif state == "PUBLISHED":
        store.update_postiz_status(
            publication_id,
            status="confirmation_required",
            postiz_state=state,
            sanitized_error=(
                "Postiz reported an unsafe release URL"
                if raw_release_url
                else "Postiz reported PUBLISHED without a release URL"
            ),
        )
    elif state == "ERROR":
        store.update_postiz_status(
            publication_id,
            status="failed",
            postiz_state=state,
            sanitized_error="Postiz reported a publishing error",
        )
    elif state == "DRAFT":
        store.update_postiz_status(publication_id, status="draft", postiz_state=state)
    elif state == "QUEUE":
        store.update_postiz_status(publication_id, status="scheduled", postiz_state=state)
    else:
        store.update_postiz_status(
            publication_id,
            postiz_state=state or None,
            sanitized_error="Postiz returned an unknown publication state",
        )
    return RedirectResponse(url="/publishing?refresh=ok", status_code=303)


@app.post("/signal-scans")
def create_signal_scan(
    request: Request,
    backend: Annotated[str, Form()] = "mock",
    use_ai: Annotated[bool, Form()] = False,
) -> RedirectResponse:
    settings = _settings(request)
    ready, reason = settings.backend_ready(backend)
    if not ready:
        raise HTTPException(status_code=400, detail=reason or "后端未配置")
    brand = _store(request).get_brand_profile()
    if not brand.get("brand_name") or not brand.get("x_handle"):
        raise HTTPException(status_code=422, detail="请先完成品牌配置")
    if _store(request).has_active_run("signal_scan"):
        return RedirectResponse(url="/radar/people?active=1", status_code=303)
    _store(request).create_run(
        query="X KOL and topic signal scan",
        backend=backend,
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
    backend: Annotated[str, Form()] = "mock",
    use_ai: Annotated[bool, Form()] = False,
    seed_set_id: Annotated[int | None, Form()] = None,
) -> RedirectResponse:
    settings = _settings(request)
    ready, reason = settings.backend_ready(backend)
    if not ready:
        raise HTTPException(status_code=400, detail=reason or "后端未配置")
    if kind not in {"theme", "global", "seed_build", "seed_expand", "signal_scan"}:
        raise HTTPException(status_code=422, detail="Invalid run kind")
    if not query.strip() and kind == "theme":
        raise HTTPException(status_code=422, detail="主题不能为空")
    if kind == "seed_expand" and not seed_set_id:
        raise HTTPException(status_code=422, detail="扩散任务必须选择基础种子集")
    run_id = _store(request).create_run(
        query=query.strip() or "global crypto library",
        backend=backend,
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
