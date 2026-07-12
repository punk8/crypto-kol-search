from __future__ import annotations

import json
import re
from pathlib import Path

from kol_search.models import Account, BackendCapabilities, Post
from kol_search.settings import PROJECT_ROOT


def _load_json(name: str) -> list[dict]:
    path = PROJECT_ROOT / "fixtures" / name
    with path.open(encoding="utf-8") as f:
        return json.load(f)


class MockTwitterClient:
    """Offline deterministic backend using fixtures/."""

    name = "mock"
    capabilities = BackendCapabilities(user_search=True, followings=True, verified_followers=True)

    def __init__(self, fixtures_dir: Path | None = None) -> None:
        root = fixtures_dir or (PROJECT_ROOT / "fixtures")
        users_raw = json.loads((root / "mock_users.json").read_text(encoding="utf-8"))
        tweets_raw = json.loads((root / "mock_tweets.json").read_text(encoding="utf-8"))
        self._users: dict[str, Account] = {}
        self._by_id: dict[str, Account] = {}
        for u in users_raw:
            acc = Account(
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
        self._posts: list[Post] = []
        for t in tweets_raw:
            self._posts.append(
                Post(
                    id=str(t["id"]),
                    author_id=str(t["author_id"]),
                    author_username=t.get("author_username"),
                    text=t.get("text", ""),
                    created_at=t.get("created_at"),
                    like_count=int(t.get("like_count", 0)),
                    retweet_count=int(t.get("retweet_count", 0)),
                    reply_count=int(t.get("reply_count", 0)),
                    quote_count=int(t.get("quote_count", 0)),
                    mentioned_usernames=[m.lstrip("@") for m in t.get("mentioned_usernames", [])],
                    raw=t,
                )
            )

    def search_tweets(
        self,
        query: str,
        max_results: int = 40,
        since_id: str | None = None,
    ) -> list[Post]:
        # Simple token match: split on OR/AND/quotes-ish
        tokens = [t for t in re.split(r"\s+OR\s+|\s+AND\s+|\s+", query, flags=re.I) if t]
        tokens = [t.strip('"()').lstrip("@$").lower() for t in tokens if t.strip('"()')]
        tokens = [t for t in tokens if t and t not in {"or", "and"}]

        matched: list[Post] = []
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
        uniq: list[Post] = []
        for p in sorted(matched, key=lambda x: x.engagement, reverse=True):
            if p.id in seen:
                continue
            seen.add(p.id)
            uniq.append(p)
        return uniq[:max_results]

    def search_users(self, query: str, max_results: int = 100) -> list[Account]:
        tokens = [t.lower().lstrip("@$#") for t in re.findall(r"[\w\u4e00-\u9fff-]+", query)]
        ignored = {"or", "and", "crypto", "web3"}
        tokens = [t for t in tokens if t not in ignored]
        scored: list[tuple[int, Account]] = []
        for account in self._users.values():
            blob = f"{account.username} {account.name or ''} {account.description or ''}".lower()
            score = sum(1 for token in tokens if token in blob)
            if score or not tokens:
                scored.append((score, account))
        scored.sort(key=lambda item: (item[0], item[1].followers_count), reverse=True)
        return [account for _, account in scored[:max_results]]

    def get_user_by_username(self, username: str) -> Account | None:
        return self._users.get(username.lstrip("@").lower())

    def get_users_by_usernames(self, usernames: list[str]) -> list[Account]:
        out: list[Account] = []
        for u in usernames:
            acc = self.get_user_by_username(u)
            if acc:
                out.append(acc)
        return out

    def get_user_tweets(
        self, user_id: str, max_results: int = 10, *, username: str | None = None
    ) -> list[Post]:
        posts = [p for p in self._posts if p.author_id == str(user_id)]
        posts.sort(key=lambda p: p.created_at or "", reverse=True)
        return posts[:max_results]

    def get_followings(self, username: str, max_results: int = 20) -> list[Account]:
        source = self.get_user_by_username(username)
        if source is None:
            return []
        values = [a for a in self._by_id.values() if a.id != source.id]
        start = int(source.id) % max(1, len(values)) if values else 0
        return (values[start:] + values[:start])[:max_results]

    def get_verified_followers(
        self, user_id: str, max_results: int = 20, *, username: str | None = None
    ) -> list[Account]:
        values = [a for a in self._by_id.values() if a.id != str(user_id) and a.verified]
        return values[:max_results]
