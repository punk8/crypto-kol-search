from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from kol_search.platforms.x import X_MANIFEST
from kol_search.platforms.xiaohongshu import XIAOHONGSHU_MANIFEST


TEMPLATE_DIR = Path(__file__).parents[1] / "src" / "kol_search" / "templates"


def _environment() -> Environment:
    return Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        autoescape=select_autoescape(["html"]),
    )


def test_platform_manifests_own_thin_workspace_templates():
    assert X_MANIFEST.metadata["workspace_template"] == "platform_x_workspace.html"
    assert (
        XIAOHONGSHU_MANIFEST.metadata["workspace_template"]
        == "platform_xiaohongshu_workspace.html"
    )

    environment = _environment()
    for template_name in (
        "platform_x_workspace.html",
        "platform_xiaohongshu_workspace.html",
    ):
        source, _, _ = environment.loader.get_source(environment, template_name)
        assert '{% extends "platform_workspace.html" %}' in source


def test_platform_center_renders_independent_platform_summaries():
    template = _environment().get_template("platform_center.html")
    html = template.render(
        urls={"kill_switch": "/controls/kill-switch"},
        global_kill_switch=False,
        capability_labels={
            "content_search": "内容搜索",
            "comment": "评论",
            "analytics": "效果分析",
        },
        summary={
            "enabled_platforms": 2,
            "total_platforms": 2,
            "active_tasks": 1,
            "queued_tasks": 3,
            "open_opportunities": 9,
            "alert_count": 1,
            "paused_channels": 1,
            "failed_tasks": 0,
        },
        platforms=[
            {
                "platform_id": "x",
                "name": "X",
                "short_name": "X",
                "version": "1.0",
                "connection_status": "connected",
                "connection_message": "",
                "enabled": True,
                "workspace_url": "/platforms/x",
                "capabilities": ["content_search", "comment"],
                "last_scan_at": "2026-07-21 09:30",
                "metrics": {
                    "active_kols": 42,
                    "open_opportunities": 6,
                    "succeeded_actions_today": 2,
                    "paused_channels": 0,
                },
            },
            {
                "platform_id": "xiaohongshu",
                "name": "小红书",
                "short_name": "红书",
                "version": "1.0",
                "connection_status": "degraded",
                "connection_message": "评论通道已暂停",
                "enabled": True,
                "workspace_url": "/platforms/xiaohongshu",
                "capabilities": ["content_search", "analytics"],
                "last_scan_at": None,
                "metrics": {
                    "active_kols": 18,
                    "open_opportunities": 3,
                    "succeeded_actions_today": 0,
                    "paused_channels": 1,
                },
            },
        ],
        alerts=[
            {
                "severity": "warning",
                "title": "通道已暂停",
                "message": "连续写入失败",
                "platform_name": "小红书",
                "created_at": "10:15",
                "href": "/platforms/xiaohongshu#automation",
            }
        ],
        recent_runs=[
            {
                "id": 7,
                "platform_short_name": "X",
                "label": "信号扫描",
                "created_at": "09:30",
                "progress": 75,
                "status": "running",
                "detail_url": "/runs/7",
            }
        ],
    )

    assert "平台中心" in html
    assert "这里只汇总运行健康度" in html
    assert "/platforms/x" in html
    assert "/platforms/xiaohongshu" in html
    assert "评论通道已暂停" in html
    assert "/static/platforms.css" in html


