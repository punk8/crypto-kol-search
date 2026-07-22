from __future__ import annotations

from collections.abc import Callable
from typing import Any

from kol_search.twitter.base import TwitterBackendError, XReadProvider
from kol_search.twitter.models import XAccount, XReadCapabilities, XTweet, XTrend


def _fallback_eligible(exc: TwitterBackendError) -> bool:
    if exc.status_code is None:
        return True
    return exc.status_code in {401, 402, 403, 408, 429} or exc.status_code >= 500


class FailoverTwitterClient:
    """Sticky per-run failover from a primary Twitter backend to a fallback backend."""

    def __init__(self, primary: XReadProvider, fallback: XReadProvider) -> None:
        self.primary = primary
        self.fallback = fallback
        self.name = f"{primary.name}+{fallback.name}"
        self.capabilities = XReadCapabilities(
            **{
                field: max(
                    getattr(primary.capabilities, field),
                    getattr(fallback.capabilities, field),
                )
                for field in XReadCapabilities.model_fields
            }
        )
        # Preserve the primary's full capability set until failover actually happens.
        self.capabilities.verified_followers = primary.capabilities.verified_followers
        self._failed_over = False
        self.warnings: list[str] = []
        self.diagnostics: dict[str, Any] = self._new_diagnostics()

    def _new_diagnostics(self) -> dict[str, Any]:
        return {
            "active_backend": self.primary.name,
            "backend_calls": {self.primary.name: 0, self.fallback.name: 0},
            "fallback_count": 0,
            "fallback_reason": None,
        }

    def begin_run(self) -> None:
        """Reset sticky failover at the boundary of one core platform job."""

        self._failed_over = False
        self.warnings.clear()
        self.diagnostics = self._new_diagnostics()
        for client in (self.primary, self.fallback):
            begin_run = getattr(client, "begin_run", None)
            if callable(begin_run):
                begin_run()

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

    def search_users(self, query: str, max_results: int = 100) -> list[XAccount]:
        return self._call("search_users", query, max_results=max_results)

    def search_tweets(
        self, query: str, max_results: int = 40, since_id: str | None = None
    ) -> list[XTweet]:
        return self._call("search_tweets", query, max_results=max_results, since_id=since_id)

    def get_user_by_username(self, username: str) -> XAccount | None:
        return self._call("get_user_by_username", username)

    def get_users_by_usernames(self, usernames: list[str]) -> list[XAccount]:
        return self._call("get_users_by_usernames", usernames)

    def get_user_tweets(
        self,
        user_id: str,
        max_results: int = 10,
        *,
        username: str | None = None,
        include_replies: bool = False,
    ) -> list[XTweet]:
        if user_id.startswith("opencli:") and self.fallback.capabilities.user_timeline:
            self.diagnostics["backend_calls"][self.fallback.name] += 1
            return self.fallback.get_user_tweets(
                user_id,
                max_results=max_results,
                username=username,
                include_replies=include_replies,
            )
        return self._call(
            "get_user_tweets",
            user_id,
            max_results=max_results,
            username=username,
            include_replies=include_replies,
        )

    def get_followings(self, username: str, max_results: int = 20) -> list[XAccount]:
        return self._call("get_followings", username, max_results=max_results)

    def get_verified_followers(
        self,
        user_id: str,
        max_results: int = 20,
        *,
        username: str | None = None,
    ) -> list[XAccount]:
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

    def get_trends(self, max_results: int = 20) -> list[XTrend]:
        if not self.primary.capabilities.trends and self.fallback.capabilities.trends:
            self.diagnostics["backend_calls"][self.fallback.name] += 1
            return self.fallback.get_trends(max_results=max_results)
        return self._call("get_trends", max_results=max_results)

    def close(self) -> None:
        for client in (self.primary, self.fallback):
            close: Callable[[], Any] | None = getattr(client, "close", None)
            if close:
                close()
