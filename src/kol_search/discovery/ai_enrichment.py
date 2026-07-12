from __future__ import annotations

import hashlib
import json
from typing import Callable

from pydantic import BaseModel, Field

from kol_search.models import Account, AccountEnrichment, Post


class AIClassification(BaseModel):
    account_type: str
    languages: list[str] = Field(default_factory=list)
    topics: list[str] = Field(default_factory=list)
    summary: str
    relevance_score: float = Field(ge=0, le=1)
    confidence: float = Field(ge=0, le=1)


def enrichment_cache_key(account: Account, posts: list[Post], topic: str, model: str) -> str:
    payload = {
        "version": 1,
        "model": model,
        "topic": topic,
        "account": account.model_dump(exclude={"raw"}),
        "posts": [post.text for post in posts[:10]],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def enrich_with_openai(
    account: Account,
    posts: list[Post],
    topic: str,
    *,
    api_key: str,
    model: str,
    cache_get: Callable[[str], dict | None] | None = None,
    cache_set: Callable[[str, dict], None] | None = None,
) -> AccountEnrichment:
    key = enrichment_cache_key(account, posts, topic, model)
    cached = cache_get(key) if cache_get else None
    if cached:
        return AccountEnrichment.model_validate(cached)

    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError('OpenAI enhancement requires: pip install -e ".[ai]"') from exc

    client = OpenAI(api_key=api_key)
    response = client.responses.parse(
        model=model,
        input=[
            {
                "role": "system",
                "content": (
                    "Classify a public crypto X account. Return only the requested structure. "
                    "account_type must be one of person, media, fund, project, company, community, unknown. "
                    "Do not infer or output contact information. Use zh/en language codes."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "search_topic": topic,
                        "username": account.username,
                        "name": account.name,
                        "bio": account.description,
                        "recent_posts": [post.text for post in posts[:10]],
                    },
                    ensure_ascii=False,
                ),
            },
        ],
        text_format=AIClassification,
    )
    parsed = response.output_parsed
    if parsed is None:
        raise RuntimeError("OpenAI returned no structured classification")
    allowed = {"person", "media", "fund", "project", "company", "community", "unknown"}
    account_type = parsed.account_type if parsed.account_type in allowed else "unknown"
    result = AccountEnrichment(
        account_type=account_type,  # type: ignore[arg-type]
        languages=parsed.languages,
        topics=parsed.topics,
        summary=parsed.summary,
        relevance_score=parsed.relevance_score,
        confidence=parsed.confidence,
        source="openai",
    )
    if cache_set:
        cache_set(key, result.model_dump())
    return result

