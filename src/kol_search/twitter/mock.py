from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from kol_search.twitter.models import XAccount, XReadCapabilities, XTweet, XTrend
from kol_search.settings import PROJECT_ROOT


def _load_json(name: str) -> list[dict]:
    path = PROJECT_ROOT / "fixtures" / name
    with path.open(encoding="utf-8") as f:
        return json.load(f)


class MockTwitterClient:
    """Offline deterministic backend using fixtures/."""

    name = "mock"
    capabilities = XReadCapabilities(
        user_search=True, followings=True, verified_followers=True, trends=True
    )

    def __init__(self, fixtures_dir: Path | None = None) -> None:
        root = fixtures_dir or (PROJECT_ROOT / "fixtures")
        users_raw = json.loads((root / "mock_users.json").read_text(encoding="utf-8"))
        tweets_raw = json.loads((root / "mock_tweets.json").read_text(encoding="utf-8"))
        parsed_dates = [
            datetime.fromisoformat(str(item["created_at"]).replace("Z", "+00:00"))
            for item in tweets_raw
            if item.get("created_at")
        ]
        fixture_latest = max(parsed_dates) if parsed_dates else None
        # Keep fixture content recent without changing its timestamp on every
        # client/process construction. Stable timestamps are required for the
        # persisted per-account scan cursor to suppress already-seen content.
        demo_latest = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        self._users: dict[str, XAccount] = {}
        self._by_id: dict[str, XAccount] = {}
        for u in users_raw:
            acc = XAccount(
                id=str(u["id"]),
                username=u["username"],
                name=u.get("name"),
                description=u.get("description"),
                followers_count=int(u.get("followers_count", 0)),
                following_count=int(u.get("following_count", 0)),
                tweet_count=int(u.get("tweet_count", 0)),
                listed_count=int(u.get("listed_count", 0)),
                verified=bool(u.get("verified", False)),
                created_at=u.get("created_at"),
                profile_image_url=u.get("profile_image_url"),
                url=u.get("url"),
                location=u.get("location"),
                entities=u.get("entities") or {},
                protected=bool(u.get("protected", False)),
                raw=u,
            )
            self._users[acc.username.lower()] = acc
            self._by_id[acc.id] = acc
        self._posts: list[XTweet] = []
        for t in tweets_raw:
            post_id = str(t["id"])
            username = t.get("author_username")
            created_at = t.get("created_at")
            if fixture_latest and created_at:
                original = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
                created_at = (demo_latest - (fixture_latest - original)).isoformat()
            self._posts.append(
                XTweet(
                    id=post_id,
                    author_id=str(t["author_id"]),
                    author_username=t.get("author_username"),
                    text=t.get("text", ""),
                    created_at=created_at,
                    like_count=int(t.get("like_count", 0)),
                    retweet_count=int(t.get("retweet_count", 0)),
                    reply_count=int(t.get("reply_count", 0)),
                    quote_count=int(t.get("quote_count", 0)),
                    view_count=int(t.get("view_count", 0)),
                    bookmark_count=int(t.get("bookmark_count", 0)),
                    lang=t.get("lang"),
                    url=t.get("url") or (
                        f"https://x.com/{username}/status/{post_id}" if username else None
                    ),
                    conversation_id=str(t.get("conversation_id") or post_id),
                    in_reply_to_user_id=t.get("in_reply_to_user_id"),
                    in_reply_to_username=t.get("in_reply_to_username"),
                    referenced_post_id=t.get("referenced_post_id"),
                    reference_type=t.get("reference_type"),
                    mentioned_usernames=[m.lstrip("@") for m in t.get("mentioned_usernames", [])],
                    raw=t,
                )
            )

    def search_tweets(
        self,
        query: str,
        max_results: int = 40,
        since_id: str | None = None,
    ) -> list[XTweet]:
        # Simple token match: split on OR/AND/quotes-ish
        tokens = [t for t in re.split(r"\s+OR\s+|\s+AND\s+|\s+", query, flags=re.I) if t]
        tokens = [t.strip('"()').lstrip("@$").lower() for t in tokens if t.strip('"()')]
        tokens = [t for t in tokens if t and t not in {"or", "and"}]

        matched: list[XTweet] = []
        for p in self._posts:
            blob = p.text.lower()
            if any(tok in blob for tok in tokens):
                matched.append(p)
            # also match mention queries like (@handle)
            for tok in tokens:
                if tok.startswith("@") and p.author_username:
                    if p.author_username.lower() == tok.lstrip("@").lower():
                        matched.append(p)
        # de-dupe by id, sort by engagement
        seen: set[str] = set()
        uniq: list[XTweet] = []
        for p in sorted(matched, key=lambda x: x.engagement, reverse=True):
            if p.id in seen:
                continue
            seen.add(p.id)
            uniq.append(p)
        return uniq[:max_results]

    def search_users(self, query: str, max_results: int = 100) -> list[XAccount]:
        tokens = [t.lower().lstrip("@$#") for t in re.findall(r"[\w\u4e00-\u9fff-]+", query)]
        ignored = {"or", "and", "crypto", "web3"}
        tokens = [t for t in tokens if t not in ignored]
        scored: list[tuple[int, XAccount]] = []
        for account in self._users.values():
            blob = f"{account.username} {account.name or ''} {account.description or ''}".lower()
            score = sum(1 for token in tokens if token in blob)
            if score or not tokens:
                scored.append((score, account))
        scored.sort(key=lambda item: (item[0], item[1].followers_count), reverse=True)
        return [account for _, account in scored[:max_results]]

    def get_user_by_username(self, username: str) -> XAccount | None:
        return self._users.get(username.lstrip("@").lower())

    def get_users_by_usernames(self, usernames: list[str]) -> list[XAccount]:
        out: list[XAccount] = []
        for u in usernames:
            acc = self.get_user_by_username(u)
            if acc:
                out.append(acc)
        return out

    def get_user_tweets(
        self,
        user_id: str,
        max_results: int = 10,
        *,
        username: str | None = None,
        include_replies: bool = False,
    ) -> list[XTweet]:
        posts = [p for p in self._posts if p.author_id == str(user_id)]
        posts.sort(key=lambda p: p.created_at or "", reverse=True)
        return posts[:max_results]

    def get_followings(self, username: str, max_results: int = 20) -> list[XAccount]:
        source = self.get_user_by_username(username)
        if source is None:
            return []
        values = [a for a in self._by_id.values() if a.id != source.id]
        start = int(source.id) % max(1, len(values)) if values else 0
        return (values[start:] + values[:start])[:max_results]

    def get_verified_followers(
        self, user_id: str, max_results: int = 20, *, username: str | None = None
    ) -> list[XAccount]:
        values = [a for a in self._by_id.values() if a.id != str(user_id) and a.verified]
        return values[:max_results]

    def get_trends(self, max_results: int = 20) -> list[XTrend]:
        return [
            XTrend(name="DeFi", rank=1, post_count=12000),
            XTrend(name="Bitcoin", rank=2, post_count=9000),
            XTrend(name="Solana", rank=3, post_count=6000),
        ][:max_results]
