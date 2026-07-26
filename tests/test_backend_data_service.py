from __future__ import annotations

from fastapi.testclient import TestClient

from kol_search.backend.models import ProcessContentRequest
from kol_search.backend.processing import process_content
from kol_search.web import app


def test_source_catalog_explains_supported_and_rejected_x_paths(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("KOL_DB_PATH", str(tmp_path / "backend-service.db"))
    monkeypatch.setenv("KOL_ENABLE_SIGNAL_SCAN", "false")
    with TestClient(app) as client:
        response = client.get("/api/backend/sources?platform=x")
        assert response.status_code == 200
        sources = {item["id"]: item for item in response.json()["items"]}
        assert sources["x_official"]["status"] == "primary"
        assert sources["twitterapi_io"]["status"] == "secondary"
        assert sources["getxapi"]["status"] == "secondary"
        assert sources["twscrape"]["status"] == "conditional"
        assert sources["fxembed"]["status"] == "conditional"
        assert sources["getdaytrends"]["status"] == "not_for_ingestion"
        assert sources["nitter"]["status"] == "not_for_ingestion"

        supported = client.get(
            "/api/backend/sources?platform=x&include_rejected=false"
        ).json()["items"]
        assert all(item["status"] != "not_for_ingestion" for item in supported)


def test_backend_health_is_request_scoped(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("KOL_DB_PATH", str(tmp_path / "backend-health.db"))
    monkeypatch.setenv("KOL_ENABLE_SIGNAL_SCAN", "false")
    with TestClient(app) as client:
        response = client.get("/api/backend/health")
        assert response.status_code == 200
        assert response.json() == {
            "service": "discover-data-backend",
            "status": "ok",
            "execution": "request-scoped",
            "version": "0.1.0",
        }


def test_content_processing_keeps_latest_snapshot_and_native_identity() -> None:
    result = process_content(
        ProcessContentRequest.model_validate(
            {
                "records": [
                    {
                        "platform": "x",
                        "provider": "official",
                        "native_id": "post-1",
                        "author_native_id": "user-1",
                        "text": "first observation",
                        "published_at": "2026-07-26T08:00:00Z",
                        "retrieved_at": "2026-07-26T08:02:00Z",
                        "metrics": {"likes": 10},
                    },
                    {
                        "platform": "x",
                        "provider": "official",
                        "native_id": "post-1",
                        "author_native_id": "user-1",
                        "text": "first observation",
                        "published_at": "2026-07-26T08:00:00Z",
                        "retrieved_at": "2026-07-26T08:05:00Z",
                        "metrics": {"likes": 25},
                    },
                    {
                        "platform": "youtube",
                        "provider": "youtube_data_api",
                        "native_id": "video-2",
                        "text": "newer video",
                        "published_at": "2026-07-26T09:00:00Z",
                        "retrieved_at": "2026-07-26T09:01:00Z",
                        "metrics": {"views": 100},
                    },
                ]
            }
        )
    )

    assert result.input_count == 3
    assert result.unique_count == 2
    assert result.duplicate_snapshots == 1
    assert result.items[0].native_id == "video-2"
    x_item = next(item for item in result.items if item.native_id == "post-1")
    assert x_item.metrics == {"likes": 25}
    assert x_item.metric_snapshot_count == 2
    assert x_item.retrieval_lag_seconds == 300
