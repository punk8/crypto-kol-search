from __future__ import annotations

import hashlib
import math
from collections.abc import Iterable, Sequence
from datetime import datetime, timezone
from pathlib import Path

from kol_search.platforms.kernel import PlatformTaskContext, PlatformTaskResult


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def log_ratio(value: int, reference: int) -> float:
    return min(1.0, math.log1p(max(0, value)) / math.log1p(reference))


def keyword_relevance_score(
    text: str,
    terms: Sequence[str],
    *,
    negated_phrases: Sequence[str] = (),
) -> float:
    """Return a conservative offline relevance score for platform content."""

    lowered = text.casefold()
    matches = sum(1 for term in terms if term.casefold() in lowered)
    if matches == 1 and any(
        phrase.casefold() in lowered for phrase in negated_phrases
    ):
        return 0.0
    return min(1.0, 0.55 + 0.15 * (matches - 1)) if matches else 0.0


def stable_native_id(*values: object, length: int = 24) -> str:
    material = "\x1f".join(str(value) for value in values)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:length]


def resolve_database_path(settings: object) -> Path | None:
    db_path = getattr(settings, "db_path", None)
    if callable(db_path):
        return Path(db_path())
    return None


def build_opportunity_task(
    adapter: object, context: PlatformTaskContext
) -> PlatformTaskResult:
    """Adapt a platform-owned projection into the registered task envelope."""

    items = context.payload.get("items") or ()
    if not isinstance(items, Iterable):
        raise TypeError("opportunity build items must be iterable")
    projection = adapter.project_signals(items, payload=context.payload)  # type: ignore[attr-defined]
    return PlatformTaskResult.completed(
        projection.opportunities,
        warnings=projection.warnings,
        metadata={
            **dict(projection.metadata),
            "native_counts": dict(projection.native_counts),
        },
    )
