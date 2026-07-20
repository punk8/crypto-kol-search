from __future__ import annotations

import hashlib
import json
from typing import Any, Callable

from pydantic import BaseModel, Field


class ReplyDraftResult(BaseModel):
    suitable: bool = True
    reason: str = "Relevant to the campaign goal"
    draft: str | None = None
    angle: str | None = None


class TopicDraftResult(BaseModel):
    title: str
    summary: str
    outline: str
    draft: str


class SemanticTopicCluster(BaseModel):
    cluster_key: str
    title: str
    post_ids: list[str] = Field(default_factory=list)


class SemanticTopicClusters(BaseModel):
    clusters: list[SemanticTopicCluster] = Field(default_factory=list)


def _cache_key(kind: str, model: str, payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        {"version": 1, "kind": kind, "model": model, "payload": payload},
        ensure_ascii=False,
        sort_keys=True,
    ).encode()
    return f"signal:{hashlib.sha256(encoded).hexdigest()}"


def _client(api_key: str):  # noqa: ANN202
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError('Signal enhancement requires: pip install -e ".[ai]"') from exc
    return OpenAI(api_key=api_key)


def generate_reply_draft(
    *,
    post: dict[str, Any],
    brand: dict[str, Any],
    kol: dict[str, Any] | None = None,
    recent_context: list[dict[str, Any]] | None = None,
    sender: dict[str, Any] | None = None,
    comment_style: str = "brand",
    campaign_goal: str = "Introduce LaunchVibes when directly relevant",
    conversation_context: list[dict[str, Any]] | None = None,
    language: str,
    api_key: str,
    model: str,
    cache_get: Callable[[str], dict | None] | None = None,
    cache_set: Callable[[str, dict], None] | None = None,
) -> ReplyDraftResult:
    payload = {
        "post": {
            "author": post.get("author_username"),
            "text": post.get("text"),
            "url": post.get("url"),
            "conversation_id": post.get("conversation_id"),
        },
        "conversation_context": (conversation_context or [])[:5],
        "kol": kol or {},
        "recent_kol_context": (recent_context or [])[:5],
        "brand": brand,
        "sender": sender or {},
        "comment_style": comment_style,
        "campaign_goal": campaign_goal,
        "language": language,
    }
    key = _cache_key("reply", model, payload)
    cached = cache_get(key) if cache_get else None
    if cached:
        return ReplyDraftResult.model_validate(cached)

    response = _client(api_key).responses.parse(
        model=model,
        input=[
            {
                "role": "system",
                "content": (
                    "Decide whether the supplied X post is genuinely suitable for a LaunchVibes "
                    "reply. If unrelated to creator growth, content planning, or creator workflow, "
                    "return suitable=false, explain why, and return no draft. Otherwise draft one "
                    "relevant reply grounded in the original post and supplied context. Brand style "
                    "may directly promote the product. Conversational style changes tone only: it "
                    "must never hide affiliation or pretend to be an independent ordinary user. "
                    "Never invent product use, testimonials, results, metrics, partnerships, claims, "
                    "or social proof. Use only permitted claims, avoid forbidden wording, follow the "
                    "requested language, and keep the reply under 260 characters."
                ),
            },
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        text_format=ReplyDraftResult,
    )
    if response.output_parsed is None:
        raise RuntimeError("OpenAI returned no reply draft")
    result = response.output_parsed
    if cache_set:
        cache_set(key, result.model_dump())
    return result


def generate_topic_content(
    *,
    topic: str,
    posts: list[dict[str, Any]],
    brand: dict[str, Any],
    language: str,
    api_key: str,
    model: str,
    cache_get: Callable[[str], dict | None] | None = None,
    cache_set: Callable[[str, dict], None] | None = None,
) -> TopicDraftResult:
    payload = {
        "topic": topic,
        "posts": [
            {
                "author": post.get("author_username"),
                "text": post.get("text"),
                "url": post.get("url"),
            }
            for post in posts[:10]
        ],
        "brand": brand,
        "language": language,
    }
    key = _cache_key("topic", model, payload)
    cached = cache_get(key) if cache_get else None
    if cached:
        return TopicDraftResult.model_validate(cached)

    response = _client(api_key).responses.parse(
        model=model,
        input=[
            {
                "role": "system",
                "content": (
                    "Create a single-X-post editorial proposal from public crypto posts. Follow the "
                    "requested language and brand tone. Separate observed facts from interpretation, "
                    "cite no fact absent from the supplied posts, avoid forbidden terms, and keep the "
                    "draft under 260 characters. Return only the requested structure."
                ),
            },
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        text_format=TopicDraftResult,
    )
    if response.output_parsed is None:
        raise RuntimeError("OpenAI returned no topic draft")
    result = response.output_parsed
    if cache_set:
        cache_set(key, result.model_dump())
    return result


def generate_semantic_topic_clusters(
    *,
    posts: list[dict[str, Any]],
    api_key: str,
    model: str,
    cache_get: Callable[[str], dict | None] | None = None,
    cache_set: Callable[[str, dict], None] | None = None,
) -> SemanticTopicClusters:
    payload = {
        "posts": [
            {"id": str(post.get("id")), "text": post.get("text", "")}
            for post in posts[:80]
        ]
    }
    key = _cache_key("semantic-topic-clusters", model, payload)
    cached = cache_get(key) if cache_get else None
    if cached:
        return SemanticTopicClusters.model_validate(cached)

    response = _client(api_key).responses.parse(
        model=model,
        input=[
            {
                "role": "system",
                "content": (
                    "Group crypto X posts that discuss the same concrete event or narrative. Do not "
                    "group posts only because they share a broad sector such as DeFi. Each cluster_key "
                    "must be a stable lowercase snake_case label. Use only supplied post IDs, assign "
                    "each post to at most one cluster, and omit clusters with fewer than two posts."
                ),
            },
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        text_format=SemanticTopicClusters,
    )
    if response.output_parsed is None:
        raise RuntimeError("OpenAI returned no semantic topic clusters")
    result = response.output_parsed
    if cache_set:
        cache_set(key, result.model_dump())
    return result
