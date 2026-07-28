from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field, field_validator


PlatformId = Literal["x", "youtube", "reddit", "instagram", "web"]
AcquisitionPurpose = Literal[
    "trend_discovery",
    "content_discovery",
    "kol_discovery",
    "kol_tracking",
]
SourceStatus = Literal["primary", "secondary", "conditional", "not_for_ingestion"]
ConnectorCapability = Literal[
    "native_trends",
    "content_search",
    "account_search",
    "account_timeline",
    "content_lookup",
    "public_metrics",
]


class AcquisitionSource(BaseModel):
    id: str
    platform: PlatformId
    name: str
    kind: Literal["official_api", "data_provider", "web_search", "feed", "website"]
    status: SourceStatus
    purposes: tuple[AcquisitionPurpose, ...]
    native_semantics: bool
    server_http: bool
    supports_incremental: bool
    storage_policy: str
    constraints: tuple[str, ...] = ()
    recommendation: str


class SourceCatalogResponse(BaseModel):
    items: list[AcquisitionSource]
    total: int


class RawContentRecord(BaseModel):
    platform: PlatformId
    provider: str = Field(min_length=1, max_length=80)
    native_id: str = Field(min_length=1, max_length=300)
    author_native_id: str | None = Field(default=None, max_length=300)
    author_name: str | None = Field(default=None, max_length=300)
    title: str | None = Field(default=None, max_length=1000)
    text: str = Field(default="", max_length=20_000)
    canonical_url: str | None = Field(default=None, max_length=2000)
    published_at: datetime
    retrieved_at: datetime
    metrics: dict[str, int | float] = Field(default_factory=dict)

    @field_validator("published_at", "retrieved_at")
    @classmethod
    def normalize_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value


class ProcessContentRequest(BaseModel):
    records: list[RawContentRecord] = Field(max_length=500)
    limit: int = Field(default=100, ge=1, le=500)


class DisplayContentItem(BaseModel):
    platform: PlatformId
    provider: str
    native_id: str
    author_native_id: str | None
    author_name: str | None
    title: str | None
    text: str
    canonical_url: str | None
    published_at: datetime
    retrieved_at: datetime
    metrics: dict[str, int | float]
    metric_snapshot_count: int
    retrieval_lag_seconds: int


class ProcessContentResponse(BaseModel):
    items: list[DisplayContentItem]
    input_count: int
    unique_count: int
    duplicate_snapshots: int


class ProviderDescriptor(BaseModel):
    id: str
    ready: bool
    configured: bool
    role: Literal["primary", "fallback"]
    detail: str | None = None


class ProviderUsageItem(BaseModel):
    provider: str
    state: Literal["healthy", "low_balance", "budget_exhausted", "unavailable"]
    requests_today: int = Field(ge=0)
    daily_limit: int = Field(ge=1)
    credits_remaining: float | None = Field(default=None, ge=0)
    credits_used: float | None = Field(default=None, ge=0)
    upstream_total_requests: int | None = Field(default=None, ge=0)
    observed_at: datetime | None = None


class ProviderUsageResponse(BaseModel):
    platform: PlatformId
    items: list[ProviderUsageItem]
    total: int


class PlatformConnectorDescriptor(BaseModel):
    platform: PlatformId
    name: str
    capabilities: tuple[ConnectorCapability, ...]
    providers: list[ProviderDescriptor]


class ConnectorListResponse(BaseModel):
    items: list[PlatformConnectorDescriptor]
    total: int


class TrendQuery(BaseModel):
    category: str | None = Field(default=None, max_length=80)
    locale: str | None = Field(default=None, max_length=80)
    limit: int = Field(default=20, ge=1, le=50)


