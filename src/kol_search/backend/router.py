from __future__ import annotations

from fastapi import APIRouter, Query

from kol_search.backend.catalog import SOURCES
from kol_search.backend.models import (
    AcquisitionPurpose,
    PlatformId,
    ProcessContentRequest,
    ProcessContentResponse,
    SourceCatalogResponse,
)
from kol_search.backend.processing import process_content


router = APIRouter(prefix="/api/backend", tags=["data-backend"])


@router.get("/health")
def backend_health() -> dict[str, str]:
    return {
        "service": "discover-data-backend",
        "status": "ok",
        "execution": "request-scoped",
        "version": "0.1.0",
    }


@router.get("/sources", response_model=SourceCatalogResponse)
def list_sources(
    platform: PlatformId | None = None,
    purpose: AcquisitionPurpose | None = None,
    include_rejected: bool = Query(default=True),
) -> SourceCatalogResponse:
    items = [
        item
        for item in SOURCES
        if (platform is None or item.platform == platform)
        and (purpose is None or purpose in item.purposes)
        and (include_rejected or item.status != "not_for_ingestion")
    ]
    return SourceCatalogResponse(items=items, total=len(items))


@router.post("/process/content", response_model=ProcessContentResponse)
def process_content_records(payload: ProcessContentRequest) -> ProcessContentResponse:
    return process_content(payload)
