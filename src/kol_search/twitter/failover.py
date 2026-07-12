from __future__ import annotations

from collections.abc import Callable
from typing import Any

from kol_search.models import Account, BackendCapabilities, Post
from kol_search.twitter.base import TwitterBackendError, TwitterClient


def _fallback_eligible(exc: TwitterBackendError) -> bool:
    if exc.status_code is None:
        return True
    return exc.status_code in {401, 402, 403, 408, 429} or exc.status_code >= 500


class FailoverTwitterClient:
    """Sticky per-run failover from a primary Twitter backend to a fallback backend."""

    def __init__(self, primary: TwitterClient, fallback: TwitterClient) -> None:
        self.primary = primary
        self.fallback = fallback
        self.name = f"{primary.name}+{fallback.name}"
        self.capabilities = BackendCapabilities(
            **{
                field: max(
                    getattr(primary.capabilities, field),
                    getattr(fallback.capabilities, field),
                )
                for field in BackendCapabilities.model_fields
            }
        )
        # Preserve the primary's full capability set until failover actually happens.
        self.capabilities.verified_followers = primary.capabilities.verified_followers
        self._failed_over = False
        self.warnings: list[str] = []
        self.diagnostics: dict[str, Any] = {
            "active_backend": primary.name,
            "backend_calls": {primary.name: 0, fallback.name: 0},
            "fallback_count": 0,
            "fallback_reason": None,
        }

    def _call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        if self._failed_over:
            self.diagnostics["backend_calls"][self.fallback.name] += 1
            return getattr(self.fallback, method)(*args, **kwargs)
        self.diagnostics["backend_calls"][self.primary.name] += 1
        try:
            return getattr(self.primary, method)(*args, **kwargs)
        except TwitterBackendError as exc:
            if not _fallback_eligible(exc):
                raise
            self._failed_over = True
            self.diagnostics["active_backend"] = self.fallback.name
            self.diagnostics["fallback_count"] += 1
            self.diagnostics["fallback_reason"] = str(exc)
            self.warnings.append(
                f"{self.primary.name} 不可用，已切换到 {self.fallback.name}: {exc}"
            )
            self.diagnostics["backend_calls"][self.fallback.name] += 1
            try:
                return getattr(self.fallback, method)(*args, **kwargs)
            except TwitterBackendError as fallback_exc:
                raise TwitterBackendError(
                    f"Primary failed ({exc}); fallback failed ({fallback_exc})",
                    hint="Check API availability and the configured OpenCLI Chrome profile.",
                ) from fallback_exc

    def search_users(self, query: str, max_results: int = 100) -> list[Account]:
        return self._call("search_users", query, max_results=max_results)

    def search_tweets(
        self, query: str, max_results: int = 40, since_id: str | None = None
    ) -> list[Post]:
        return self._call("search_tweets", query, max_results=max_results, since_id=since_id)

    def get_user_by_username(self, username: str) -> Account | None:
        return self._call("get_user_by_username", username)

    def get_users_by_usernames(self, usernames: list[str]) -> list[Account]:
        return self._call("get_users_by_usernames", usernames)

    def get_user_tweets(
        self,
        user_id: str,
        max_results: int = 10,
        *,
        username: str | None = None,
    ) -> list[Post]:
        return self._call(
            "get_user_tweets", user_id, max_results=max_results, username=username
        )

    def get_followings(self, username: str, max_results: int = 20) -> list[Account]:
        return self._call("get_followings", username, max_results=max_results)

    def get_verified_followers(
        self,
        user_id: str,
        max_results: int = 20,
        *,
        username: str | None = None,
    ) -> list[Account]:
        if self._failed_over and not self.fallback.capabilities.verified_followers:
            warning = "OpenCLI fallback 不支持 verified followers，已跳过该扩散源"
            if warning not in self.warnings:
                self.warnings.append(warning)
            return []
        values = self._call(
            "get_verified_followers", user_id, max_results=max_results, username=username
        )
        if self._failed_over and not self.fallback.capabilities.verified_followers:
            warning = "OpenCLI fallback 不支持 verified followers，已跳过该扩散源"
            if warning not in self.warnings:
                self.warnings.append(warning)
            return []
        return values

    def close(self) -> None:
        for client in (self.primary, self.fallback):
            close: Callable[[], Any] | None = getattr(client, "close", None)
            if close:
                close()
