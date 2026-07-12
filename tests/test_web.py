import time
from pathlib import Path

from fastapi.testclient import TestClient

from kol_search.web import app


def test_dashboard_run_detail_review_and_exports(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("KOL_DB_PATH", str(tmp_path / "web.db"))
    monkeypatch.setenv("TWITTER_BACKEND", "mock")
    monkeypatch.setenv("KOL_MAX_USER_QUERIES", "8")
    monkeypatch.setenv("KOL_MAX_POST_QUERIES", "8")
    monkeypatch.setenv("KOL_MAX_CANDIDATES", "500")
    monkeypatch.setenv("KOL_MAX_ENRICHED_CANDIDATES", "200")
    monkeypatch.setenv("KOL_MAX_CONTACT_ACCOUNTS", "100")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with TestClient(app) as client:
        dashboard = client.get("/")
        assert dashboard.status_code == 200
        assert "新建搜索任务" in dashboard.text

        library = client.get("/library")
        assert library.status_code == 200
        assert "策展种子库" in library.text
        assert "VitalikButerin" in library.text

        response = client.post(
            "/runs",
            data={
                "query": "DeFi, 去中心化金融",
                "kind": "theme",
                "language": "all",
                "account_type": "all",
                "min_followers": "1000",
                "result_limit": "30",
                "backend": "mock",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        run_url = response.headers["location"]
        for _ in range(50):
            detail = client.get(run_url)
            if "completed" in detail.text:
                break
            time.sleep(0.05)
        assert detail.status_code == 200
        assert "alpha_defi_researcher" in detail.text

        run_id = int(run_url.rsplit("/", 1)[1])
        csv_response = client.get(f"/exports/{run_id}.csv")
        assert csv_response.status_code == 200
        assert "contact_source_url" in csv_response.text
        json_response = client.get(f"/exports/{run_id}.json")
        assert json_response.status_code == 200
        assert json_response.json()["results"]

        store = app.state.store
        result = store.get_run_results(run_id, "all")[0]
        account = client.get(f"/accounts/{result['account_id']}")
        assert account.status_code == 200


def test_web_seed_build_expand_review_and_exports(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("KOL_DB_PATH", str(tmp_path / "seed-web.db"))
    monkeypatch.setenv("TWITTER_BACKEND", "mock")
    monkeypatch.setenv("KOL_ENABLE_WEEKLY_REFRESH", "false")
    with TestClient(app) as client:
        build = client.post(
            "/runs",
            data={"kind": "seed_build", "query": "seed build", "backend": "mock"},
            follow_redirects=False,
        )
        assert build.status_code == 303
        build_id = int(build.headers["location"].rsplit("/", 1)[1])
        for _ in range(100):
            run = app.state.store.get_run(build_id)
            if run and run["status"].startswith("completed"):
                break
            time.sleep(0.05)
        base_set = app.state.store.list_seed_sets()[0]
        assert base_set["approved_count"] == 100

        expand = client.post(
            "/runs",
            data={
                "kind": "seed_expand",
                "query": "seed expand",
                "backend": "mock",
                "seed_set_id": str(base_set["id"]),
            },
            follow_redirects=False,
        )
        assert expand.status_code == 303
        expand_id = int(expand.headers["location"].rsplit("/", 1)[1])
        for _ in range(140):
            run = app.state.store.get_run(expand_id)
            if run and run["status"].startswith("completed"):
                break
            time.sleep(0.05)
        expanded_set = app.state.store.list_seed_sets()[0]
        detail = client.get(f"/seed-sets/{expanded_set['id']}")
        assert detail.status_code == 200
        assert "100 approved · 100 pending" in detail.text
        assert client.get(f"/exports/seed-sets/{expanded_set['id']}.csv").status_code == 200
        exported = client.get(f"/exports/seed-sets/{expanded_set['id']}.json")
        assert len(exported.json()["members"]) == 200

        pending = next(
            member
            for member in app.state.store.list_seed_members(expanded_set["id"])
            if member["review_status"] == "pending"
        )
        reviewed = client.post(
            f"/seed-sets/{expanded_set['id']}/members/{pending['account_id']}/status",
            data={"status": "approved"},
            follow_redirects=False,
        )
        assert reviewed.status_code == 303
        assert app.state.store.get_seed_set(expanded_set["id"])["approved_count"] == 101
