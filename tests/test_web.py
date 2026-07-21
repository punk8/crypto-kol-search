import time
from pathlib import Path

from fastapi.testclient import TestClient

from kol_search.postiz import PostizCreateResult, PostizIntegration
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


def test_signal_radars_brand_config_and_manual_scan(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("KOL_DB_PATH", str(tmp_path / "signal-web.db"))
    monkeypatch.setenv("TWITTER_BACKEND", "mock")
    monkeypatch.setenv("KOL_ENABLE_WEEKLY_REFRESH", "false")
    monkeypatch.setenv("KOL_ENABLE_SIGNAL_SCAN", "false")
    with TestClient(app) as client:
        saved = client.post(
            "/settings/brand",
            data={
                "brand_name": "Signal Labs",
                "x_handle": "signal_labs",
                "description": "Onchain research tools",
                "audience": "Crypto researchers",
                "tone": "concise",
                "allowed_claims": "Public onchain data",
                "forbidden_terms": "guaranteed",
            },
            follow_redirects=False,
        )
        assert saved.status_code == 303

        started = client.post(
            "/signal-scans",
            data={"backend": "mock"},
            follow_redirects=False,
        )
        assert started.status_code == 303
        for _ in range(100):
            runs = [
                run for run in app.state.store.list_runs() if run["kind"] == "signal_scan"
            ]
            if runs and runs[0]["status"].startswith("completed"):
                break
            time.sleep(0.05)

        people = client.get("/radar/people")
        topics = client.get("/radar/topics")
        assert people.status_code == 200
        assert topics.status_code == 200
        assert "今日回复机会" in people.text
        assert "热搜选题" in topics.text
        assert app.state.store.list_reply_opportunities(status="all")
        assert app.state.store.list_topic_clusters(status="all")

        opportunity = app.state.store.list_reply_opportunities(status="all")[0]
        edited = client.post(
            f"/reply-opportunities/{opportunity['id']}",
            data={"draft": "Human edited reply"},
            follow_redirects=False,
        )
        assert edited.status_code == 303
        assert app.state.store.list_reply_opportunities(status="all")[0]["draft"] == "Human edited reply"

        published = client.post(
            f"/reply-opportunities/{opportunity['id']}/publish",
            data={"backend": "mock", "draft": "Human edited reply"},
            follow_redirects=False,
        )
        assert published.status_code == 303, published.text
        saved = app.state.store.get_reply_opportunity(opportunity["id"])
        assert saved["status"] == "replied"
        assert saved["reply_url"].startswith("https://x.com/signal_labs/status/")


def test_topic_radar_postiz_owned_content_flow(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("KOL_DB_PATH", str(tmp_path / "postiz-web.db"))
    monkeypatch.setenv("TWITTER_BACKEND", "mock")
    monkeypatch.setenv("KOL_ENABLE_WEEKLY_REFRESH", "false")
    monkeypatch.setenv("POSTIZ_BASE_URL", "https://api.postiz.test")
    monkeypatch.setenv("POSTIZ_API_KEY", "test-api-key")

    class FakePostizClient:
        def __init__(self, *args, **kwargs):
            pass

        def close(self):
            return None

        def list_integrations(self):
            return [PostizIntegration("int-1", "LaunchVibes", "x", False)]

        def create_post(self, **kwargs):
            assert kwargs["integration"].id == "int-1"
            assert kwargs["content"] == "Exact owned post"
            return PostizCreateResult("post-1", "int-1")

        def get_post(self, post_id, **kwargs):
            assert post_id == "post-1"
            return {
                "id": "post-1",
                "state": "PUBLISHED",
                "releaseURL": "https://x.com/launchvibes/status/9001",
            }

    monkeypatch.setattr("kol_search.web.PostizClient", FakePostizClient)
    with TestClient(app) as client:
        store = app.state.store
        topic_id = store.upsert_topic_cluster(
            fingerprint="topic:postiz-web",
            title="Creator planning",
            summary="A relevant owned-content topic",
            language="en",
            lifecycle="rising",
            heat_score=70,
            metrics={"post_count": 3},
            outline="Planning outline",
            draft="Topic draft",
            draft_source="rules",
            native_trend=False,
            post_ids=[],
        )
        store.update_topic_cluster(topic_id, status="adopted")

        topics = client.get("/radar/topics?status=adopted")
        assert "Send to Content Publishing" in topics.text
        compose = client.get(f"/publishing?topic_id={topic_id}")
        assert compose.status_code == 200
        assert "Creator planning" in compose.text
        assert "LaunchVibes · x" in compose.text
        assert "暂无待发布内容" in compose.text
        assert "从趋势雷达采纳一个主题，生成品牌内容并提交到 Postiz。" in compose.text
        assert "查看趋势雷达" in compose.text
        assert 'href="/radar/topics"' in compose.text

        submitted = client.post(
            "/publishing",
            data={
                "topic_cluster_id": str(topic_id),
                "integration_id": "int-1",
                "content_text": "Exact owned post",
                "publish_mode": "now",
            },
            follow_redirects=False,
        )
        assert submitted.status_code == 303
        publication = store.list_postiz_publications()[0]
        assert publication["status"] == "submitted"
        assert publication["postiz_post_id"] == "post-1"
        assert publication["content_text"] == "Exact owned post"

        duplicate = client.post(
            "/publishing",
            data={
                "topic_cluster_id": str(topic_id),
                "integration_id": "int-1",
                "content_text": "Second post",
                "publish_mode": "now",
            },
        )
        assert duplicate.status_code == 422

        refreshed = client.post(
            f"/publishing/{publication['id']}/refresh",
            follow_redirects=False,
        )
        assert refreshed.status_code == 303
        saved = store.get_postiz_publication(publication["id"])
        assert saved and saved["status"] == "published"
        assert saved["published_url"] == "https://x.com/launchvibes/status/9001"
        assert store.get_topic_cluster(topic_id)["editorial_status"] == "published"
