from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from kol_search.automation import AgentStore, AutomationStore
from kol_search.automation.agents import AgentConfig, RuntimeAgentConfig
from kol_search.agent_runtime import (
    AgentModelClient,
    AgentRuntimeService,
    CandidateReply,
    OriginalPost,
)
from kol_search.platforms import PlatformTaskResult
from kol_search.platforms.x import XTweet


def _connections(automation: AutomationStore) -> tuple[int, int]:
    reply = automation.upsert_platform_connection(
        "x",
        connection_key="managed-test",
        display_name="@agent reply",
        status="connected",
        capabilities=["comment"],
        metadata={
            "kind": "managed_account",
            "username": "agent",
            "external_account_id": "42",
            "browser_profile": "agent-test",
        },
    )
    publish = automation.upsert_platform_connection(
        "x",
        connection_key="postiz-test",
        display_name="@agent publish",
        status="connected",
        capabilities=["owned_publish"],
        metadata={
            "kind": "postiz",
            "integration_id": "postiz-42",
            "username": "agent",
            "external_account_id": "42",
        },
    )
    return reply, publish


def _agent(store: AgentStore, automation: AutomationStore) -> int:
    reply, publish = _connections(automation)
    return store.create_agent(
        name="DeFi Researcher",
        platform_id="x",
        native_account_id="42",
        native_username="agent",
        reply_connection_id=reply,
        publish_connection_id=publish,
        config={**store.get_defaults(), "model": "test-model"},
        actor="tester",
    )


def test_agent_is_one_to_one_versioned_and_pauses_both_connections(tmp_path: Path) -> None:
    automation = AutomationStore(tmp_path / "agent.db")
    store = AgentStore(automation)
    agent_id = _agent(store, automation)

    agent = store.get_agent(agent_id)
    assert agent is not None
    assert agent["status"] == "draft"
    assert agent["config"]["reply_daily_limit"] == 24
    assert automation.get_account_quota(agent["reply_connection_id"], "comment") == {
        "connection_id": agent["reply_connection_id"],
        "action_type": "comment",
        "hourly_limit": 3,
        "daily_limit": 24,
        "cooldown_seconds": 7 * 86400,
        "updated_at": automation.get_account_quota(
            agent["reply_connection_id"], "comment"
        )["updated_at"],
    }

    store.set_status(agent_id, "running", actor="tester")
    assert store.get_agent(agent_id)["status"] == "running"  # type: ignore[index]
    assert automation.get_effective_channel_state(
        "x", connection_id=agent["reply_connection_id"], action_type="comment"
    )["allowed"] is True

    store.record_failure(agent_id, "temporary failure")
    store.set_status(agent_id, "auto_paused", actor="system", reason="test")
    store.set_status(agent_id, "running", actor="tester", reason="recovered")
    assert store.get_agent(agent_id)["consecutive_failures"] == 0  # type: ignore[index]

    with pytest.raises(ValueError, match="Pause"):
        store.save_config(agent_id, agent["config"], actor="tester")

    store.set_status(agent_id, "paused", actor="tester", reason="edit")
    changed = {**agent["config"], "tone": "direct and analytical"}
    store.save_config(agent_id, changed, actor="tester")
    updated = store.get_agent(agent_id)
    assert updated is not None
    assert updated["config"]["version"] == 2
    assert updated["config"]["tone"] == "direct and analytical"
    assert automation.get_effective_channel_state(
        "x", connection_id=agent["reply_connection_id"], action_type="comment"
    )["allowed"] is False


def test_agent_activity_is_connection_scoped_and_cursor_paginated(tmp_path: Path) -> None:
    automation = AutomationStore(tmp_path / "timeline.db")
    store = AgentStore(automation)
    agent_id = _agent(store, automation)
    agent = store.get_agent(agent_id)
    assert agent is not None
    for index in range(3):
        opportunity_id = automation.upsert_opportunity(
            "x",
            "reply",
            "post",
            f"tweet-{index}",
            score=90,
            payload={"text": f"target {index}", "target_url": f"https://x.com/x/status/{index}"},
            idempotency_key=f"opportunity-{index}",
        )
        action_id = automation.create_action(
            "x",
            "comment",
            "post",
            f"tweet-{index}",
            opportunity_id=opportunity_id,
            connection_id=agent["reply_connection_id"],
            draft=f"reply {index}",
            payload={"target_author_id": f"author-{index}"},
            initial_status="scheduled",
            idempotency_key=f"action-{index}",
            now=f"2026-07-23T0{index}:00:00+00:00",
        )
        store.attach_opportunity(opportunity_id, agent_id, agent["current_config_version_id"])
        store.attach_action(action_id, agent_id, agent["current_config_version_id"])

    first = store.timeline(agent_id, limit=2)
    second = store.timeline(
        agent_id,
        before_created_at=first[-1]["created_at"],
        before_id=first[-1]["id"],
        limit=2,
    )
    assert [item["draft"] for item in first] == ["reply 2", "reply 1"]
    assert [item["draft"] for item in second] == ["reply 0"]
    assert second[0]["opportunity_payload"]["text"] == "target 0"
    assert store.usage_counts(
        agent_id,
        day_start="2026-07-23T00:00:00+00:00",
        hour_start="2026-07-23T01:30:00+00:00",
    ) == {"replies_today": 3, "replies_hour": 1, "posts_today": 0}


