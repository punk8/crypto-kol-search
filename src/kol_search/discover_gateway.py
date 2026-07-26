from __future__ import annotations

from typing import TypeVar
from urllib.parse import quote

import httpx
from pydantic import BaseModel

from kol_search.backend.connectors.base import ConnectorError, ConnectorResult
from kol_search.backend.connectors.registry import ConnectorRegistry
from kol_search.backend.connectors.x import XConnector
from kol_search.backend.discover_automation import DiscoverAutomationService
from kol_search.backend.models import (
    AccountContentQuery,
    AccountListResponse,
    AccountSearchQuery,
    AccountTrackingRequest,
    AccountTrackingResponse,
    ConnectorResponseMeta,
    ContentItem,
    ContentListResponse,
    ContentLookupQuery,
    ContentSearchQuery,
    HotContentListResponse,
    ReplyDraftRequest,
    ReplyDraftResponse,
    TrendListResponse,
    TrendQuery,
    TrackedAccountListResponse,
    ProviderUsageResponse,
)
from kol_search.backend.reply_service import DeepSeekReplyService, ReplyDraftError
from kol_search.settings import Settings


class DiscoverGatewayError(RuntimeError):
    """The frontend/BFF could not obtain normalized Discover data."""


ResponseT = TypeVar("ResponseT", bound=BaseModel)
def _meta(platform_id: str, result: ConnectorResult) -> ConnectorResponseMeta:
    return ConnectorResponseMeta(
        platform=platform_id,
        provider=result.provider,
        attempted_providers=result.attempted_providers,
        observed_at=result.observed_at,
        warnings=result.warnings,
    )


