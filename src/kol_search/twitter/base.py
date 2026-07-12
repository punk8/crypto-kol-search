from __future__ import annotations

from typing import Protocol, runtime_checkable

from kol_search.models import Account, BackendCapabilities, Post


def merge_client_diagnostics(
    client: object, stats: dict, warnings: list[str]
) -> None:
    diagnostics = getattr(client, "diagnostics", None)
    if isinstance(diagnostics, dict):
        stats.update(diagnostics)
    for warning in getattr(client, "warnings", []) or []:
        if warning not in warnings:
            warnings.append(warning)


class TwitterBackendError(Exception):
    """Raised when a Twitter backend fails in a user-visible way."""

    def __init__(self, message: str, *, hint: str | None = None, status_code: int | None = None):
        super().__init__(message)
        self.hint = hint
        self.status_code = status_code

    def __str__(self) -> str:
        base = super().__str__()
        parts = [base]
        if self.status_code is not None:
            parts.append(f"(HTTP {self.status_code})")
        if self.hint:
            parts.append(f"Hint: {self.hint}")
        return " ".join(parts)


@runtime_checkable
class TwitterClient(Protocol):
    """Pluggable Twitter/X data access."""

    name: str
    capabilities: BackendCapabilities

    def search_users(self, query: str, max_results: int = 100) -> list[Account]:
        """Search public user profiles. Returns [] when the backend lacks this capability."""
        ...

    def search_tweets(
        self,
        query: str,
        max_results: int = 40,
        since_id: str | None = None,
    ) -> list[Post]:
        """Search recent posts matching query."""
        ...

    def get_user_by_username(self, username: str) -> Account | None:
        ...

    def get_users_by_usernames(self, usernames: list[str]) -> list[Account]:
        ...

    def get_user_tweets(
        self,
        user_id: str,
        max_results: int = 10,
        *,
        username: str | None = None,
    ) -> list[Post]:
        ...

    def get_followings(self, username: str, max_results: int = 20) -> list[Account]:
        """Return full public profiles followed by the user when supported."""
        ...

    def get_verified_followers(
        self,
        user_id: str,
        max_results: int = 20,
        *,
        username: str | None = None,
    ) -> list[Account]:
        """Return verified follower profiles when supported."""
        ...
