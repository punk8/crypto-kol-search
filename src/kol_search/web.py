from __future__ import annotations

import csv
import io
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from kol_search.db import Store
from kol_search.jobs import JobWorker, WeeklyScheduler
from kol_search.settings import PROJECT_ROOT, Settings, get_settings


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
        },
    )


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
    if kind not in {"theme", "global", "seed_build", "seed_expand"}:
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
