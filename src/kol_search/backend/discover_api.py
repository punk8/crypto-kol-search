from __future__ import annotations

import hmac
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from kol_search.backend.connectors.base import ConnectorError, ConnectorNotFoundError
from kol_search.backend.connectors.registry import ConnectorRegistry
from kol_search.backend.connectors.x import XConnector
from kol_search.backend.discover_automation import (
    DiscoverAutomationService,
    parse_discovery_domains,
)
from kol_search.backend.models import (
    AccountContentQuery,
    AccountListResponse,
    AccountSearchQuery,
    AccountTrackingRequest,
    AccountTrackingResponse,
    ConnectorListResponse,
    ConnectorResponseMeta,
    DiscoveryDomainListResponse,
    HotContentListResponse,
    ContentListResponse,
    ContentLookupQuery,
    ContentSearchQuery,
    ProviderUsageItem,
    ProviderUsageResponse,
    ReplyDraftRequest,
    ReplyDraftResponse,
    TrendListResponse,
    TrendQuery,
    TrackedAccountListResponse,
)
from kol_search.backend.reply_service import DeepSeekReplyService, ReplyDraftError
from kol_search.settings import Settings


router = APIRouter(prefix="/api/discover/v1", tags=["discover-api"])


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _registry(request: Request) -> ConnectorRegistry:
    return request.app.state.connector_registry


def require_backend_api_access(request: Request) -> None:
    """Authenticate split-deployment calls while keeping loopback development easy."""

    settings = _settings(request)
    expected = settings.backend_api_token
    loopback_hosts = {"127.0.0.1", "localhost", "::1"}
    local_only = (
        settings.web_host in loopback_hosts and settings.backend_host in loopback_hosts
    )
    if not expected:
        if local_only:
            return
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Backend API authentication is not configured.",
        )

    authorization = request.headers.get("Authorization", "")
    scheme, _, credential = authorization.partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(credential, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid backend service credential.",
            headers={"WWW-Authenticate": "Bearer"},
        )


def _connector(request: Request, platform_id: str):  # noqa: ANN202
    try:
        return _registry(request).get(platform_id)
    except ConnectorNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _x_connector(request: Request, platform_id: str) -> XConnector:
    connector = _connector(request, platform_id)
    if platform_id != "x" or not isinstance(connector, XConnector):
        raise HTTPException(
            status_code=404, detail="This capability is only available for X."
        )
    return connector


def _meta(platform_id: str, result) -> ConnectorResponseMeta:  # noqa: ANN001
    return ConnectorResponseMeta(
        platform=platform_id,
        provider=result.provider,
        attempted_providers=result.attempted_providers,
        observed_at=result.observed_at,
        warnings=result.warnings,
    )


def _service_unavailable(exc: ConnectorError) -> HTTPException:
    return HTTPException(status_code=503, detail=str(exc))


@router.get(
    "/platforms",
    response_model=ConnectorListResponse,
    dependencies=[Depends(require_backend_api_access)],
)
def list_platform_connectors(request: Request) -> ConnectorListResponse:
    items = _registry(request).descriptors()
    return ConnectorListResponse(items=items, total=len(items))


@router.get(
    "/domains",
    response_model=DiscoveryDomainListResponse,
    dependencies=[Depends(require_backend_api_access)],
)
def list_discovery_domains(request: Request) -> DiscoveryDomainListResponse:
    items = list(parse_discovery_domains(_settings(request)))
    return DiscoveryDomainListResponse(items=items, total=len(items))


@router.get(
    "/platforms/{platform_id}/hot-content",
    response_model=HotContentListResponse,
    dependencies=[Depends(require_backend_api_access)],
)
def list_platform_hot_content(
    request: Request,
    platform_id: str,
    source: str = Query(default="discover", pattern="^(discover|following)$"),
    domain: str | None = Query(default=None, max_length=40),
    limit: int = Query(default=30, ge=1, le=100),
) -> HotContentListResponse:
    connector = _x_connector(request, platform_id)
    service = DiscoverAutomationService(_settings(request), connector)
    try:
        result = service.hot_content(source=source, domain_key=domain, limit=limit)
    finally:
        service.close()
    return HotContentListResponse(
        meta=_meta(platform_id, result),
        items=list(result.items),
        total=len(result.items),
    )


@router.post(
    "/platforms/{platform_id}/content/{native_id}/reply-draft",
    response_model=ReplyDraftResponse,
    dependencies=[Depends(require_backend_api_access)],
)
def generate_platform_reply_draft(
    request: Request,
    platform_id: str,
    native_id: str,
    payload: ReplyDraftRequest,
) -> ReplyDraftResponse:
    connector = _x_connector(request, platform_id)
    service = DeepSeekReplyService(_settings(request), connector)
    try:
        return service.generate(native_id, payload)
    except ReplyDraftError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    finally:
        service.close()


@router.get(
    "/platforms/{platform_id}/providers/usage",
    response_model=ProviderUsageResponse,
    dependencies=[Depends(require_backend_api_access)],
)
def get_platform_provider_usage(
    request: Request,
    platform_id: str,
    refresh: bool = Query(default=False),
) -> ProviderUsageResponse:
    connector = _connector(request, platform_id)
    provider_usage = getattr(connector, "provider_usage", None)
    if not callable(provider_usage):
        raise HTTPException(
            status_code=404, detail="Provider usage monitoring is unavailable."
        )
    items = [ProviderUsageItem.model_validate(item) for item in provider_usage(refresh=refresh)]
    return ProviderUsageResponse(
        platform=platform_id,
        items=items,
        total=len(items),
    )


