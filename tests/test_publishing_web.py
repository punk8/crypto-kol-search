from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from kol_search.web import app


def test_publishing_web_draft_approval_and_missing_key_gate(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("KOL_DB_PATH", str(tmp_path / "publishing-web.db"))
    monkeypatch.setenv("KOL_PUBLISHING_MEDIA_DIR", str(tmp_path / "media"))
    monkeypatch.setenv("KOL_ENABLE_WEEKLY_REFRESH", "false")
    monkeypatch.setenv("POSTIZ_API_KEY", "")
    with TestClient(app) as client:
        app.state.store.sync_postiz_integrations(
            [{"id": "x1", "identifier": "x", "name": "Brand", "profile": "brand"}]
        )
        page = client.get("/publishing")
        assert page.status_code == 200
        assert "Postiz X 主动发布" in page.text
        assert "真实发布未就绪" in page.text

        created = client.post(
            "/publishing/posts",
            files=[
                ("integration_id", (None, "x1")),
                ("mode", (None, "now")),
                ("who_can_reply", (None, "everyone")),
                ("made_with_ai", (None, "true")),
                ("items", (None, "First")),
                ("media_urls", (None, "")),
                ("items", (None, "Second")),
                ("media_urls", (None, "")),
            ],
            follow_redirects=False,
        )
        assert created.status_code == 303
        post = app.state.store.list_owned_posts()[0]
        assert [item["content"] for item in post["items"]] == ["First", "Second"]
        assert post["made_with_ai"] == 1

        approved = client.post(
            f"/publishing/posts/{post['id']}/approve", follow_redirects=False
        )
        assert approved.status_code == 303
        blocked = client.post(
            f"/publishing/posts/{post['id']}/submit", follow_redirects=False
        )
        assert blocked.status_code == 303
        assert "error=" in blocked.headers["location"]
        assert app.state.store.get_owned_post(post["id"])["status"] == "failed"


def test_publishing_web_schedule_converts_shanghai_to_utc(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("KOL_DB_PATH", str(tmp_path / "schedule-web.db"))
    monkeypatch.setenv("KOL_ENABLE_WEEKLY_REFRESH", "false")
    monkeypatch.setenv("POSTIZ_API_KEY", "")
    local = datetime.now(ZoneInfo("Asia/Shanghai")) + timedelta(days=2)
    local_value = local.strftime("%Y-%m-%dT%H:%M")
    with TestClient(app) as client:
        app.state.store.sync_postiz_integrations(
            [{"id": "x1", "identifier": "x", "profile": "brand"}]
        )
        response = client.post(
            "/publishing/posts",
            data={
                "integration_id": "x1",
                "mode": "schedule",
                "scheduled_at_local": local_value,
                "who_can_reply": "everyone",
                "items": "Scheduled post",
                "media_urls": "",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        post = app.state.store.list_owned_posts()[0]
        expected = datetime.fromisoformat(local_value).replace(
            tzinfo=ZoneInfo("Asia/Shanghai")
        ).astimezone(ZoneInfo("UTC"))
        assert datetime.fromisoformat(post["scheduled_at_utc"]) == expected
