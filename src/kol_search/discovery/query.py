from __future__ import annotations

import re
from dataclasses import dataclass

from kol_search.models import DomainConfig


DEFAULT_TOPIC_ALIASES: dict[str, list[str]] = {
    "bitcoin": ["bitcoin", "btc", "比特币", "比特幣"],
    "ethereum_l2": ["ethereum", "eth", "layer 2", "rollup", "以太坊", "二层", "扩容"],
    "solana": ["solana", "sol", "索拉纳"],
    "defi": ["defi", "decentralized finance", "去中心化金融"],
    "stablecoin_rwa": ["stablecoin", "rwa", "real world assets", "稳定币", "真实世界资产"],
    "nft_gamefi": ["nft", "gamefi", "web3 gaming", "链游", "加密艺术"],
    "memecoin": ["memecoin", "meme coin", "meme", "模因币", "土狗"],
    "airdrop": ["airdrop", "testnet", "points", "空投", "测试网", "积分"],
    "onchain_security": ["onchain", "on-chain", "crypto security", "链上分析", "链上安全"],
    "trading_macro": ["crypto trading", "macro", "liquidity", "加密交易", "宏观", "流动性"],
    "regulation": ["crypto regulation", "mica", "sec", "加密监管", "政策"],
    "infra_ai_depin": ["crypto infrastructure", "depin", "crypto ai", "加密基础设施", "去中心化物理基础设施"],
}


@dataclass(frozen=True)
class QueryPlan:
    topic: str
    aliases: list[str]
    user_queries: list[str]
    post_queries: list[str]


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        value = value.strip()
        key = value.lower()
        if value and key not in seen:
            seen.add(key)
            output.append(value)
    return output


def all_aliases(config: DomainConfig) -> dict[str, list[str]]:
    merged = {key: list(values) for key, values in DEFAULT_TOPIC_ALIASES.items()}
    for key, values in config.topic_aliases.items():
        merged[key] = _dedupe(merged.get(key, []) + values)
    return merged


def plan_queries(
    topic: str,
    config: DomainConfig,
    *,
    max_user_queries: int = 8,
    max_post_queries: int = 8,
) -> QueryPlan:
    terms = _dedupe(re.split(r"[,，;；\n]+", topic))
    aliases = list(terms)
    mapping = all_aliases(config)
    topic_blob = " ".join(terms).lower()
    for key, values in mapping.items():
        if key.replace("_", " ") in topic_blob or any(value.lower() in topic_blob for value in values):
            aliases.extend(values)
    aliases = _dedupe(aliases)

    ascii_aliases = [
        value for value in aliases if value.isascii() and re.search(r"[A-Za-z0-9]", value)
    ]
    user_queries = _dedupe(ascii_aliases or ["crypto"])

    post_queries: list[str] = []
    for value in aliases:
        quoted = f'"{value}"' if " " in value else value
        post_queries.append(f"({quoted}) (crypto OR web3 OR blockchain OR 加密 OR 区块链)")
    if not post_queries:
        post_queries = ["crypto OR web3 OR 区块链"]

    return QueryPlan(
        topic=topic,
        aliases=aliases,
        user_queries=user_queries[:max_user_queries],
        post_queries=_dedupe(post_queries)[:max_post_queries],
    )