def test_workspace_hides_search_controls_when_platform_lacks_search():
    template = _environment().get_template("platform_workspace.html")
    html = template.render(
        platform={
            "platform_id": "video_test",
            "name": "Video Test",
            "short_name": "VT",
            "version": "0.1",
            "accent_color": "#7357d8",
            "description": "仅用于验证视频平台独立模型。",
            "connection_status": "connected",
            "connection_message": "",
            "kill_switch": False,
            "capabilities": ["timeline/feed", "analytics"],
            "scan_interval_label": "每 30 分钟",
            "safety_summary": "当前仅开放只读能力。",
        },
        urls={
            "center": "/platforms",
            "scan": "/platforms/video_test/scan",
            "kill_switch": "/platforms/video_test/kill-switch",
            "opportunities": "/platforms/video_test/opportunities",
            "discover": "/platforms/video_test/discover",
            "kols": "/platforms/video_test/kols",
            "accounts": "/platforms/video_test/accounts",
            "actions": "/platforms/video_test/actions",
            "runs": "/platforms/video_test/runs",
        },
        metrics={
            "active_kols": 2,
            "review_kols": 0,
            "open_opportunities": 0,
            "planned_opportunities": 0,
            "succeeded_actions_today": 0,
            "review_actions": 0,
            "next_scan_at": "10:30",
        },
        alerts=[],
        opportunities=[],
        discovery={"default_query": "", "default_limit": 20},
        kols=[],
        accounts=[],
        recent_actions=[],
        recent_runs=[],
        all_capabilities=[
            {"id": "account_search", "label": "账号搜索"},
            {"id": "timeline/feed", "label": "时间线 / Feed"},
            {"id": "comment", "label": "评论"},
            {"id": "analytics", "label": "效果分析"},
        ],
    )

    assert "Video Test 工作台" in html
    assert "尚未开放搜索能力" in html
    assert "name=\"query\"" not in html
    assert "timeline/feed" not in html  # The localized capability label is rendered instead.
    assert "时间线 / Feed" in html
    assert "当前仅开放只读能力" in html


def test_workspace_renders_native_evidence_and_only_provided_actions():
    template = _environment().get_template("platform_workspace.html")
    html = template.render(
        platform={
            "platform_id": "x",
            "name": "X",
            "short_name": "X",
            "version": "1.0",
            "accent_color": "#17211d",
            "description": "X KOL 发现与互动。",
            "connection_status": "connected",
            "connection_message": "",
            "kill_switch": False,
            "capabilities": ["account_search", "content_search", "comment"],
            "scan_interval_label": "每 30 分钟",
            "safety_summary": "写入不自动重试。",
        },
        urls={
            "center": "/platforms",
            "scan": "/platforms/x/scan",
            "kill_switch": "/platforms/x/kill-switch",
            "opportunities": "/platforms/x/opportunities",
            "discover": "/platforms/x/discover",
            "kols": "/platforms/x/kols",
            "accounts": "/platforms/x/accounts",
            "actions": "/platforms/x/actions",
            "runs": "/platforms/x/runs",
        },
        metrics={
            "active_kols": 1,
            "review_kols": 1,
            "open_opportunities": 1,
            "planned_opportunities": 0,
            "succeeded_actions_today": 0,
            "review_actions": 1,
            "next_scan_at": "10:30",
        },
        alerts=[],
        opportunities=[
            {
                "priority": 91,
                "kind_label": "评论",
                "title": "回复 @researcher",
                "status": "needs_review",
                "summary": "针对最新 RWA 讨论的互动机会。",
                "reasons": ["24h 互动窗口", "Active KOL"],
                "native_object_label": "Tweet 123",
                "native_url": "https://x.com/researcher/status/123",
                "created_at": "10:00",
                "review_url": "/platforms/x/actions/5",
                "review_label": "审核草稿",
            }
        ],
        discovery={"default_query": "RWA", "default_limit": 20},
        kols=[
            {
                "detail_url": "/platforms/x/kols/42",
                "initials": "RE",
                "display_name": "Researcher",
                "handle": "@researcher",
                "native_id": "42",
                "score": 0.86,
                "status": "active",
            }
        ],
        accounts=[],
        recent_actions=[],
        recent_runs=[],
        all_capabilities=[
            {"id": "account_search", "label": "账号搜索"},
            {"id": "comment", "label": "评论"},
            {"id": "dm", "label": "私信"},
        ],
    )

    assert "https://x.com/researcher/status/123" in html
    assert "Tweet 123" in html
    assert "审核草稿" in html
    assert "name=\"query\"" in html
    assert "账号搜索" in html
    assert "私信" in html and "未接入" in html
