from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from kol_search.discovery.promotion import KolStatus
from kol_search.platforms import PlatformRegistry
from kol_search.platforms.x import XAccountRelation, XTweet, create_x_plugin
from kol_search.platforms.xiaohongshu import (
    XiaohongshuComment,
    create_xiaohongshu_plugin,
)
from kol_search.twitter.models import XAccount as ProviderXAccount
from kol_search.twitter.models import XTweet as ProviderXTweet
from kol_search.settings import Settings


class NativeXProvider:
    name = "fixture"

    def get_followings(self, _username: str, _limit: int):
        return [
            ProviderXAccount(id="following-id", username="following", followers_count=50)
        ]

    def get_verified_followers(
        self,
        _user_id: str,
        _limit: int,
        *,
        username: str | None = None,
    ):
        assert username == "seed"
        return [
            ProviderXAccount(
                id="verified-id",
                username="verified_follower",
                verified=True,
            )
        ]

    def search_tweets(self, query: str, _limit: int):
        assert "to:seed" in query
        now = datetime.now(timezone.utc).isoformat()
        return [
            ProviderXTweet(
                id="reply-1",
                author_id="spam-id",
                author_username="spam_author",
                text="crypto reply",
                created_at=now,
                in_reply_to_user_id="seed-id",
                in_reply_to_username="seed",
                reference_type="replied_to",
                url="https://x.com/spam_author/status/reply-1",
            ),
            ProviderXTweet(
                id="quote-1",
                author_id="disabled-id",
                author_username="disabled_author",
                text="bitcoin quote",
                created_at=now,
                reference_type="quoted",
                referenced_post_id="seed-post",
                url="https://x.com/disabled_author/status/quote-1",
            ),
            ProviderXTweet(
                id="mention-1",
                author_id="healthy-id",
                author_username="healthy_author",
                text="ethereum mention @seed",
                created_at=now,
                mentioned_usernames=["seed"],
                url="https://x.com/healthy_author/status/mention-1",
            ),
            ProviderXTweet(
                id="mention-protected",
                author_id="protected-id",
                author_username="protected_author",
                text="web3 mention @seed",
                created_at=now,
                mentioned_usernames=["seed"],
                url="https://x.com/protected_author/status/mention-protected",
            ),
        ]

    def get_users_by_usernames(self, usernames: list[str]):
        values = {
            "spam_author": ProviderXAccount(
                id="spam-id",
                username="spam_author",
                followers_count=20,
                description="Free airdrop, guaranteed profit",
            ),
            "disabled_author": ProviderXAccount(
                id="disabled-id",
                username="disabled_author",
                followers_count=50_000,
                raw={"suspended": True},
            ),
            "healthy_author": ProviderXAccount(
                id="healthy-id",
                username="healthy_author",
                followers_count=1_000_000,
            ),
            "protected_author": ProviderXAccount(
                id="protected-id",
                username="protected_author",
                followers_count=1_000_000,
                protected=True,
            ),
        }
        return [values[value] for value in usernames]


