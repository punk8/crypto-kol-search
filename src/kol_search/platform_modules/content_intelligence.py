from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field


class NativeContentSample(BaseModel):
    """Minimal platform-owned content passed to optional semantic analysis."""

    object_id: str
    text: str
    author_id: str | None = None
    url: str | None = None


class ContentInsight(BaseModel):
    """Advisory AI output; deterministic policy never consumes a final decision."""

    object_id: str
    relevance_score: float = Field(default=0.0, ge=0.0, le=1.0)
    cluster_key: str | None = None
    cluster_title: str | None = None
    reply_draft: str | None = None
    rationale: str | None = None


class ContentInsightBatch(BaseModel):
    insights: list[ContentInsight] = Field(default_factory=list)


@runtime_checkable
class ContentIntelligence(Protocol):
    """Optional classification, clustering, and drafting boundary."""

    def analyze(
        self,
        *,
        platform_id: str,
        contents: Sequence[NativeContentSample],
        brand: Mapping[str, Any],
    ) -> Mapping[str, ContentInsight]: ...


class DisabledContentIntelligence:
    """Offline default used when no model credential is configured."""

    def analyze(
        self,
        *,
        platform_id: str,
        contents: Sequence[NativeContentSample],
        brand: Mapping[str, Any],
    ) -> Mapping[str, ContentInsight]:
        del platform_id, contents, brand
        return {}


class OpenAIContentIntelligence:
    """One structured request for platform-local semantic assistance."""

    def __init__(self, *, api_key: str, model: str) -> None:
        self.api_key = api_key
        self.model = model

    def analyze(
        self,
        *,
        platform_id: str,
        contents: Sequence[NativeContentSample],
        brand: Mapping[str, Any],
    ) -> Mapping[str, ContentInsight]:
        if not contents:
            return {}
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise RuntimeError(
                'AI content intelligence requires: pip install -e ".[ai]"'
            ) from exc

        allowed_ids = {content.object_id for content in contents}
        payload = {
            "platform_id": platform_id,
            "domain": "crypto, blockchain, web3, DeFi, stablecoins, and RWA",
            "brand": {
                "name": str(brand.get("brand_name") or ""),
                "description": str(brand.get("description") or ""),
                "audience": str(brand.get("audience") or ""),
                "tone": str(brand.get("tone") or ""),
                "allowed_claims": list(brand.get("allowed_claims") or ()),
                "forbidden_terms": list(brand.get("forbidden_terms") or ()),
            },
            "contents": [
                {
                    "object_id": content.object_id,
                    "author_id": content.author_id,
                    "text": content.text[:4_000],
                    "url": content.url,
                }
                for content in contents[:80]
            ],
        }
        response = OpenAI(api_key=self.api_key).responses.parse(
            model=self.model,
            input=[
                {
                    "role": "system",
                    "content": (
                        "Analyze public content for one social platform. Return one insight "
                        "per supplied object_id. Score topical relevance from 0 to 1. Group "
                        "content only when it describes the same concrete event or narrative; "
                        "use a stable lowercase snake_case cluster_key, otherwise null. Draft "
                        "a concise, genuine platform-native reply in the source language. Ground "
                        "every claim in the supplied text or allowed_claims, obey the brand tone, "
                        "and avoid forbidden_terms. Never decide promotion, safety, or whether an "
                        "action should execute."
                    ),
                },
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            text_format=ContentInsightBatch,
        )
        parsed = response.output_parsed
        if parsed is None:
            raise RuntimeError("OpenAI returned no structured content insights")
        output: dict[str, ContentInsight] = {}
        for insight in parsed.insights:
            if insight.object_id not in allowed_ids or insight.object_id in output:
                continue
            output[insight.object_id] = insight
        return output


def content_intelligence_from_settings(settings: object) -> ContentIntelligence:
    api_key = str(getattr(settings, "openai_api_key", "") or "").strip()
    if not api_key:
        return DisabledContentIntelligence()
    model = str(getattr(settings, "openai_model", "gpt-5.6-luna") or "").strip()
    return OpenAIContentIntelligence(api_key=api_key, model=model)


__all__ = [
    "ContentInsight",
    "ContentInsightBatch",
    "ContentIntelligence",
    "DisabledContentIntelligence",
    "NativeContentSample",
    "OpenAIContentIntelligence",
    "content_intelligence_from_settings",
]
