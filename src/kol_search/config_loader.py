from __future__ import annotations

from pathlib import Path

import yaml

from kol_search.models import DomainConfig
from kol_search.settings import PROJECT_ROOT


def load_domain_config(domain: str = "crypto") -> DomainConfig:
    """Load domain YAML from config/domain_{domain}.yaml."""
    path = PROJECT_ROOT / "config" / f"domain_{domain}.yaml"
    if not path.exists():
        raise FileNotFoundError(
            f"Domain config not found: {path}. Available domains need config/domain_*.yaml"
        )
    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return DomainConfig.model_validate(data)


def load_seed_handles(seed_file: str) -> list[str]:
    """Load seed usernames from a text file (one handle per line)."""
    path = Path(seed_file)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    if not path.exists():
        raise FileNotFoundError(f"Seed file not found: {path}")

    handles: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        handles.append(line.lstrip("@"))
    # de-dupe preserving order
    seen: set[str] = set()
    out: list[str] = []
    for h in handles:
        key = h.lower()
        if key not in seen:
            seen.add(key)
            out.append(h)
    return out
