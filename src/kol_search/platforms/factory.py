from __future__ import annotations

from collections.abc import Callable

from kol_search.platforms.base import PlatformReader
from kol_search.platforms.opencli import OpenCliXiaohongshuReader, OpenCliXReader
from kol_search.settings import Settings
from kol_search.twitter.factory import create_twitter_client


def create_platform_readers(
    settings: Settings,
    *,
    x_backend: str | None = None,
    resolve_account_id: Callable[[str, str], str] | None = None,
    platforms: tuple[str, ...] = ("x", "xiaohongshu"),
) -> dict[str, PlatformReader]:
    readers: dict[str, PlatformReader] = {}
    if "x" in platforms:
        client = create_twitter_client(
            x_backend or settings.twitter_backend,
            settings,
            resolve_account_id,
        )
        readers["x"] = OpenCliXReader(client) if client.name == "opencli" else OpenCliXReader(client)  # type: ignore[arg-type]
    if "xiaohongshu" in platforms:
        readers["xiaohongshu"] = OpenCliXiaohongshuReader(
            command=settings.opencli_command,
            profile=settings.xiaohongshu_opencli_profile,
            timeout=settings.opencli_timeout_seconds,
        )
    return readers
