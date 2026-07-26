from __future__ import annotations

from typing import TYPE_CHECKING

from kol_search.settings import Settings, get_settings
from kol_search.twitter.base import TwitterBackendError, XReadProvider
from kol_search.twitter.mock import MockTwitterClient
from kol_search.twitter.official import OfficialTwitterClient
from kol_search.twitter.third_party import ThirdPartyTwitterClient

if TYPE_CHECKING:
    from kol_search.backend.provider_state import ProviderStateStore


def create_x_read_provider(
    backend: str | None = None,
    settings: Settings | None = None,
    state_store: "ProviderStateStore | None" = None,
) -> XReadProvider:
    """
    Build the selected X read provider.

    backend: mock | official | third_party | twitterapi_io | getxapi | twscrape | fxembed
    """
    settings = settings or get_settings()
    name = (backend or settings.twitter_backend or "twitterapi_io").lower().strip()

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
        return OfficialTwitterClient(
            bearer_token=token,
            trend_woeid=settings.x_trend_woeid,
        )

    if name == "third_party":
        return ThirdPartyTwitterClient(
            base_url=settings.twitter_tp_base_url or "",
            api_key=settings.twitter_tp_api_key or "",
            api_key_header=settings.twitter_tp_api_key_header,
            supports_user_search=settings.twitter_tp_supports_user_search,
        )

    if name == "twitterapi_io":
        from kol_search.twitter.twitterapi_io import TwitterApiIoClient

        if not settings.twitterapi_io_api_key:
            raise TwitterBackendError("TwitterAPI.io backend requires TWITTERAPI_IO_API_KEY.")

        return TwitterApiIoClient(
            api_key=settings.twitterapi_io_api_key or "",
            base_url=settings.twitterapi_io_base_url,
            trend_woeid=settings.x_trend_woeid,
        )

    if name == "getxapi":
        from kol_search.twitter.getxapi import GetXApiClient

        if not settings.get_x_api_key:
            raise TwitterBackendError("GetXAPI backend requires GET_X_API_KEY.")
        return GetXApiClient(
            api_key=settings.get_x_api_key,
            base_url=settings.get_x_api_base_url,
            trend_woeid=settings.x_trend_woeid,
            state_store=state_store,
            daily_call_limit=settings.get_x_api_daily_call_limit,
            min_credits=settings.get_x_api_min_credits,
            balance_cache_seconds=settings.get_x_api_balance_cache_seconds,
            estimated_credits_per_call=settings.get_x_api_estimated_credits_per_call,
        )

    if name == "twscrape":
        if not settings.enable_twscrape:
            raise TwitterBackendError(
                "twscrape backend is disabled.",
                hint="Set ENABLE_TWSCRAPE=true only after reviewing X terms and account-ban risk.",
            )
        from kol_search.twitter.twscrape_backend import TwscrapeTwitterClient

        return TwscrapeTwitterClient(
            accounts_file=settings.twscrape_accounts_file,
            accounts_db=str(settings.twscrape_db_path()),
            username=settings.twscrape_username,
            auth_token=settings.twscrape_auth_token,
            ct0=settings.twscrape_ct0,
            wait_timeout=settings.twscrape_wait_timeout_seconds,
        )

    if name == "fxembed":
        if not settings.enable_fxembed:
            raise TwitterBackendError("FxEmbed backend is disabled.")
        from kol_search.twitter.fxembed import FxEmbedTwitterClient

        return FxEmbedTwitterClient(
            base_url=settings.fxembed_base_url,
            timeout=settings.fxembed_timeout_seconds,
        )

    raise TwitterBackendError(
        f"Unknown TWITTER_BACKEND={name!r}",
        hint="Use one of: mock, official, third_party, twitterapi_io, getxapi, twscrape, fxembed",
    )