def test_global_defaults_version_inheriting_agents_without_stopping_them(tmp_path: Path) -> None:
    automation = AutomationStore(tmp_path / "defaults.db")
    store = AgentStore(automation)
    agent_id = _agent(store, automation)
    store.set_status(agent_id, "running", actor="tester")

    defaults = {**store.get_defaults(), "reply_daily_limit": 20}
    store.save_defaults(defaults)

    agent = store.get_agent(agent_id)
    assert agent is not None
    assert agent["status"] == "running"
    assert agent["config"]["version"] == 2
    assert agent["config"]["reply_daily_limit"] == 20
    assert automation.get_account_quota(
        agent["reply_connection_id"], "comment"
    )["daily_limit"] == 20


def test_local_runtime_config_requires_private_permissions(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "model": {
                    "api_key": "secret",
                    "base_url": "https://api.openai.com/v1",
                    "allowed_models": ["test-model"],
                },
                "telegram": {"enabled": False},
            }
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)
    assert RuntimeAgentConfig.load(path).model.model_ids() == ("test-model",)
    path.chmod(0o644)
    with pytest.raises(RuntimeError, match="0600"):
        RuntimeAgentConfig.load(path)


def test_agent_config_enforces_v1_hard_limits() -> None:
    with pytest.raises(ValueError):
        AgentConfig(reply_hourly_limit=4)
    with pytest.raises(ValueError):
        AgentConfig(reply_daily_limit=25)
    with pytest.raises(ValueError):
        AgentConfig(original_posts_daily=4, original_post_times=["09:30"])
    with pytest.raises(ValueError):
        OriginalPost(text="x" * 271)