def test_x_native_relations_are_candidates_with_traceable_evidence_and_risk(
    tmp_path: Path,
) -> None:
    settings = Settings(_env_file=None, KOL_DB_PATH=str(tmp_path / "x-native.db"))
    plugin = create_x_plugin(settings, client_factory=NativeXProvider)
    adapter = plugin.automation_adapter
    assert adapter is not None
    adapter.add_seed("seed-id", "Seed")

    result = PlatformRegistry([plugin]).dispatch(
        "x",
        "discover",
        payload={
            "seed_accounts": [
                {"id": "seed-id", "handle": "seed", "status": "seed"}
            ],
            "search_accounts": False,
            "search_content": False,
        },
    )

    relations = [item for item in result.items if isinstance(item, XAccountRelation)]
    assert {item.relation_type for item in relations} == {
        "following",
        "verified_follower",
        "reply",
        "quote",
        "mention",
    }
    assert {
        item.evidence_external_id for item in relations if item.evidence_external_id
    } == {"reply-1", "quote-1", "mention-1", "mention-protected"}
    assert len([item for item in result.items if isinstance(item, XTweet)]) == 4

    adapter.project_discovery(result.items, payload={})
    assert adapter.repository.relationship_evidence_count("healthy-id") == 1
    assert adapter.get_kol("healthy-id")["status"] == KolStatus.ACTIVE.value
    spam = adapter.get_kol("spam-id")
    disabled = adapter.get_kol("disabled-id")
    protected = adapter.get_kol("protected-id")
    assert spam["status"] == KolStatus.REJECTED.value
    assert spam["spam_risk"] >= 0.7
    assert "spam_phrase_in_bio" in spam["anomaly_signals"]
    assert "spam risk exceeds threshold" in spam["reasons"]
    assert disabled["status"] == KolStatus.REJECTED.value
    assert disabled["disabled"] == 1
    assert "account is disabled" in disabled["reasons"]
    assert protected["status"] == KolStatus.REJECTED.value
    assert protected["protected"] == 1
    assert "account is protected" in protected["reasons"]

    with sqlite3.connect(settings.db_path()) as connection:
        evidence = connection.execute(
            """
            SELECT evidence, evidence_url FROM x_discovery_evidence
            WHERE target_account_id='healthy-id'
            """
        ).fetchone()
    assert evidence == (
        "mention:mention-1",
        "https://x.com/healthy_author/status/mention-1",
    )


class NativeXiaohongshuProvider:
    provider = "fixture"

    def get_account_posts(self, _external_id: str, _limit: int):
        return [
            {
                "id": "xiaohongshu:seed-note",
                "author_id": "xiaohongshu:seed-user",
                "author_username": "Seed",
                "text": "区块链研究\n原生信号扫描",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "url": "https://www.xiaohongshu.com/explore/seed-note",
            }
        ]

    def get_comments(self, _url: str, _limit: int):
        return [
            {
                "id": "comment-spam",
                "content": "研究得很好",
                "user": {
                    "id": "spam-user",
                    "nickname": "Spam",
                    "bio": "加微信 私信返利",
                },
            },
            {
                "id": "comment-disabled",
                "content": "学习了",
                "user": {
                    "id": "disabled-user",
                    "nickname": "Disabled",
                    "status": "banned",
                },
            },
            {
                "id": "comment-private",
                "content": "先收藏",
                "user": {
                    "id": "private-user",
                    "nickname": "Private",
                    "private": True,
                },
            },
        ]

    def get_trends(self, _limit: int):
        return []


def test_xiaohongshu_commenter_safety_signals_reach_promotion_policy(
    tmp_path: Path,
) -> None:
    settings = Settings(_env_file=None, KOL_DB_PATH=str(tmp_path / "xhs-native.db"))
    plugin = create_xiaohongshu_plugin(
        settings,
        provider_factory=NativeXiaohongshuProvider,
    )
    adapter = plugin.automation_adapter
    assert adapter is not None
    adapter.add_seed("seed-user", "Seed")

    result = PlatformRegistry([plugin]).dispatch(
        "xiaohongshu",
        "scan_signals",
        payload={
            "account_external_ids": ["seed-user"],
            "include_trends": False,
            "include_comments": True,
            "comment_threads_limit": 1,
        },
    )
    comments = [
        item for item in result.items if isinstance(item, XiaohongshuComment)
    ]
    assert len(comments) == 3
    assert comments[0].author_spam_risk >= 0.7
    assert comments[1].author_disabled is True
    assert comments[2].author_protected is True

    adapter.project_signals(result.items, payload={})
    spam = adapter.get_kol("spam-user")
    disabled = adapter.get_kol("disabled-user")
    protected = adapter.get_kol("private-user")
    assert adapter.repository.relationship_evidence_count("spam-user") == 1
    assert spam["status"] == KolStatus.REJECTED.value
    assert "spam_phrase_in_bio" in spam["anomaly_signals"]
    assert "spam risk exceeds threshold" in spam["reasons"]
    assert disabled["status"] == KolStatus.REJECTED.value
    assert "provider_disabled" in disabled["anomaly_signals"]
    assert "account is disabled" in disabled["reasons"]
    assert protected["status"] == KolStatus.REJECTED.value
    assert "private_profile" in protected["anomaly_signals"]
    assert "account is protected" in protected["reasons"]
