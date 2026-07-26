from __future__ import annotations

from collections import defaultdict

from kol_search.backend.models import (
    DisplayContentItem,
    ProcessContentRequest,
    ProcessContentResponse,
    RawContentRecord,
)


def process_content(payload: ProcessContentRequest) -> ProcessContentResponse:
    """Project provider records into a stable, deduplicated frontend contract."""

    snapshots: dict[tuple[str, str], list[RawContentRecord]] = defaultdict(list)
    for record in payload.records:
        snapshots[(record.platform, record.native_id)].append(record)

    items: list[DisplayContentItem] = []
    for records in snapshots.values():
        latest = max(records, key=lambda item: item.retrieved_at)
        lag = max(0, int((latest.retrieved_at - latest.published_at).total_seconds()))
        items.append(
            DisplayContentItem(
                **latest.model_dump(),
                metric_snapshot_count=len(records),
                retrieval_lag_seconds=lag,
            )
        )

    items.sort(key=lambda item: (item.published_at, item.retrieved_at), reverse=True)
    limited = items[: payload.limit]
    return ProcessContentResponse(
        items=limited,
        input_count=len(payload.records),
        unique_count=len(items),
        duplicate_snapshots=len(payload.records) - len(items),
    )
