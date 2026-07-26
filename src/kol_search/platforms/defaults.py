from __future__ import annotations

from collections.abc import Iterable

from kol_search.platforms.kernel import PlatformRegistry
from kol_search.platforms.x import create_x_plugin
from kol_search.platforms.xiaohongshu import create_xiaohongshu_plugin


def build_default_registry(
    settings: object,
    *,
    platforms: Iterable[str] | None = None,
) -> PlatformRegistry:
    """Build the code-defined platform registry without performing network I/O."""

    if platforms is None:
        configured = getattr(settings, "platform_ids", None)
        if callable(configured):
            platforms = configured()
        else:
            raw = str(getattr(settings, "enabled_platforms", "x"))
            platforms = raw.split(",")
    selected = tuple(dict.fromkeys(value.strip().lower() for value in platforms))
    unknown = set(selected) - {"x", "xiaohongshu"}
    if unknown:
        raise ValueError(f"Unknown built-in platforms: {', '.join(sorted(unknown))}")

    registry = PlatformRegistry()
    if "x" in selected:
        registry.register(create_x_plugin(settings))
    if "xiaohongshu" in selected:
        registry.register(create_xiaohongshu_plugin(settings))
    return registry


__all__ = ["build_default_registry"]
