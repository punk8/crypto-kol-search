from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from kol_search.db import Store
from kol_search.discovery.multiplatform import expand_query, select_window
from kol_search.models import Account, Post
from kol_search.outbound import (
    OutboundError,
    OutboundReceipt,
    OutboundService,
    outbound_idempotency_key,
)
from kol_search.platforms.opencli import OpenCliXiaohongshuReader
from kol_search.settings import Settings


def test_store_migrates_platform_identity_without_cross_platform_handle_collision(tmp_path: Path):
    store = Store(tmp_path / "platform.db")
    x_id = store.upsert_account(Account(id="42", username="alice").model_dump())
    xhs_id = store.upsert_account(
        Account(
            id="xiaohongshu:42",
            platform="xiaohongshu",
            external_id="42",
            username="alice",
        ).model_dump()
    )
    assert x_id == "42"
    assert xhs_id == "xiaohongshu:42"
    assert store.get_account(x_id)["platform"] == "x"
    assert store.get_account(xhs_id)["platform"] == "xiaohongshu"


def test_query_expansion_and_recent_window_are_real_and_explainable():
    aliases = expand_query("RWA")
    assert "real world assets" in aliases
    now = datetime.now(timezone.utc)
    posts = [
        Post(
            id=f"p{day}",
            author_id="a",
            url=f"https://x.com/a/status/{day}",
            created_at=(now - timedelta(days=day)).isoformat(),
        )
        for day in (0, 2, 10)
    ]
    posts.append(Post(id="undated", author_id="a", url="https://x.com/a/status/99"))
    selected, days, undated = select_window(posts, target=2)
    assert days == 7
    assert {post.id for post in selected} == {"p0", "p2"}
    assert undated == 1


def test_xiaohongshu_reader_maps_real_rows_and_keeps_note_identity():
    def runner(command, **kwargs):  # noqa: ANN001
        assert "xiaohongshu" in command
        payload = [
            {
                "title": "RWA 资产代币化观察",
                "author": "研究员小红",
                "userId": "user-1",
                "likes": "1.2万",
                "published_at": "2026-07-18",
                "url": "https://www.xiaohongshu.com/explore/note123?xsec_token=ok",
            }
        ]
        return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")

    reader = OpenCliXiaohongshuReader(profile="xhs-test", runner=runner)
    post = reader.search_posts("RWA", 10)[0]
    assert post.id == "xiaohongshu:note123"
    assert post.author_id == "xiaohongshu:user-1"
    assert post.like_count == 12_000
    assert post.platform == "xiaohongshu"
    assert post.source_provider == "opencli"


class _Writer:
    def reply(self, action):  # noqa: ANN001
        return OutboundReceipt(
            status="sent",
            url="https://x.com/brand/status/999",
            external_id="999",
            raw={"ok": True},
        )

    def send_dm(self, action):  # noqa: ANN001
        return OutboundReceipt(status="sent", external_id="dm-1", raw={"ok": True})

    def verify_receipt(self, action, receipt):  # noqa: ANN001
        return True


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        KOL_DB_PATH=str(tmp_path / "outbound.db"),
        KOL_ENABLE_MOCK_BACKEND=True,
        KOL_LIVE_WRITE_ENABLED=True,
        KOL_APPROVED_PRODUCT_DOMAINS="example.com",
        KOL_GLOBAL_KILL_SWITCH=False,
    )


def test_outbound_requires_approval_is_idempotent_and_records_receipt(tmp_path: Path):
    settings = _settings(tmp_path)
    store = Store(settings.db_path())
    account_id = store.upsert_managed_account(
        platform="x",
        external_account_id="brand",
        username="brand",
        browser_profile="brand-profile",
        roles=["engagement", "official_dm"],
    )
    key = outbound_idempotency_key(
        platform="x", kind="comment", target_external_id="post-1", text="Useful context"
    )
    action_id = store.create_outbound_action(
        kind="comment",
        platform="x",
        managed_account_id=account_id,
        target_external_id="post-1",
        target_author_id="author-1",
        target_url="https://x.com/author/status/1",
        draft="Useful context",
        idempotency_key=key,
    )
    assert store.create_outbound_action(
        kind="comment",
        platform="x",
        managed_account_id=account_id,
        target_external_id="post-1",
        target_author_id="author-1",
        target_url="https://x.com/author/status/1",
        draft="Useful context",
        idempotency_key=key,
    ) == action_id
    with pytest.raises(OutboundError, match="尚未逐条审批"):
        OutboundService(store, settings, _Writer()).execute(action_id)
    store.approve_outbound_action(action_id, final_text="Useful context", actor="admin")
    OutboundService(store, settings, _Writer()).execute(action_id)
    saved = store.get_outbound_action(action_id)
    assert saved["status"] == "sent"
    assert saved["receipt_url"].endswith("/999")
    assert any(event["action"] == "outbound.sent" for event in store.list_audit_events())


def test_outbound_kill_switch_comment_links_and_dm_domain_are_enforced(tmp_path: Path):
    settings = _settings(tmp_path)
    store = Store(settings.db_path())
    account_id = store.upsert_managed_account(
        platform="x",
        external_account_id="brand",
        username="brand",
        browser_profile="brand-profile",
        roles=["engagement", "official_dm"],
    )
    comment_id = store.create_outbound_action(
        kind="comment",
        platform="x",
        managed_account_id=account_id,
        target_external_id="post-link",
        target_author_id="author-link",
        target_url="https://x.com/author/status/2",
        draft="Visit https://example.com",
        idempotency_key="comment-link",
    )
    store.approve_outbound_action(
        comment_id, final_text="Visit https://example.com", actor="admin"
    )
    with pytest.raises(OutboundError, match="禁止链接"):
        OutboundService(store, settings, _Writer()).execute(comment_id)

    dm_id = store.create_outbound_action(
        kind="dm",
        platform="x",
        managed_account_id=account_id,
        target_external_id="lead",
        target_author_id="lead",
        target_url="https://x.com/lead",
        draft="Official product: https://evil.example.net",
        idempotency_key="dm-link",
    )
    store.approve_outbound_action(
        dm_id, final_text="Official product: https://evil.example.net", actor="admin"
    )
    with pytest.raises(OutboundError, match="域名未获批准"):
        OutboundService(store, settings, _Writer()).execute(dm_id)

    store.set_control("global_kill_switch", "true", actor="admin")
    safe_dm = store.create_outbound_action(
        kind="dm",
        platform="x",
        managed_account_id=account_id,
        target_external_id="lead-2",
        target_author_id="lead-2",
        target_url="https://x.com/lead-2",
        draft="Official product: https://example.com",
        idempotency_key="dm-safe",
    )
    store.approve_outbound_action(
        safe_dm, final_text="Official product: https://example.com", actor="admin"
    )
    with pytest.raises(OutboundError, match="Kill Switch"):
        OutboundService(store, settings, _Writer()).execute(safe_dm)