@router.get(
    "/platforms/{platform_id}/accounts/tracking",
    response_model=TrackedAccountListResponse,
    dependencies=[Depends(require_backend_api_access)],
)
def list_tracked_platform_accounts(
    request: Request,
    platform_id: str,
    status_filter: str = Query(default="active", alias="status", pattern="^(seed|active|paused)$"),
    limit: int = Query(default=100, ge=1, le=500),
) -> TrackedAccountListResponse:
    connector = _connector(request, platform_id)
    try:
        result = connector.list_tracked_accounts(status=status_filter, limit=limit)
    except ConnectorError as exc:
        raise _service_unavailable(exc) from exc
    return TrackedAccountListResponse(
        meta=_meta(platform_id, result),
        items=list(result.items),
        total=len(result.items),
    )


@router.get(
    "/platforms/{platform_id}/trends",
    response_model=TrendListResponse,
    dependencies=[Depends(require_backend_api_access)],
)
def get_platform_trends(
    request: Request,
    platform_id: str,
    category: str | None = Query(default=None, max_length=80),
    locale: str | None = Query(default=None, max_length=80),
    limit: int = Query(default=20, ge=1, le=50),
) -> TrendListResponse:
    connector = _connector(request, platform_id)
    try:
        result = connector.get_trends(
            TrendQuery(category=category, locale=locale, limit=limit)
        )
    except ConnectorError as exc:
        raise _service_unavailable(exc) from exc
    return TrendListResponse(
        meta=_meta(platform_id, result),
        items=list(result.items),
        total=len(result.items),
    )


@router.get(
    "/platforms/{platform_id}/content/search",
    response_model=ContentListResponse,
    dependencies=[Depends(require_backend_api_access)],
)
def search_platform_content(
    request: Request,
    platform_id: str,
    q: str = Query(min_length=1, max_length=500),
    sort: str = Query(default="latest", pattern="^(latest|top)$"),
    limit: int = Query(default=20, ge=1, le=100),
    since_id: str | None = Query(default=None, max_length=300),
    start_time: datetime | None = None,
) -> ContentListResponse:
    connector = _connector(request, platform_id)
    try:
        result = connector.search_content(
            ContentSearchQuery(
                query=q,
                sort=sort,
                limit=limit,
                since_id=since_id,
                start_time=start_time,
            )
        )
    except ConnectorError as exc:
        raise _service_unavailable(exc) from exc
    return ContentListResponse(
        meta=_meta(platform_id, result),
        items=list(result.items),
        total=len(result.items),
    )


@router.get(
    "/platforms/{platform_id}/accounts/search",
    response_model=AccountListResponse,
    dependencies=[Depends(require_backend_api_access)],
)
def search_platform_accounts(
    request: Request,
    platform_id: str,
    q: str = Query(min_length=1, max_length=120),
    language: str | None = Query(default=None, max_length=20),
    limit: int = Query(default=12, ge=1, le=50),
    sample_size: int = Query(default=50, ge=5, le=100),
    recent_posts_per_account: int = Query(default=5, ge=1, le=10),
    min_engagement: int = Query(default=25, ge=0, le=100_000),
) -> AccountListResponse:
    connector = _connector(request, platform_id)
    try:
        result = connector.search_accounts(
            AccountSearchQuery(
                query=q,
                language=language,
                limit=limit,
                sample_size=sample_size,
                recent_posts_per_account=recent_posts_per_account,
                min_engagement=min_engagement,
            )
        )
    except ConnectorError as exc:
        raise _service_unavailable(exc) from exc
    return AccountListResponse(
        meta=_meta(platform_id, result),
        items=list(result.items),
        total=len(result.items),
    )


@router.post(
    "/platforms/{platform_id}/accounts/{native_id}/tracking",
    response_model=AccountTrackingResponse,
    dependencies=[Depends(require_backend_api_access)],
)
def set_platform_account_tracking(
    request: Request,
    platform_id: str,
    native_id: str,
    payload: AccountTrackingRequest,
) -> AccountTrackingResponse:
    connector = _connector(request, platform_id)
    try:
        result = connector.set_account_tracking(native_id, enabled=payload.enabled)
    except ConnectorError as exc:
        raise _service_unavailable(exc) from exc
    return AccountTrackingResponse(
        meta=_meta(platform_id, result),
        item=result.items[0],
    )


@router.get(
    "/platforms/{platform_id}/accounts/{account_ref}/content",
    response_model=ContentListResponse,
    dependencies=[Depends(require_backend_api_access)],
)
def get_platform_account_content(
    request: Request,
    platform_id: str,
    account_ref: str,
    limit: int = Query(default=20, ge=1, le=100),
    include_replies: bool = False,
    start_time: datetime | None = None,
) -> ContentListResponse:
    connector = _connector(request, platform_id)
    try:
        result = connector.get_account_content(
            AccountContentQuery(
                account_ref=account_ref,
                limit=limit,
                include_replies=include_replies,
                start_time=start_time,
            )
        )
    except ConnectorError as exc:
        raise _service_unavailable(exc) from exc
    return ContentListResponse(
        meta=_meta(platform_id, result),
        items=list(result.items),
        total=len(result.items),
    )


@router.get(
    "/platforms/{platform_id}/content/{native_id}",
    response_model=ContentListResponse,
    dependencies=[Depends(require_backend_api_access)],
)
def get_platform_content(
    request: Request,
    platform_id: str,
    native_id: str,
) -> ContentListResponse:
    connector = _connector(request, platform_id)
    try:
        result = connector.get_content(ContentLookupQuery(native_id=native_id))
    except ConnectorError as exc:
        raise _service_unavailable(exc) from exc
    return ContentListResponse(
        meta=_meta(platform_id, result),
        items=list(result.items),
        total=len(result.items),
    )
