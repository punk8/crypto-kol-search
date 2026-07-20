from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel

from kol_search.models import Account, PlatformName, Post, TrendSignal


class PlatformHealth(BaseModel):
    platform: PlatformName
    ready: bool
    account: str | None = None
    detail: str | None = None


class PlatformReader(Protocol):
    platform: PlatformName
    provider: str

    def search_posts(self, query: str, limit: int = 20) -> list[Post]: ...

    def get_account(self, external_id: str) -> Account | None: ...

    def get_account_posts(self, external_id: str, limit: int = 20) -> list[Post]: ...

    def get_trends(self, limit: int = 20) -> list[TrendSignal]: ...

    def get_comments(self, post_url: str, limit: int = 20) -> list[dict]: ...

    def health_check(self) -> PlatformHealth: ...

    def close(self) -> None: ...
