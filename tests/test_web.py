import time
from pathlib import Path

from fastapi.testclient import TestClient

from kol_search.models import Post
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

        sender = client.post(
            "/settings/x-senders",
            data={
                "label": "Signal Labs",
                "x_handle": "signal_labs",
                "sender_type": "brand",
                "send_method": "mock",
                "enabled": "true",
                "comment_auto_publish": "true",
                "daily_comment_limit": "10",
                "daily_dm_limit": "10",
            },
            follow_redirects=False,
        )
        assert sender.status_code == 303
        sender_id = app.state.store.list_x_senders()[0]["id"]

        started = client.post(
            "/signal-scans",
            data={
                "backend": "mock",
                "sender_account_id": str(sender_id),
                "comment_style": "brand",
                "publish_mode": "review",
                "campaign_goal": "Introduce LaunchVibes when relevant",
            },
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
        source_context = " ".join(opportunity["text"].split()[:4])
        grounded = f"{source_context} — creator workflow with LaunchVibes"
        edited = client.post(
            f"/reply-opportunities/{opportunity['id']}",
            data={"draft": grounded},
            follow_redirects=False,
        )
        assert edited.status_code == 303
        assert app.state.store.list_reply_opportunities(status="all")[0]["draft"] == grounded

        validated = client.post(
            f"/reply-opportunities/{opportunity['id']}",
            data={
                "draft": grounded,
                "status": "validated",
                "sender_account_id": str(sender_id),
                "comment_style": "brand",
                "publish_mode": "review",
                "campaign_goal": "Introduce LaunchVibes when relevant",
            },
            follow_redirects=False,
        )
        assert validated.status_code == 303
        assert app.state.store.get_reply_opportunity(opportunity["id"])["status"] == "validated"

        published = client.post(
            f"/reply-opportunities/{opportunity['id']}/publish",
            follow_redirects=False,
        )
        assert published.status_code == 303, published.text
        saved = app.state.store.get_reply_opportunity(opportunity["id"])
        assert saved["status"] == "replied"
        assert saved["reply_url"].startswith("https://x.com/signal_labs/status/")


def test_web_dm_batch_sender_and_account_history(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("KOL_DB_PATH", str(tmp_path / "dm-web.db"))
    monkeypatch.setenv("TWITTER_BACKEND", "mock")
    monkeypatch.setenv("KOL_ENABLE_WEEKLY_REFRESH", "false")
    with TestClient(app) as client:
        empty_senders = client.get("/settings/x-senders")
        assert empty_senders.status_code == 200
        assert "新增发送账号" in empty_senders.text
        assert "创建一个新的 X sender 配置" in empty_senders.text
        assert "已保存的发送账号" in empty_senders.text
        assert "查看、启用或修改现有 sender 配置" in empty_senders.text
        assert "还没有已保存的发送账号。" in empty_senders.text
        assert "例如：LaunchVibes Official" in empty_senders.text
        assert "例如：@launchvibes" in empty_senders.text
        assert "例如：launchvibes-test" in empty_senders.text
        assert "仅用于系统内识别，不会显示在 X 上。" in empty_senders.text
        assert "真实 X DM 接入后生效，当前 dry run 不计入此限制。" in empty_senders.text
        assert 'name="daily_dm_limit" type="number" min="0" value="10" readonly' in empty_senders.text
        assert "REAL X DM BLOCKED" in empty_senders.text

        alice = app.state.store.upsert_account(
            {"id": "101", "username": "alice", "name": "Alice"},
            {"account_type": "person"},
        )
        bob = app.state.store.upsert_account(
            {"id": "102", "username": "bob", "name": None},
            {"account_type": "person"},
        )
        saved = client.post(
            "/settings/x-senders",
            data={
                "label": "Dry Run",
                "x_handle": "florus",
                "sender_type": "brand",
                "send_method": "mock",
                "enabled": "true",
                "daily_comment_limit": "5",
                "daily_dm_limit": "5",
            },
            follow_redirects=False,
        )
        assert saved.status_code == 303
        sender_id = app.state.store.list_x_senders()[0]["id"]
        sender_page = client.get("/settings/x-senders")
        assert "Dry Run" in sender_page.text
        assert '<details class="card sender-card">' in sender_page.text
        assert "<summary>" in sender_page.text
        assert "Auto publish: disabled" in sender_page.text
        assert "Comments/day: 5" in sender_page.text
        assert "编辑已保存账号" in sender_page.text
        assert "更新账号" in sender_page.text
        saved_card = sender_page.text.split('<details class="card sender-card">', 1)[1]
        assert "data-opencli-only hidden" in saved_card
        assert "当前发送方式不会使用此限制。" in saved_card

        library = client.get("/library")
        assert 'name="account_ids"' in library.text
        preview = client.post(
            "/outreach/dm/new",
            data={
                "account_ids": [alice, bob],
                "sender_account_id": str(sender_id),
                "template_key": "launchvibes_invitation",
            },
        )
        assert preview.status_code == 200
        assert "Hi Alice" in preview.text
        assert "Hi bob" in preview.text

        created = client.post(
            "/outreach/dm",
            data={
                "account_ids": [alice, bob],
                "sender_account_id": str(sender_id),
                "template_key": "launchvibes_invitation",
            },
            follow_redirects=False,
        )
        assert created.status_code == 303
        batch_url = created.headers["location"]
        batch = client.get(batch_url)
        assert batch.status_code == 200
        assert "DRY RUN — NOT SENT TO X" in batch.text
        started = client.post(f"{batch_url}/start", follow_redirects=False)
        assert started.status_code == 303
        for _ in range(50):
            saved_batch = app.state.store.get_dm_batch(int(batch_url.rsplit("/", 1)[1]))
            if saved_batch["status"] == "completed":
                break
            time.sleep(0.05)
        assert saved_batch["sent_count"] == 0
        assert saved_batch["skipped_count"] == 2
        account = client.get(f"/accounts/{alice}")
        assert "X Outreach" in account.text
        assert "launchvibes_invitation" in account.text
        assert "DRY RUN — NOT SENT TO X" in account.text


def test_people_radar_default_view_displays_publish_audit(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("KOL_DB_PATH", str(tmp_path / "radar-audit.db"))
    monkeypatch.setenv("TWITTER_BACKEND", "mock")
    monkeypatch.setenv("KOL_ENABLE_WEEKLY_REFRESH", "false")
    monkeypatch.setenv("KOL_ENABLE_SIGNAL_SCAN", "false")
    with TestClient(app) as client:
        store = app.state.store
        account_id = store.upsert_account(
            {"id": "501", "username": "alice", "name": "Alice"}
        )
        sender_id = store.save_x_sender(
            label="LaunchVibes", x_handle="launchvibes", sender_type="brand",
            send_method="mock", enabled=True, comment_auto_publish=False,
            daily_comment_limit=10, daily_dm_limit=0,
        )
        store.save_posts([
            Post(id="5011", author_id=account_id, author_username="alice", text="Creator workflow planning", created_at="2026-07-20T01:00:00+00:00", url="https://x.com/alice/status/5011"),
            Post(id="5012", author_id=account_id, author_username="alice", text="Content planning systems", created_at="2026-07-20T02:00:00+00:00", url="https://x.com/alice/status/5012"),
            Post(id="5013", author_id=account_id, author_username="alice", text="Creator growth workflow", created_at="2026-07-20T03:00:00+00:00", url="https://x.com/alice/status/5013"),
            Post(id="5014", author_id=account_id, author_username="alice", text="Creator review queue", created_at="2026-07-20T04:00:00+00:00", url="https://x.com/alice/status/5014"),
        ])

        def opportunity(post_id: str, draft: str, status: str = "validated") -> int:
            return store.upsert_reply_opportunity(
                post_id=post_id, account_id=account_id, score=10, language="en",
                score_payload={}, reasons=[], draft=draft, draft_source="openai",
                expires_at="2099-01-01T00:00:00+00:00",
                sender_account_id=sender_id, comment_style="brand",
                publish_mode="review", campaign_goal="Audit test",
                suitable=True, suitability_reason="Relevant creator workflow",
                validation_status="passed", validation_reason="Validated",
                initial_status=status,
            )

        published_id = opportunity("5011", "Exact published LaunchVibes comment")
        store.update_reply_opportunity(
            published_id, status="publishing", send_backend="mock"
        )
        store.update_reply_opportunity(
            published_id, status="replied",
            reply_url="https://x.com/launchvibes/status/9101",
        )

        failed_id = opportunity("5012", "Exact failed LaunchVibes comment")
        store.update_reply_opportunity(failed_id, status="publishing", send_backend="opencli")
        store.update_reply_opportunity(
            failed_id, status="failed", sanitized_error="Sanitized provider failure"
        )

        ambiguous_id = opportunity("5013", "Exact ambiguous LaunchVibes comment")
        store.update_reply_opportunity(
            ambiguous_id, status="publishing", send_backend="opencli"
        )
        store.update_reply_opportunity(
            ambiguous_id, status="confirmation_required",
            sanitized_error="Receipt could not be confirmed",
        )
        opportunity("5014", "Actionable review-required comment", "review_required")

        response = client.get("/radar/people")
        assert response.status_code == 200
        assert "Exact published LaunchVibes comment" in response.text
        assert "Actionable review-required comment" in response.text
        assert "Published by @launchvibes" in response.text
        assert "Published at:" in response.text
        assert "Backend: mock" in response.text
        assert "https://x.com/launchvibes/status/9101" in response.text
        assert "Failed" in response.text
        assert "Backend: opencli" in response.text
        assert "Sanitized provider failure" in response.text
        assert "Confirmation required" in response.text
        assert "No automatic retry was performed" in response.text
        assert "Receipt could not be confirmed" in response.text
        assert response.text.count('name="reply_url"') == 1
        assert "raw_provider_payload" not in response.text

        account = client.get(f"/accounts/{account_id}")
        assert "Exact failed LaunchVibes comment" in account.text
        assert "Sanitized provider failure" in account.text


def test_outreach_pages_explain_profile_and_sender_identity_context(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("KOL_DB_PATH", str(tmp_path / "outreach-context.db"))
    monkeypatch.setenv("TWITTER_BACKEND", "mock")
    monkeypatch.setenv("KOL_ENABLE_WEEKLY_REFRESH", "false")
    monkeypatch.setenv("KOL_ENABLE_SIGNAL_SCAN", "false")
    with TestClient(app) as client:
        brand_page = client.get("/settings/brand")
        assert brand_page.status_code == 200
        assert "review 模式需要人工发布" in brand_page.text
        assert "auto 模式仅在人物雷达中明确选择" in brand_page.text
        assert "每日限额检查全部通过后才会发布" in brand_page.text

        sender_page = client.get("/settings/x-senders")
        assert sender_page.status_code == 200
        assert "用于该 sender 的评论发布和身份验证，与全局 discovery profile 分开" in sender_page.text

        store = app.state.store
        store.save_brand_profile(
            brand_name="LaunchVibes",
            x_handle="launchvibes",
            description="Creator growth tools",
            audience="Creators",
            tone="concise",
            allowed_claims=[],
            forbidden_terms=[],
        )
        account_id = store.upsert_account(
            {"id": "context-501", "username": "creator", "name": "Creator"}
        )
        sender_id = store.save_x_sender(
            label="Founder account",
            x_handle="launchvibes_founder",
            sender_type="founder",
            send_method="mock",
            enabled=True,
            comment_auto_publish=False,
            daily_comment_limit=1,
            daily_dm_limit=0,
        )
        store.save_posts([
            Post(
                id="context-post-501",
                author_id=account_id,
                author_username="creator",
                text="Creator workflow planning",
                created_at="2026-07-20T01:00:00+00:00",
                url="https://x.com/creator/status/context-post-501",
            )
        ])
        store.upsert_reply_opportunity(
            post_id="context-post-501",
            account_id=account_id,
            score=10,
            language="en",
            score_payload={},
            reasons=[],
            draft="A context-aware draft",
            draft_source="openai",
            expires_at="2099-01-01T00:00:00+00:00",
            sender_account_id=sender_id,
            comment_style="brand",
            publish_mode="review",
            campaign_goal="Context clarity",
            suitable=True,
            suitability_reason="Relevant",
            validation_status="passed",
            validation_reason="Validated",
            initial_status="validated",
        )

        radar = client.get("/radar/people")
        assert radar.status_code == 200
        assert 'data-brand-handle="launchvibes"' in radar.text
        assert 'data-sender-handle="launchvibes_founder"' in radar.text
        assert "生成使用的品牌身份：@launchvibes" in radar.text
        assert "实际发布 sender：@launchvibes_founder" in radar.text
        assert "请确认回复措辞符合实际 sender 身份" in radar.text
        assert "此机会已绑定 @launchvibes_founder" in radar.text
        assert "后续扫描选择其他 sender 时不会自动替换" in radar.text