class DiscoverGateway:
    """BFF gateway that uses remote HTTP in split deployments and local connectors in dev."""

    def __init__(self, settings: Settings, registry: ConnectorRegistry) -> None:
        self.settings = settings
        self.registry = registry
        self._client: httpx.Client | None = None
        if settings.backend_url:
            headers = {"Accept": "application/json"}
            if settings.backend_api_token:
                headers["Authorization"] = f"Bearer {settings.backend_api_token}"
            self._client = httpx.Client(
                base_url=settings.backend_url.rstrip("/"),
                headers=headers,
                timeout=settings.backend_request_timeout_seconds,
                follow_redirects=False,
            )

    @property
    def mode(self) -> str:
        return "remote" if self._client is not None else "local"

    def _get(
        self,
        path: str,
        *,
        params: dict[str, object | None],
        response_model: type[ResponseT],
    ) -> ResponseT:
        if self._client is None:
            raise RuntimeError("Remote client is not configured")
        clean_params = {key: value for key, value in params.items() if value is not None}
        try:
            response = self._client.get(path, params=clean_params)
        except httpx.HTTPError as exc:
            raise DiscoverGatewayError("Discover backend network request failed.") from exc
        if response.status_code >= 400:
            raise DiscoverGatewayError(
                f"Discover backend returned HTTP {response.status_code}."
            )
        try:
            return response_model.model_validate(response.json())
        except (ValueError, TypeError) as exc:
            raise DiscoverGatewayError(
                "Discover backend returned an incompatible response."
            ) from exc

    def _post(
        self,
        path: str,
        *,
        payload: BaseModel,
        response_model: type[ResponseT],
    ) -> ResponseT:
        if self._client is None:
            raise RuntimeError("Remote client is not configured")
        try:
            response = self._client.post(path, json=payload.model_dump(mode="json"))
        except httpx.HTTPError as exc:
            raise DiscoverGatewayError("Discover backend network request failed.") from exc
        if response.status_code >= 400:
            raise DiscoverGatewayError(
                f"Discover backend returned HTTP {response.status_code}."
            )
        try:
            return response_model.model_validate(response.json())
        except (ValueError, TypeError) as exc:
            raise DiscoverGatewayError(
                "Discover backend returned an incompatible response."
            ) from exc

    def trends(self, platform_id: str, query: TrendQuery) -> TrendListResponse:
        if self._client is not None:
            return self._get(
                f"/api/discover/v1/platforms/{platform_id}/trends",
                params=query.model_dump(mode="json", exclude_none=True),
                response_model=TrendListResponse,
            )
        try:
            result = self.registry.get(platform_id).get_trends(query)
        except ConnectorError as exc:
            raise DiscoverGatewayError(str(exc)) from exc
        return TrendListResponse(
            meta=_meta(platform_id, result),
            items=list(result.items),
            total=len(result.items),
        )

    def provider_usage(
        self, platform_id: str, *, refresh: bool = False
    ) -> ProviderUsageResponse:
        if self._client is not None:
            return self._get(
                f"/api/discover/v1/platforms/{quote(platform_id, safe='')}/providers/usage",
                params={"refresh": refresh},
                response_model=ProviderUsageResponse,
            )
        connector = self.registry.get(platform_id)
        provider_usage = getattr(connector, "provider_usage", None)
        values = provider_usage(refresh=refresh) if callable(provider_usage) else []
        return ProviderUsageResponse(
            platform=platform_id,
            items=values,
            total=len(values),
        )

    def hot_content(
        self,
        platform_id: str,
        *,
        source: str,
        domain: str | None = None,
        limit: int = 30,
    ) -> HotContentListResponse:
        if self._client is not None:
            return self._get(
                f"/api/discover/v1/platforms/{quote(platform_id, safe='')}/hot-content",
                params={"source": source, "domain": domain, "limit": limit},
                response_model=HotContentListResponse,
            )
        connector = self.registry.get(platform_id)
        if platform_id != "x" or not isinstance(connector, XConnector):
            raise DiscoverGatewayError("Hot Content ranking is only available for X.")
        service = DiscoverAutomationService(self.settings, connector)
        try:
            result = service.hot_content(source=source, domain_key=domain, limit=limit)
        finally:
            service.close()
        return HotContentListResponse(
            meta=_meta(platform_id, result),
            items=list(result.items),
            total=len(result.items),
        )

    def reply_draft(
        self,
        platform_id: str,
        content_id: str,
        request: ReplyDraftRequest,
    ) -> ReplyDraftResponse:
        if self._client is not None:
            return self._post(
                "/api/discover/v1/platforms/"
                f"{quote(platform_id, safe='')}/content/"
                f"{quote(content_id, safe='')}/reply-draft",
                payload=request,
                response_model=ReplyDraftResponse,
            )
        connector = self.registry.get(platform_id)
        if platform_id != "x" or not isinstance(connector, XConnector):
            raise DiscoverGatewayError("Reply drafting is only available for X.")
        service = DeepSeekReplyService(self.settings, connector)
        try:
            return service.generate(content_id, request)
        except ReplyDraftError as exc:
            raise DiscoverGatewayError(str(exc)) from exc
        finally:
            service.close()

    def search_content(
        self, platform_id: str, query: ContentSearchQuery
    ) -> ContentListResponse:
        if self._client is not None:
            params = query.model_dump(mode="json", exclude_none=True)
            params["q"] = params.pop("query")
            return self._get(
                f"/api/discover/v1/platforms/{platform_id}/content/search",
                params=params,
                response_model=ContentListResponse,
            )
        try:
            result = self.registry.get(platform_id).search_content(query)
        except ConnectorError as exc:
            raise DiscoverGatewayError(str(exc)) from exc
        return ContentListResponse(
            meta=_meta(platform_id, result),
            items=list(result.items),
            total=len(result.items),
        )

    def search_accounts(
        self, platform_id: str, query: AccountSearchQuery
    ) -> AccountListResponse:
        if self._client is not None:
            params = query.model_dump(mode="json", exclude_none=True)
            params["q"] = params.pop("query")
            return self._get(
                "/api/discover/v1/platforms/"
                f"{quote(platform_id, safe='')}/accounts/search",
                params=params,
                response_model=AccountListResponse,
            )
        try:
            result = self.registry.get(platform_id).search_accounts(query)
        except ConnectorError as exc:
            raise DiscoverGatewayError(str(exc)) from exc
        return AccountListResponse(
            meta=_meta(platform_id, result),
            items=list(result.items),
            total=len(result.items),
        )

    def account_content(
        self, platform_id: str, query: AccountContentQuery
    ) -> ContentListResponse:
        if self._client is not None:
            params = query.model_dump(mode="json", exclude_none=True)
            account_ref = str(params.pop("account_ref"))
            return self._get(
                "/api/discover/v1/platforms/"
                f"{quote(platform_id, safe='')}/accounts/"
                f"{quote(account_ref, safe='')}/content",
                params=params,
                response_model=ContentListResponse,
            )
        try:
            result = self.registry.get(platform_id).get_account_content(query)
        except ConnectorError as exc:
            raise DiscoverGatewayError(str(exc)) from exc
        return ContentListResponse(
            meta=_meta(platform_id, result),
            items=list(result.items),
            total=len(result.items),
        )

    def set_account_tracking(
        self, platform_id: str, native_id: str, *, enabled: bool
    ) -> AccountTrackingResponse:
        if self._client is not None:
            return self._post(
                "/api/discover/v1/platforms/"
                f"{quote(platform_id, safe='')}/accounts/"
                f"{quote(native_id, safe='')}/tracking",
                payload=AccountTrackingRequest(enabled=enabled),
                response_model=AccountTrackingResponse,
            )
        try:
            result = self.registry.get(platform_id).set_account_tracking(
                native_id, enabled=enabled
            )
        except ConnectorError as exc:
            raise DiscoverGatewayError(str(exc)) from exc
        return AccountTrackingResponse(
            meta=_meta(platform_id, result),
            item=result.items[0],
        )

    def tracked_accounts(
        self, platform_id: str, *, status: str = "active", limit: int = 100
    ) -> TrackedAccountListResponse:
        if self._client is not None:
            return self._get(
                "/api/discover/v1/platforms/"
                f"{quote(platform_id, safe='')}/accounts/tracking",
                params={"status": status, "limit": limit},
                response_model=TrackedAccountListResponse,
            )
        try:
            result = self.registry.get(platform_id).list_tracked_accounts(
                status=status, limit=limit
            )
        except ConnectorError as exc:
            raise DiscoverGatewayError(str(exc)) from exc
        return TrackedAccountListResponse(
            meta=_meta(platform_id, result),
            items=list(result.items),
            total=len(result.items),
        )

    def content(self, platform_id: str, native_id: str) -> ContentListResponse:
        if self._client is not None:
            return self._get(
                "/api/discover/v1/platforms/"
                f"{quote(platform_id, safe='')}/content/{quote(native_id, safe='')}",
                params={},
                response_model=ContentListResponse,
            )
        try:
            result = self.registry.get(platform_id).get_content(
                ContentLookupQuery(native_id=native_id)
            )
        except ConnectorError as exc:
            raise DiscoverGatewayError(str(exc)) from exc
        return ContentListResponse(
            meta=_meta(platform_id, result),
            items=list(result.items),
            total=len(result.items),
        )

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None


__all__ = ["DiscoverGateway", "DiscoverGatewayError"]
