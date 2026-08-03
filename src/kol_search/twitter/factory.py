from __future__ import annotations

from collections.abc import Callable

from kol_search.settings import Settings, get_settings
from kol_search.twitter.base import TwitterBackendError, XReadProvider
from kol_search.twitter.mock import MockTwitterClient
from kol_search.twitter.official import OfficialTwitterClient
from kol_search.twitter.third_party import ThirdPartyTwitterClient


def create_x_read_provider(
    backend: str | None = None,
    settings: Settings | None = None,
    resolve_account_id: Callable[[str, str], str] | None = None,
) -> XReadProvider:
    """
    Build the selected X read provider.

    backend: mock | official | third_party | twitterapi_io | twscrape | opencli
    """
    settings = settings or get_settings()
    name = (backend or settings.twitter_backend or "twitterapi_io").lower().strip()

    def build_opencli() -> XReadProvider:
        from kol_search.twitter.opencli import OpenCliTwitterClient

        return OpenCliTwitterClient(
            command=settings.opencli_command,
            profile=settings.opencli_profile,
            timeout=settings.opencli_timeout_seconds,
            resolve_account_id=resolve_account_id,
        )

    if name == "mock":
        if not settings.enable_mock_backend:
            raise TwitterBackendError(
                "Mock backend is disabled outside explicit test/demo mode.",
                hint="Use a configured real backend, or set KOL_ENABLE_MOCK_BACKEND=true only for tests.",
            )
        return MockTwitterClient()

    if name == "official":
        token = settings.x_api_bearer_token()
        if not token:
            raise TwitterBackendError(
                "Official backend requires X_BEARER_TOKEN.",
                hint="Copy .env.example → .env and follow README.md",
            )
        primary = OfficialTwitterClient(
            bearer_token=token,
            trend_woeid=settings.x_trend_woeid,
        )
        if settings.twitter_fallback_backend == "opencli":
            from kol_search.twitter.failover import FailoverTwitterClient

            return FailoverTwitterClient(primary, build_opencli())
        return primary

    if name == "third_party":
        return ThirdPartyTwitterClient(
            base_url=settings.twitter_tp_base_url or "",
            api_key=settings.twitter_tp_api_key or "",
            api_key_header=settings.twitter_tp_api_key_header,
            supports_user_search=settings.twitter_tp_supports_user_search,
        )

    if name == "opencli":
        return build_opencli()

    if name == "twitterapi_io":
        from kol_search.twitter.twitterapi_io import TwitterApiIoClient

        if not settings.twitterapi_io_api_key:
            if settings.twitter_fallback_backend == "opencli":
                return build_opencli()
            raise TwitterBackendError("TwitterAPI.io backend requires TWITTERAPI_IO_API_KEY.")

        primary = TwitterApiIoClient(
            api_key=settings.twitterapi_io_api_key or "",
            base_url=settings.twitterapi_io_base_url,
        )
        if settings.twitter_fallback_backend == "opencli":
            from kol_search.twitter.failover import FailoverTwitterClient

            return FailoverTwitterClient(primary, build_opencli())
        return primary

    if name == "twscrape":
        if not settings.enable_twscrape:
            raise TwitterBackendError(
                "twscrape backend is disabled.",
                hint="Set ENABLE_TWSCRAPE=true only after reviewing X terms and account-ban risk.",
            )
        from kol_search.twitter.twscrape_backend import TwscrapeTwitterClient

        return TwscrapeTwitterClient(accounts_file=settings.twscrape_accounts_file)

    raise TwitterBackendError(
        f"Unknown TWITTER_BACKEND={name!r}",
        hint="Use one of: mock, official, third_party, twitterapi_io, twscrape, opencli",
    )
