from __future__ import annotations

import sqlite3
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from kol_search.automation import AutomationStore
from kol_search.cli import app as cli_app
from kol_search.platforms import PlatformManifest
from kol_search.web import _platform_workspace_template, app as web_app


@pytest.fixture
def isolated_platform_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Use deterministic local configuration with request-scoped execution."""

    database_path = tmp_path / "platform-integration.db"
    monkeypatch.setenv("KOL_DB_PATH", str(database_path))
    monkeypatch.setenv("KOL_DATABASE_URL_KEYCHAIN_SERVICE", "")
    monkeypatch.setenv("KOL_ENABLED_PLATFORMS", "x,xiaohongshu")
    monkeypatch.setenv("TWITTER_BACKEND", "mock")
    monkeypatch.setenv("KOL_X_PROVIDER_CHAIN", "mock")
    monkeypatch.setenv("KOL_ENABLE_MOCK_BACKEND", "true")
    monkeypatch.setenv("KOL_BACKEND_URL", "")
    monkeypatch.setenv("KOL_X_WATCH_HANDLES", "")
    monkeypatch.setenv("KOL_ENABLE_SIGNAL_SCAN", "false")
    monkeypatch.setenv("KOL_AUTO_EXECUTION_ENABLED", "false")
    monkeypatch.setenv("KOL_ADMIN_PASSWORD", "")
    monkeypatch.setenv("POSTIZ_API_KEY", "")
    monkeypatch.setenv("OPENAI_API_KEY", "")

    return database_path


def test_platform_center_and_independent_workspaces_gate_capability_controls(
    isolated_platform_environment: Path,
) -> None:
    with TestClient(web_app) as client:
        center = client.get("/")
        assert center.status_code == 200
        assert center.template.name == "discover.html"
        assert "Trend Radar" in center.text
        assert "X trend observations" in center.text
        assert "Native X ranking" in center.text
        assert 'href="/kols"' in center.text
        assert 'href="/watchlist"' in center.text
        assert "X via mock" in center.text
        assert "Live · mock" in center.text
        assert "YouTube" not in center.text
        assert "Reddit" not in center.text
        assert "Instagram" not in center.text

        x_workspace = client.get("/platforms/x")
        red_workspace = client.get("/platforms/xiaohongshu")
        missing_workspace = client.get("/platforms/youtube")

        assert x_workspace.status_code == 200
        assert red_workspace.status_code == 200
        assert missing_workspace.status_code == 404
        assert x_workspace.template.name == "platform_x_workspace.html"
        assert red_workspace.template.name == "platform_xiaohongshu_workspace.html"
        assert "X 数据源" in x_workspace.text
        assert "近期 Trend 推文" in x_workspace.text
        assert "KOL 最新推文" in x_workspace.text
        assert 'http-equiv="refresh" content="300"' in x_workspace.text
        assert 'data-x-feed="trends"' in x_workspace.text
        assert 'data-x-feed="kols"' in x_workspace.text
        assert "向下滚动加载更多" in x_workspace.text
        assert client.get("/platforms/x/feed/trends?offset=0&limit=20").json() == {
            "items": [],
            "next_offset": 0,
            "has_more": False,
        }
        assert client.get("/platforms/x/feed/unknown").status_code == 404
        assert "小红书 数据源" in red_workspace.text
        assert "近期 Trend" not in red_workspace.text
        assert "账号身份与评分不与其他平台合并" not in red_workspace.text

        x_discovery_match = re.search(
            r'<form action="/platforms/x/discover".*?</form>',
            x_workspace.text,
            re.DOTALL,
        )
        red_discovery_match = re.search(
            r'<form action="/platforms/xiaohongshu/discover".*?</form>',
            red_workspace.text,
            re.DOTALL,
        )
        assert x_discovery_match is not None
        assert red_discovery_match is not None
        x_discovery = x_discovery_match.group(0)
        red_discovery = red_discovery_match.group(0)

        # X declares account search and relations. Xiaohongshu currently does
        # not, so those controls are absent while its content search remains.
        assert 'option value="accounts"' in x_discovery
        assert 'option value="relations"' in x_discovery
        assert 'option value="content"' in red_discovery
        assert 'option value="accounts"' not in red_discovery
        assert 'option value="relations"' not in red_discovery
        assert "账号搜索" not in red_workspace.text
        assert "自有发布" not in red_workspace.text
        assert 'action="/publishing/posts"' not in red_workspace.text
        assert 'href="/platforms/x/runs"' in x_workspace.text
        assert 'id="runs"' not in x_workspace.text
        assert "RECENT RUNS" not in x_workspace.text
        assert "无需人工维护" not in x_workspace.text
        assert "candidate / active / paused / rejected" not in x_workspace.text

        added_seed = client.post(
            "/platforms/x/seeds",
            data={"native_id": "auto-managed", "display_name": "Auto Managed"},
            follow_redirects=False,
        )
        assert added_seed.status_code == 303
        managed_workspace = client.get("/platforms/x")
        assert "后台自动管理" in managed_workspace.text
        assert "自动评分" in managed_workspace.text
        assert "无需人工维护" not in managed_workspace.text
        assert 'action="/platforms/x/seeds"' not in managed_workspace.text
        assert 'action="/platforms/x/kols/auto-managed/status"' not in managed_workspace.text


def test_workspace_template_falls_back_for_future_platforms() -> None:
    manifest = PlatformManifest(
        platform_id="youtube_test",
        name="YouTube Test",
        version="0.1",
    )

    assert _platform_workspace_template(manifest) == "platform_workspace.html"


def test_platform_discovery_and_scan_routes_execute_jobs_in_request(
    isolated_platform_environment: Path,
) -> None:
    with TestClient(web_app) as client:
        discovered = client.post(
            "/platforms/x/discover",
            data={"query": "RWA research", "limit": "17", "source": "content"},
            follow_redirects=False,
        )
        scanned = client.post("/platforms/x/scan", follow_redirects=False)
        unknown = client.post(
            "/platforms/youtube/scan", follow_redirects=False
        )

        assert discovered.status_code == 303
        assert discovered.headers["location"] == (
            "/platforms/x?discovery=completed&job_id=1#library"
        )
        assert scanned.status_code == 303
        assert scanned.headers["location"] == (
            "/platforms/x?scan=completed&job_id=2#inbox"
        )
        assert unknown.status_code == 404

        jobs = web_app.state.automation.list_jobs(limit=20)
        discovery_job = next(row for row in jobs if row["job_type"] == "manual_discovery")
        signal_job = next(row for row in jobs if row["job_type"] == "signal_refresh")
        assert discovery_job["platform_id"] == "x"
        assert discovery_job["status"] == "succeeded"
        assert discovery_job["payload"] == {
            "limit": 17,
            "query": "RWA research",
            "source": "content",
        }
        assert signal_job["platform_id"] == "x"
        assert signal_job["status"] == "succeeded"
        assert signal_job["result"]["trends"] == 3


def test_global_trend_and_kol_discovery_refresh_the_home_index(
    isolated_platform_environment: Path,
) -> None:
    with TestClient(web_app) as client:
        trend = client.post(
            "/discover/trends",
            data={"platform_id": "x", "limit": "5"},
            follow_redirects=False,
        )
        kol = client.post(
            "/discover/kols",
            data={
                "platform_id": "x",
                "query": "crypto",
                "source": "accounts",
                "limit": "5",
            },
            follow_redirects=False,
        )
        assert trend.status_code == 303
        assert trend.headers["location"].endswith("#trends")
        assert kol.status_code == 303
        assert kol.headers["location"].endswith("#kols")
        assert all(
            row["status"] == "succeeded"
            for row in web_app.state.automation.list_jobs(limit=10)
        )
        home = client.get("/")
        assert "Trend Radar" in home.text
        assert "DeFi" in home.text
        assert "X native signals" in home.text
        kol_page = client.get(kol.headers["location"].split("#", 1)[0])
        assert "X Score" in kol_page.text
        assert "Scores are comparable only within X" in kol_page.text
        assert "Discover KOLs" in kol_page.text
        assert "Agent Brief" not in kol_page.text


def test_discover_product_views_preserve_platform_semantics(
    isolated_platform_environment: Path,
) -> None:
    with TestClient(web_app) as client:
        trends = client.get("/trends?category=ai")
        hot_content = client.get("/hot-content?platform=all&source=discover")
        following = client.get("/hot-content?platform=all&source=following")
        x_hot_content = client.get("/hot-content?platform=x&source=discover")
        x_kols = client.get("/kols?platform=x")
        x_watchlist = client.get("/watchlist?platform=x")
        youtube = client.get("/kols?platform=youtube")
        reddit = client.get("/kols?platform=reddit")
        watchlist = client.get("/watchlist")
        watchlist_breakout = client.get("/watchlist?mode=breakout")

        assert trends.status_code == 200
        assert "X trend observations" in trends.text
        assert "X native signals" in trends.text
        assert "Native X ranking" in trends.text
        assert "Hot X posts driving this trend" in trends.text
        assert "View all in Hot Content" in trends.text
        assert "YouTube" not in trends.text
        assert "Reddit" not in trends.text
        assert "Instagram" not in trends.text

        assert hot_content.status_code == 200
        assert "Hot Content" in hot_content.text
        assert "Live X posts use current public metrics" in hot_content.text
        assert 'platform=x' in hot_content.text
        assert "YouTube" not in hot_content.text
        assert "Reddit" not in hot_content.text
        assert "Instagram" not in hot_content.text
        assert "Dedy" not in hot_content.text

        assert following.status_code == 200
        assert "Watched KOL breakouts" in following.text
        assert "No X accounts are enrolled yet" in following.text
        assert 'data-breakout-reason="platform"' not in following.text

        assert x_hot_content.status_code == 200
        assert "X via mock" in x_hot_content.text
        assert "Live public candidates" in x_hot_content.text
        assert 'data-hot-content-row' in x_hot_content.text
        assert 'data-breakout-reason="live"' in x_hot_content.text
        assert "Backend connector" not in x_hot_content.text
        assert "Awaiting history" in x_hot_content.text
        assert "mock posts have been removed" not in x_hot_content.text
        assert x_kols.status_code == 200
        assert "X Score" in x_kols.text
        assert "Discover KOLs" in x_kols.text
        assert "Internal v1" in x_kols.text
        assert "Live · mock" in x_kols.text
        assert "Stable X native account ID resolved" in x_kols.text or "domain" in x_kols.text.lower()
        assert "Agent Brief" not in x_kols.text
        assert x_watchlist.status_code == 200
        assert "No X accounts are enrolled yet" in x_watchlist.text

        assert youtube.status_code == 200
        assert reddit.status_code == 200
        for forced_x_page in (youtube, reddit):
            assert "X Score" in forced_x_page.text
            assert "YouTube" not in forced_x_page.text
            assert "Reddit Score" not in forced_x_page.text
            assert "Instagram" not in forced_x_page.text

        assert watchlist.status_code == 200
        assert "KOL Watchlist" in watchlist.text
        assert "No X accounts are enrolled yet" in watchlist.text
        assert all(label not in watchlist.text for label in ("Video", "Discussion", "Reel"))
        assert "Agent Brief" not in watchlist.text
        assert "Source health" in watchlist.text

        assert watchlist_breakout.status_code == 200
        assert "Breakout Content" in watchlist_breakout.text
        assert "Open in Hot Content" in watchlist_breakout.text
        assert "One eval every agent builder should run" not in watchlist_breakout.text


def test_x_kol_candidate_requires_explicit_watchlist_enrollment(
    isolated_platform_environment: Path,
) -> None:
    with TestClient(web_app) as client:
        discovery = client.get("/kols?platform=x&domain=DeFi")
        match = re.search(
            r'name="native_id" value="([^"]+)"',
            discovery.text,
        )
        assert match is not None
        native_id = match.group(1)

        before = client.get("/watchlist?platform=x")
        assert "No X accounts are enrolled yet" in before.text

        enrolled = client.post(
            "/kols/watchlist",
            data={"platform": "x", "native_id": native_id, "domain": "DeFi"},
            follow_redirects=False,
        )
        assert enrolled.status_code == 303
        assert enrolled.headers["location"].startswith("/watchlist?platform=x")

        after = client.get(enrolled.headers["location"])
        assert "No X accounts are enrolled yet" not in after.text
        assert "Tracked X KOLs" in after.text
        assert 'href="https://x.com/' in after.text


def test_discovery_route_rejects_a_source_the_platform_does_not_support(
    isolated_platform_environment: Path,
) -> None:
    with TestClient(web_app) as client:
        unsupported = client.post(
            "/platforms/xiaohongshu/discover",
            data={"query": "RWA research", "limit": "20", "source": "accounts"},
        )

        assert unsupported.status_code == 422
        assert "does not support account_search discovery" in unsupported.text
        assert web_app.state.automation.list_jobs(limit=20) == []


def test_web_global_and_platform_kill_switches_are_independent(
    isolated_platform_environment: Path,
) -> None:
    with TestClient(web_app) as client:
        global_pause = client.post(
            "/automation/controls/kill-switch",
            data={"enabled": "true"},
            follow_redirects=False,
        )
        assert global_pause.status_code == 303
        assert global_pause.headers["location"] == "/settings/brand#automation-controls"
        assert web_app.state.automation.get_effective_channel_state("x")["allowed"] is False
        center = client.get("/")
        assert "Trend Radar" in center.text

        global_resume = client.post(
            "/automation/controls/kill-switch",
            data={"enabled": "false"},
            follow_redirects=False,
        )
        assert global_resume.status_code == 303
        assert web_app.state.automation.get_effective_channel_state("x")["allowed"] is True

        x_pause = client.post(
            "/automation/controls/kill-switch",
            data={"enabled": "true", "platform_id": "x"},
            follow_redirects=False,
        )
        assert x_pause.status_code == 303
        assert x_pause.headers["location"] == "/settings/brand#automation-controls"
        assert web_app.state.automation.get_effective_channel_state("x")["allowed"] is False
        assert (
            web_app.state.automation.get_effective_channel_state("xiaohongshu")[
                "allowed"
            ]
            is True
        )
        workspace = client.get("/platforms/x")
        assert "X 的写入已暂停" in workspace.text
        assert "暂停平台写入" not in workspace.text
        assert "动作类型开关" not in workspace.text

        safety = client.get("/settings/brand")
        assert "自动化安全控制" in safety.text
        assert "全局写入" in safety.text
        assert "动作类型" in safety.text
        assert 'name="scope" value="platform"' in safety.text


@pytest.mark.parametrize(
    "path",
    (
        "/tasks",
        "/radar/people",
        "/radar/topics",
        "/publishing",
        "/operations",
        "/runs/1",
        "/library",
        "/contacts/1/status",
    ),
)
def test_removed_legacy_routes_are_not_registered_and_never_create_old_schema(
    isolated_platform_environment: Path,
    path: str,
) -> None:
    with TestClient(web_app) as client:
        assert client.get(path).status_code == 404

    with sqlite3.connect(isolated_platform_environment) as connection:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert not {"runs", "contacts", "seed_sets", "owned_posts"} & tables


def test_brand_handles_are_derived_from_registered_platforms(
    isolated_platform_environment: Path,
) -> None:
    with TestClient(web_app) as client:
        saved = client.post(
            "/settings/brand",
            data={
                "brand_name": "Signal Labs",
                "platform_handle_x": "@signal_x",
                "platform_handle_xiaohongshu": "signal-red",
                "forbidden_terms": "guaranteed",
            },
            follow_redirects=False,
        )
        assert saved.status_code == 303
        brand = web_app.state.automation.get_brand_config()
        assert brand["platform_handles"] == {
            "x": "signal_x",
            "xiaohongshu": "signal-red",
        }
        page = client.get("/settings/brand")
        assert page.status_code == 200
        assert 'name="platform_handle_x"' in page.text
        assert 'name="platform_handle_xiaohongshu"' in page.text


def test_workspace_can_dismiss_opportunity_and_resolve_uncertain_action(
    isolated_platform_environment: Path,
) -> None:
    with TestClient(web_app) as client:
        store = web_app.state.automation
        connection_id = store.upsert_platform_connection(
            "x",
            connection_key="test-writer",
            status="connected",
            capabilities=["comment"],
            metadata={"kind": "managed_account"},
        )
        dismissed_opportunity = store.upsert_opportunity(
            "x", "reply", "tweet", "dismiss-from-ui", score=85, priority=85
        )
        pending_action = store.create_action(
            "x",
            "comment",
            "tweet",
            "dismiss-from-ui",
            opportunity_id=dismissed_opportunity,
            connection_id=connection_id,
            initial_status="needs_review",
            idempotency_key="dismiss-from-ui-action",
        )
        dismissed = client.post(
            f"/automation/opportunities/{dismissed_opportunity}/dismiss",
            follow_redirects=False,
        )
        assert dismissed.status_code == 303
        assert store.get_opportunity(dismissed_opportunity)["status"] == "dismissed"
        assert store.get_action(pending_action)["status"] == "cancelled"

        uncertain_opportunity = store.upsert_opportunity(
            "x", "reply", "tweet", "uncertain-from-ui", score=90, priority=90
        )
        uncertain_action = store.create_action(
            "x",
            "comment",
            "tweet",
            "uncertain-from-ui",
            opportunity_id=uncertain_opportunity,
            connection_id=connection_id,
            initial_status="scheduled",
            idempotency_key="uncertain-from-ui-action",
        )
        assert store.claim_next_action("test-worker")["id"] == uncertain_action
        store.record_action_result(
            uncertain_action,
            success=True,
            confirmed=False,
            external_id="provider-receipt-1",
            receipt={"accepted": True},
            error="receipt not confirmed",
        )
        resolved = client.post(
            f"/automation/actions/{uncertain_action}/resolve",
            data={
                "resolution": "succeeded",
                "receipt_url": "https://x.com/brand/status/42",
            },
            follow_redirects=False,
        )
        assert resolved.status_code == 303
        saved = store.get_action(uncertain_action)
        assert saved["status"] == "succeeded"
        assert saved["external_id"] == "provider-receipt-1"
        assert saved["receipt"] == {"accepted": True}
        assert saved["receipt_url"] == "https://x.com/brand/status/42"


def test_admin_settings_registers_http_connections_without_browser_profiles(
    isolated_platform_environment: Path,
) -> None:
    with TestClient(web_app) as client:
        unsupported_browser = client.post(
            "/platforms/x/connections",
            data={
                "connection_kind": "browser",
                "display_name": "Brand X",
                "external_account_id": "brand-1",
                "username": "brand",
                "browser_profile": "brand-profile",
                "capabilities": "comment",
            },
            follow_redirects=False,
        )
        assert unsupported_browser.status_code == 422
        first_postiz = client.post(
            "/platforms/x/connections",
            data={
                "connection_kind": "postiz",
                "display_name": "Publisher One",
                "integration_id": "postiz-1",
                "external_account_id": "publisher-1",
                "username": "publisher_one",
                "is_default": "true",
            },
            follow_redirects=False,
        )
        second_postiz = client.post(
            "/platforms/x/connections",
            data={
                "connection_kind": "postiz",
                "display_name": "Publisher Two",
                "integration_id": "postiz-2",
                "external_account_id": "publisher-2",
                "username": "publisher_two",
            },
            follow_redirects=False,
        )
        assert first_postiz.status_code == 303
        assert second_postiz.status_code == 303
        assert first_postiz.headers["location"] == (
            "/settings/brand?connection=saved#platform-connections"
        )
        managed_connections = [
            row
            for row in web_app.state.automation.list_platform_connections(
                platform_id="x"
            )
            if (row.get("metadata") or {}).get("kind") == "postiz"
        ]
        assert len(managed_connections) == 2
        settings_page = client.get("/settings/brand")
        workspace = client.get("/platforms/x")
        assert "账号通道管理" in settings_page.text
        assert "Publisher One" in settings_page.text
        assert "Publisher Two" in settings_page.text
        assert 'action="/platforms/x/connections"' in settings_page.text
        assert "登记 Browser 写入账号" not in workspace.text
        assert "登记 Postiz 发布账号" not in workspace.text

        unsupported = client.post(
            "/platforms/xiaohongshu/connections",
            data={"connection_kind": "postiz", "integration_id": "red-1"},
        )
        assert unsupported.status_code == 422

    with sqlite3.connect(isolated_platform_environment) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert "runs" not in tables


def test_cli_platforms_and_scan_use_registry_and_shared_job_queue(
    isolated_platform_environment: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner()

    help_result = runner.invoke(cli_app, ["--help"])
    assert help_result.exit_code == 0, help_result.output
    assert "platforms" in help_result.output
    assert "scan" in help_result.output
    assert "db" in help_result.output

    platforms_result = runner.invoke(cli_app, ["platforms"])
    assert platforms_result.exit_code == 0, platforms_result.output
    assert "x\tconnected\t" in platforms_result.output
    assert "account_search" in platforms_result.output
    assert "xiaohongshu\tdisconnected\t" in platforms_result.output
    assert "content_search" in platforms_result.output

    assert "worker" not in help_result.output

    scan_result = runner.invoke(cli_app, ["scan", "--platform", "x"])
    assert scan_result.exit_code == 0, scan_result.output
    assert "x scan #" in scan_result.output
    jobs = AutomationStore(isolated_platform_environment).list_jobs(limit=10)
    assert len(jobs) == 1
    assert jobs[0]["platform_id"] == "x"
    assert jobs[0]["job_type"] == "signal_refresh"
    assert jobs[0]["status"] == "succeeded"

    invalid_scan = runner.invoke(
        cli_app, ["scan", "--platform", "youtube"]
    )
    assert invalid_scan.exit_code != 0
    assert "Unknown platform: youtube" in invalid_scan.output


def test_cli_db_rebuild_preserves_old_database_by_default(
    isolated_platform_environment: Path,
) -> None:
    with sqlite3.connect(isolated_platform_environment) as connection:
        connection.execute("CREATE TABLE old_marker(value TEXT)")
        connection.execute("INSERT INTO old_marker VALUES('recoverable')")

    result = CliRunner().invoke(cli_app, ["db", "rebuild", "--backup"])
    assert result.exit_code == 0, result.output
    assert f"Rebuilt {isolated_platform_environment}" in result.output
    assert "Backup:" in result.output

    backups = list(
        isolated_platform_environment.parent.glob(
            f"{isolated_platform_environment.name}.backup-*"
        )
    )
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as connection:
        assert connection.execute("SELECT value FROM old_marker").fetchone() == (
            "recoverable",
        )
    with sqlite3.connect(isolated_platform_environment) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert {"automation_jobs", "x_accounts", "xhs_users"} <= tables
    assert "runs" not in tables

    # Normal startup and registry CLI cannot resurrect removed business tables.
    with TestClient(web_app) as client:
        assert client.get("/").status_code == 200
    assert CliRunner().invoke(cli_app, ["platforms"]).exit_code == 0
    with sqlite3.connect(isolated_platform_environment) as connection:
        tables_after_startup = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert "runs" not in tables_after_startup
