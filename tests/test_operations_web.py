from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from kol_search.web import app


def test_operations_account_approval_and_live_write_gate(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("KOL_DB_PATH", str(tmp_path / "operations.db"))
    monkeypatch.setenv("TWITTER_BACKEND", "mock")
    monkeypatch.setenv("KOL_ENABLE_MOCK_BACKEND", "true")
    monkeypatch.setenv("KOL_ENABLE_WEEKLY_REFRESH", "false")
    monkeypatch.setenv("KOL_LIVE_WRITE_ENABLED", "false")
    with TestClient(app) as client:
        saved = client.post(
            "/managed-accounts",
            data={
                "platform": "x",
                "external_account_id": "brand",
                "username": "brand",
                "browser_profile": "brand-profile",
                "roles": "engagement,official_dm",
            },
            follow_redirects=False,
        )
        assert saved.status_code == 303
        account_id = app.state.store.list_managed_accounts()[0]["id"]
        drafted = client.post(
            "/outbound-actions",
            data={
                "kind": "comment",
                "platform": "x",
                "managed_account_id": str(account_id),
                "target_external_id": "post-1",
                "target_author_id": "author-1",
                "target_url": "https://x.com/author/status/1",
                "draft": "Relevant context",
            },
            follow_redirects=False,
        )
        assert drafted.status_code == 303
        action = app.state.store.list_outbound_actions()[0]
        approved = client.post(
            f"/outbound-actions/{action['id']}/approve",
            data={"final_text": "Relevant context"},
            follow_redirects=False,
        )
        assert approved.status_code == 303
        blocked = client.post(
            f"/outbound-actions/{action['id']}/execute", follow_redirects=False
        )
        assert blocked.status_code == 303
        assert "error=" in blocked.headers["location"]
        assert app.state.store.get_outbound_action(action["id"])["status"] == "approved"
        page = client.get("/operations")
        assert page.status_code == 200
        assert "评论与私信审批" in page.text