class ContentSearchQuery(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    sort: Literal["latest", "top"] = "latest"
    limit: int = Field(default=20, ge=1, le=100)
    since_id: str | None = Field(default=None, max_length=300)
    start_time: datetime | None = None


class AccountContentQuery(BaseModel):
    account_ref: str = Field(min_length=1, max_length=300)
    limit: int = Field(default=20, ge=1, le=100)
    include_replies: bool = False
    start_time: datetime | None = None


class AccountSearchQuery(BaseModel):
    query: str = Field(min_length=1, max_length=120)
    language: str | None = Field(default=None, max_length=20)
    limit: int = Field(default=12, ge=1, le=50)
    sample_size: int = Field(default=50, ge=5, le=100)
    recent_posts_per_account: int = Field(default=5, ge=1, le=10)
    min_engagement: int = Field(default=25, ge=0, le=100_000)


class AccountTrackingRequest(BaseModel):
    enabled: bool = True


class AccountTrackingItem(BaseModel):
    platform: PlatformId
    native_id: str
    status: Literal["active", "paused"]
    updated_at: datetime


class TrackedAccountItem(BaseModel):
    platform: PlatformId
    native_id: str
    username: str
    display_name: str | None = None
    profile_url: str | None = None
    status: Literal["seed", "active", "paused"]
    score: float = Field(default=0, ge=0, le=1)


class ContentLookupQuery(BaseModel):
    native_id: str = Field(min_length=1, max_length=300)


class AccountSummary(BaseModel):
    native_id: str
    username: str | None = None
    display_name: str | None = None
    profile_url: str | None = None


class AccountScoreComponent(BaseModel):
    key: Literal[
        "relevance",
        "authority",
        "engagement",
        "consistency",
        "freshness",
        "credibility",
    ]
    label: str
    score: int = Field(ge=0, le=100)
    weight: float = Field(ge=0, le=1)
    reason: str


class AccountEvidence(BaseModel):
    kind: Literal["profile", "domain_content", "recent_activity"]
    summary: str
    native_content_id: str | None = None
    url: str | None = None
    observed_at: datetime


class AccountItem(BaseModel):
    platform: PlatformId
    native_id: str
    provider: str
    username: str
    display_name: str | None = None
    description: str | None = None
    profile_url: str | None = None
    profile_image_url: str | None = None
    followers_count: int = Field(default=0, ge=0)
    following_count: int = Field(default=0, ge=0)
    post_count: int = Field(default=0, ge=0)
    listed_count: int = Field(default=0, ge=0)
    verified: bool = False
    protected: bool = False
    score: int = Field(ge=0, le=100)
    confidence: Literal["High", "Medium", "Low"]
    score_version: str
    score_components: list[AccountScoreComponent]
    evidence: list[AccountEvidence]
    recent_content: list[ContentItem]
    observed_at: datetime


class ContentMetrics(BaseModel):
    likes: int | None = Field(default=None, ge=0)
    reposts: int | None = Field(default=None, ge=0)
    replies: int | None = Field(default=None, ge=0)
    quotes: int | None = Field(default=None, ge=0)
    views: int | None = Field(default=None, ge=0)
    bookmarks: int | None = Field(default=None, ge=0)


class TrendItem(BaseModel):
    platform: PlatformId
    native_key: str
    name: str
    category: str | None = None
    locale: str | None = None
    rank: int | None = Field(default=None, ge=1)
    post_count: int | None = Field(default=None, ge=0)
    url: str | None = None
    provider: str
    native_semantics: bool
    observed_at: datetime
    context: str | None = None


class ContentItem(BaseModel):
    platform: PlatformId
    native_id: str
    provider: str
    author: AccountSummary
    title: str | None = None
    text: str
    canonical_url: str | None = None
    published_at: datetime | None = None
    observed_at: datetime
    language: str | None = None
    conversation_id: str | None = None
    reference_type: str | None = None
    referenced_content_id: str | None = None
    metrics: ContentMetrics


class DiscoveryDomain(BaseModel):
    key: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,39}$")
    name: str = Field(min_length=1, max_length=80)
    query: str = Field(min_length=1, max_length=500)
    exclusions: tuple[str, ...] = ()
    languages: tuple[str, ...] = ()
    enabled: bool = True


class DiscoveryDomainListResponse(BaseModel):
    items: list[DiscoveryDomain]
    total: int


class HotContentScoreComponents(BaseModel):
    velocity: float = Field(ge=0, le=100)
    engagement: float = Field(ge=0, le=100)
    recency: float = Field(ge=0, le=100)
    relevance: float = Field(ge=0, le=100)
    author_quality: float = Field(ge=0, le=100)


class HotContentItem(BaseModel):
    content: ContentItem
    source: Literal["discover", "following"]
    hot_score: float = Field(ge=0, le=100)
    engagement_total: int = Field(ge=0)
    velocity_per_hour: float = Field(ge=0)
    matched_domains: tuple[str, ...] = ()
    score_components: HotContentScoreComponents
    reason: str


class HotContentListResponse(BaseModel):
    meta: ConnectorResponseMeta
    items: list[HotContentItem]
    total: int


class ReplyDraftRequest(BaseModel):
    tone: Literal["thoughtful", "supportive", "curious", "concise"] = "thoughtful"
    language: str = Field(default="auto", min_length=2, max_length=20)


class ReplyDraftResponse(BaseModel):
    platform: PlatformId
    content_id: str
    draft: str = Field(min_length=1, max_length=280)
    reply_url: str
    model: str
    cached: bool = False
    generated_at: datetime


class ConnectorResponseMeta(BaseModel):
    platform: PlatformId
    provider: str
    attempted_providers: tuple[str, ...]
    observed_at: datetime
    warnings: tuple[str, ...] = ()


class TrendListResponse(BaseModel):
    meta: ConnectorResponseMeta
    items: list[TrendItem]
    total: int


class ContentListResponse(BaseModel):
    meta: ConnectorResponseMeta
    items: list[ContentItem]
    total: int


class AccountListResponse(BaseModel):
    meta: ConnectorResponseMeta
    items: list[AccountItem]
    total: int


class AccountTrackingResponse(BaseModel):
    meta: ConnectorResponseMeta
    item: AccountTrackingItem


class TrackedAccountListResponse(BaseModel):
    meta: ConnectorResponseMeta
    items: list[TrackedAccountItem]
    total: int
