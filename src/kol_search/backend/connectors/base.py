from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Generic, Protocol, TypeVar, runtime_checkable

from kol_search.backend.models import (
    AccountContentQuery,
    AccountItem,
    AccountSearchQuery,
    AccountTrackingItem,
    ContentItem,
    ContentLookupQuery,
    ContentSearchQuery,
    PlatformConnectorDescriptor,
    TrendItem,
    TrendQuery,
    TrackedAccountItem,
)


class ConnectorError(RuntimeError):
    """A platform connector cannot serve the requested read operation."""


class ConnectorNotFoundError(ConnectorError):
    """The requested platform has no registered connector."""


T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class ConnectorResult(Generic[T]):
    items: tuple[T, ...]
    provider: str
    attempted_providers: tuple[str, ...]
    warnings: tuple[str, ...] = ()
    observed_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


@runtime_checkable
class PlatformConnector(Protocol):
    platform_id: str

    def descriptor(self) -> PlatformConnectorDescriptor: ...

    def get_trends(self, query: TrendQuery) -> ConnectorResult[TrendItem]: ...

    def search_content(
        self, query: ContentSearchQuery
    ) -> ConnectorResult[ContentItem]: ...

    def search_accounts(
        self, query: AccountSearchQuery
    ) -> ConnectorResult[AccountItem]: ...

    def set_account_tracking(
        self, native_id: str, *, enabled: bool
    ) -> ConnectorResult[AccountTrackingItem]: ...

    def list_tracked_accounts(
        self, *, status: str, limit: int
    ) -> ConnectorResult[TrackedAccountItem]: ...

    def get_account_content(
        self, query: AccountContentQuery
    ) -> ConnectorResult[ContentItem]: ...

    def get_content(
        self, query: ContentLookupQuery
    ) -> ConnectorResult[ContentItem]: ...

    def close(self) -> None: ...


ConnectorFactory = Callable[[], PlatformConnector]