def test_compatible_model_response_accepts_fenced_validated_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "runtime.json"
    path.write_text(
        json.dumps(
            {
                "model": {"api_key": "test-only", "allowed_models": ["test-model"]},
                "telegram": {"enabled": False},
            }
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)
    client = AgentModelClient(RuntimeAgentConfig.load(path))
    response = SimpleNamespace(
        output_text=(
            "Result:\n```json\n"
            '{"replies":[{"object_id":"tweet-1","score":91,"safe":true,'
            '"reply":"Useful context.","rationale":"Specific.","safety_reason":""}]}'
            "\n```"
        )
    )
    api = SimpleNamespace(responses=SimpleNamespace(create=lambda **kwargs: response))
    monkeypatch.setattr(client, "_client", lambda config: api)

    replies = client.rank_and_reply(
        AgentConfig(model="test-model"),
        [{"object_id": "tweet-1", "text": "A target post"}],
    )

    assert replies[0].reply == "Useful context."
    assert replies[0].score == 91


def test_migrated_reply_planner_generates_one_contextual_draft(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "runtime.json"
    path.write_text(
        json.dumps(
            {
                "model": {"api_key": "test-only", "allowed_models": ["test-model"]},
                "telegram": {"enabled": False},
            }
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)
    client = AgentModelClient(RuntimeAgentConfig.load(path))
    calls: list[dict[str, object]] = []

    def create(**kwargs):  # noqa: ANN003, ANN202
        calls.append(kwargs)
        return SimpleNamespace(
            output_text=(
                '{"object_id":"tweet-1","score":94,"safe":true,'
                '"reply":"The settlement layer is the real bottleneck here.",'
                '"rationale":"Adds a specific systems perspective.",'
                '"reply_type":"add evidence",'
                '"follow_up_content_idea":"Compare settlement latency across designs."}'
            )
        )

    api = SimpleNamespace(responses=SimpleNamespace(create=create))
    monkeypatch.setattr(client, "_client", lambda config: api)

    reply = client.generate_reply(
        AgentConfig(model="test-model"),
        {
            "object_id": "tweet-1",
            "author_username": "builder",
            "text": "RWA settlement needs better liquidity primitives.",
            "url": "https://x.com/builder/status/tweet-1",
            "trend": "RWA",
        },
        manual_context="A pasted discussion that should be treated as untrusted context.",
    )

    assert reply.object_id == "tweet-1"
    assert reply.reply_type == "add evidence"
    assert reply.follow_up_content_idea.startswith("Compare settlement")
    assert calls
    assert "reply_planner" in str(calls[0]["input"])
    assert "untrusted context" in str(calls[0]["input"])


def test_model_retries_empty_structured_output_locally(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "runtime.json"
    path.write_text(
        json.dumps(
            {
                "model": {"api_key": "test-only", "allowed_models": ["test-model"]},
                "telegram": {"enabled": False},
            }
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)
    client = AgentModelClient(RuntimeAgentConfig.load(path))
    responses = iter(
        [
            SimpleNamespace(output_text=""),
            SimpleNamespace(output_text='{"text":"Recovered output","rationale":"retry"}'),
        ]
    )
    calls = 0

    def create(**kwargs):  # noqa: ANN003, ANN202
        nonlocal calls
        calls += 1
        return next(responses)

    api = SimpleNamespace(responses=SimpleNamespace(create=create))
    monkeypatch.setattr(client, "_client", lambda config: api)

    post = client.original_post(
        AgentConfig(model="test-model", model_retries=1),
        trend_context=[],
    )

    assert post.text == "Recovered output"
    assert calls == 2


def test_agent_runtime_scan_creates_scored_reply_for_bound_opencli_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_path = tmp_path / "runtime.json"
    runtime_path.write_text(
        json.dumps(
            {
                "model": {
                    "api_key": "test-only",
                    "allowed_models": ["test-model"],
                },
                "telegram": {"enabled": False},
            }
        ),
        encoding="utf-8",
    )
    runtime_path.chmod(0o600)
    monkeypatch.setenv("KOL_AGENT_CONFIG_PATH", str(runtime_path))

    automation = AutomationStore(tmp_path / "runtime.db")
    store = AgentStore(automation)
    agent_id = _agent(store, automation)
    store.set_status(agent_id, "running", actor="tester")
    agent = store.get_agent(agent_id)
    assert agent is not None

    class Registry:
        def dispatch(self, platform_id, task, **kwargs):  # noqa: ANN001
            assert platform_id == "x"
            return PlatformTaskResult.completed(
                [
                    XTweet(
                        external_id="tweet-1",
                        author_external_id="author-1",
                        author_username="builder",
                        text="RWA settlement needs better liquidity primitives.",
                        created_at=datetime.now(timezone.utc).isoformat(),
                        url="https://x.com/builder/status/tweet-1",
                    )
                ],
                cursor="cursor-1",
            )

    class Models:
        def rank_and_reply(self, config, candidates):  # noqa: ANN001
            assert candidates[0]["object_id"] == "tweet-1"
            return [
                CandidateReply(
                    object_id="tweet-1",
                    score=92,
                    safe=True,
                    reply="The liquidity layer matters as much as the asset wrapper.",
                    rationale="Adds a concrete DeFi perspective.",
                )
            ]

        def original_post(self, config, *, trend_context):  # noqa: ANN001
            return OriginalPost(text="A test post", rationale="test")

    runtime = AgentRuntimeService(automation, Registry(), object())  # type: ignore[arg-type]
    runtime.models = Models()  # type: ignore[assignment]
    runtime.scan_agent(agent)

    actions = store.timeline(agent_id)
    assert len(actions) == 1
    assert actions[0]["status"] == "scheduled"
    assert actions[0]["connection_id"] == agent["reply_connection_id"]
    assert actions[0]["payload"]["target_url"].endswith("tweet-1")
    assert store.get_agent(agent_id)["scan_cursor"] == "cursor-1"  # type: ignore[index]

    runtime.create_original_post(agent, command_id=1)
    original = store.timeline(agent_id)[0]
    assert original["action_type"] == "owned_post"
    assert original["payload"]["mode"] == "now"

    store.record_failure(agent_id, "transient model response")
    runtime.create_original_post(agent, command_id=2)
    assert store.get_agent(agent_id)["consecutive_failures"] == 0  # type: ignore[index]

    for index in range(3):
        action_id = automation.create_action(
            "x",
            "owned_post",
            "agent_original_post",
            f"receipt-{index}",
            connection_id=agent["publish_connection_id"],
            draft=f"post {index}",
            initial_status="scheduled",
            idempotency_key=f"receipt-action-{index}",
            now=f"2026-07-23T1{index}:00:00+00:00",
        )
        store.attach_action(action_id, agent_id, agent["current_config_version_id"])
        automation.transition_action(action_id, "executing")
        automation.record_action_result(
            action_id,
            success=True,
            confirmed=False,
            external_id=f"postiz-{index}",
        )
    runtime._reconcile_agents()
    assert store.get_agent(agent_id)["status"] == "running"  # type: ignore[index]
