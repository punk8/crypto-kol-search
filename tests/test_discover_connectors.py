from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from kol_search.backend.connectors.x import XConnector
from kol_search.backend.connectors.registry import ConnectorRegistry
from kol_search.backend.app import app as backend_app
from kol_search.backend.kol_scoring import score_x_account
from kol_search.backend.models import (
    AccountContentQuery,
    AccountSearchQuery,
    ContentSearchQuery,
    TrendQuery,
)
from kol_search.backend.provider_state import ProviderStateStore
from kol_search.discover_gateway import DiscoverGateway, DiscoverGatewayError
from kol_search.settings import Settings
from kol_search.twitter.base import TwitterBackendError
from kol_search.twitter.fxembed import FxEmbedTwitterClient
from kol_search.twitter.getxapi import GetXApiClient
from kol_search.twitter.mock import MockTwitterClient
from kol_search.twitter.models import XAccount, XReadCapabilities, XTrend, XTweet
from kol_search.twitter.twscrape_backend import TwscrapeTwitterClient
from kol_search.web import app


def _status(post_id: str = "42") -> dict:
    return {
        "type": "status",
        "id": post_id,
        "text": "A useful AI release",
        "created_at": "Sun Jul 26 09:00:00 +0000 2026",
        "likes": 120,
        "replies": 9,
        "retweets": 24,
        "views": 5000,
        "author": {
            "id": "7",
            "screen_name": "alice",
            "name": "Alice",
            "followers": 1000,
            "website": {"url": "https://example.com", "display_url": "example.com"},
            "avatar_url": "https://example.com/avatar.jpg",
        },
    }


def test_fxembed_adapter_normalizes_search_timeline_lookup_and_trends() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/2/search":
            return httpx.Response(200, json={"code": 200, "results": [_status()]})
        if request.url.path == "/2/profile/alice":
            return httpx.Response(200, json={"code": 200, "profile": _status()["author"]})
        if request.url.path == "/2/profile/alice/statuses":
            return httpx.Response(200, json={"code": 200, "statuses": [_status("43")]})
        if request.url.path == "/2/status/42":
            return httpx.Response(200, json={"code": 200, "status": _status()})
        if request.url.path == "/2/trends":
            return httpx.Response(
                200,
                json={
                    "code": 200,
                    "trends": [{"name": "OpenAI", "rank": 1, "tweet_count": 50000}],
                },
            )
        raise AssertionError(f"unexpected FxEmbed request: {request.url}")

    client = FxEmbedTwitterClient()
    client._client.close()  # noqa: SLF001
    client._client = httpx.Client(  # noqa: SLF001
        base_url="https://api.fxtwitter.com",
        transport=httpx.MockTransport(handler),
    )
    try:
        posts = client.search_tweets("AI", 5)
        account = client.get_user_by_username("alice")
        timeline = client.get_user_tweets("7", 5, username="alice")
        post = client.get_tweet("42")
        trends = client.get_trends(5, category="news", locale="worldwide")
    finally:
        client.close()

    assert posts[0].source_provider == "fxembed"
    assert posts[0].author_username == "alice"
    assert posts[0].like_count == 120
    assert posts[0].view_count == 5000
    assert account is not None and account.username == "alice"
    assert account.url == "https://example.com"
    assert timeline[0].id == "43"
    assert post is not None and post.id == "42"
    assert trends[0].name == "OpenAI"
    assert [request.url.path for request in requests] == [
        "/2/search",
        "/2/profile/alice",
        "/2/profile/alice/statuses",
        "/2/status/42",
        "/2/trends",
    ]


def test_getxapi_adapter_normalizes_discovery_content_and_graph_reads() -> None:
    requests: list[httpx.Request] = []

    def user(username: str = "alice", user_id: str = "7") -> dict:
        return {
            "id": user_id,
            "userName": username,
            "name": username.title(),
            "description": "AI agent researcher",
            "followers": 12_000,
            "following": 300,
            "tweets": 900,
            "listed": 42,
            "isBlueVerified": True,
        }

    def tweet(post_id: str = "42") -> dict:
        return {
            "id": post_id,
            "text": "A useful AI release",
            "createdAt": "Sun Jul 26 09:00:00 +0000 2026",
            "likeCount": 120,
            "replyCount": 9,
            "retweetCount": 24,
            "viewCount": 5000,
            "author": user(),
        }

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["authorization"] == "Bearer secret"
        path = request.url.path
        if path == "/twitter/user/search":
            return httpx.Response(200, json={"users": [user()], "has_more": False})
        if path == "/twitter/tweet/advanced_search":
            return httpx.Response(200, json={"tweets": [tweet()], "has_more": False})
        if path == "/twitter/user/info":
            return httpx.Response(200, json={"status": "success", "data": user()})
        if path == "/twitter/user/tweets":
            return httpx.Response(200, json={"tweets": [tweet("43")], "has_more": False})
        if path == "/twitter/tweet/detail":
            return httpx.Response(200, json={"status": "success", "data": tweet()})
        if path == "/twitter/trends":
            return httpx.Response(
                200,
                json={
                    "trends": [
                        {
                            "name": "OpenAI",
                            "rank": 1,
                            "tweet_volume": 50_000,
                            "search_url": "https://x.com/search?q=OpenAI",
                        }
                    ]
                },
            )
        if path == "/twitter/user/following":
            return httpx.Response(200, json={"following": [user("bob", "8")]})
        if path == "/twitter/user/verified_followers":
            return httpx.Response(200, json={"verified_followers": [user("carol", "9")]})
        raise AssertionError(f"unexpected GetXAPI request: {request.url}")

    client = GetXApiClient("secret")
    client._client.close()  # noqa: SLF001
    client._client = httpx.Client(  # noqa: SLF001
        base_url="https://api.getxapi.com",
        headers={"Authorization": "Bearer secret"},
        transport=httpx.MockTransport(handler),
    )
    try:
        users = client.search_users("AI agents", 5)
        posts = client.search_tweets("AI agents", 5)
        profile = client.get_user_by_username("alice")
        timeline = client.get_user_tweets("7", 5, username="alice")
        detail = client.get_tweet("42")
        trends = client.get_trends(5)
        following = client.get_followings("alice", 5)
        verified = client.get_verified_followers("7", 5, username="alice")
    finally:
        client.close()

    assert users[0].source_provider == "getxapi"
    assert users[0].listed_count == 42
    assert posts[0].like_count == 120 and posts[0].view_count == 5000
    assert profile is not None and profile.username == "alice"
    assert timeline[0].id == "43"
    assert detail is not None and detail.id == "42"
    assert trends[0].name == "OpenAI" and trends[0].post_count == 50_000
    assert following[0].username == "bob"
    assert verified[0].username == "carol"
    assert requests[0].url.params["q"] == "AI agents"
    assert requests[1].url.params["product"] == "Latest"


def test_getxapi_enforces_daily_budget_and_caches_balance_check(tmp_path) -> None:
    account_checks = 0
    trend_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal account_checks, trend_calls
        if request.url.path == "/account/me":
            account_checks += 1
            return httpx.Response(
                200,
                json={
                    "credits_remaining": 1.0,
                    "credits_used": 0.1,
                    "total_requests": 100,
                },
            )
        if request.url.path == "/twitter/trends":
            trend_calls += 1
            return httpx.Response(
                200,
                json={"trends": [{"name": "OpenAI", "rank": 1}]},
            )
        raise AssertionError(request.url)

    state = ProviderStateStore(tmp_path / "budget.db")
    client = GetXApiClient(
        "secret",
        state_store=state,
        daily_call_limit=2,
        min_credits=0.01,
        balance_cache_seconds=300,
    )
    client._client.close()  # noqa: SLF001
    client._client = httpx.Client(  # noqa: SLF001
        base_url="https://api.getxapi.com",
        transport=httpx.MockTransport(handler),
    )
    try:
        assert client.get_trends(1)
        assert client.get_trends(1)
        with pytest.raises(TwitterBackendError, match="daily call budget exhausted"):
            client.get_trends(1)
        usage = client.usage_status()
    finally:
        client.close()
        state.close()

    assert account_checks == 1
    assert trend_calls == 2
    assert usage["state"] == "budget_exhausted"
    assert usage["requests_today"] == 2
    assert usage["credits_remaining"] == pytest.approx(0.998)


def test_getxapi_stops_before_billable_call_when_balance_is_low(tmp_path) -> None:
    billable_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal billable_calls
        if request.url.path == "/account/me":
            return httpx.Response(
                200,
                json={"credits_remaining": 0.005, "total_requests": 10},
            )
        billable_calls += 1
        return httpx.Response(200, json={"trends": []})

    state = ProviderStateStore(tmp_path / "low-balance.db")
    client = GetXApiClient(
        "secret",
        state_store=state,
        daily_call_limit=100,
        min_credits=0.01,
    )
    client._client.close()  # noqa: SLF001
    client._client = httpx.Client(  # noqa: SLF001
        base_url="https://api.getxapi.com",
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(TwitterBackendError, match="minimum credit reserve"):
            client.get_trends(1)
        usage = client.usage_status()
    finally:
        client.close()
        state.close()

    assert billable_calls == 0
    assert usage["state"] == "low_balance"
    assert usage["requests_today"] == 0


def test_getxapi_projects_balance_between_cached_account_checks(tmp_path) -> None:
    account_checks = 0
    billable_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal account_checks, billable_calls
        if request.url.path == "/account/me":
            account_checks += 1
            return httpx.Response(200, json={"credits_remaining": 0.015})
        billable_calls += 1
        return httpx.Response(200, json={"trends": []})

    state = ProviderStateStore(tmp_path / "projected-balance.db")
    client = GetXApiClient(
        "secret",
        state_store=state,
        daily_call_limit=100,
        min_credits=0.01,
        balance_cache_seconds=300,
        estimated_credits_per_call=0.001,
    )
    client._client.close()  # noqa: SLF001
    client._client = httpx.Client(  # noqa: SLF001
        base_url="https://api.getxapi.com",
        transport=httpx.MockTransport(handler),
    )
    try:
        for _ in range(4):
            client.get_trends(1)
        with pytest.raises(TwitterBackendError, match="minimum credit reserve"):
            client.get_trends(1)
        usage = client.usage_status()
    finally:
        client.close()
        state.close()

    assert account_checks == 1
    assert billable_calls == 4
    assert usage["state"] == "low_balance"
    assert usage["requests_today"] == 4
    assert usage["credits_remaining"] == pytest.approx(0.011)


def test_x_connector_falls_back_without_changing_frontend_schema(monkeypatch) -> None:
    class FailingProvider:
        name = "primary"
        capabilities = XReadCapabilities(post_search=True)

        def search_tweets(self, *args, **kwargs):  # noqa: ANN002, ANN003
            raise TwitterBackendError("temporary upstream failure")

    settings = Settings(
        _env_file=None,
        KOL_X_PROVIDER_CHAIN="twscrape,fxembed",
    )
    connector = XConnector(settings)
    connector.provider_names = ("primary", "fallback")
    providers = {"primary": FailingProvider(), "fallback": MockTwitterClient()}
    monkeypatch.setattr(connector, "_provider", lambda name: providers[name])

    result = connector.search_content(
        ContentSearchQuery(query="DeFi", sort="top", limit=5)
    )

    assert result.provider == "mock"
    assert result.attempted_providers == ("primary", "fallback")
    assert result.items and result.items[0].platform == "x"
    assert "primary failed" in result.warnings[0]


def test_x_connector_uses_free_provider_without_touching_paid_fallback(
    monkeypatch, tmp_path
) -> None:
    paid_calls = 0

    class FreeProvider:
        name = "fxembed"
        capabilities = XReadCapabilities(trends=True)

        def get_trends(self, *args, **kwargs):  # noqa: ANN002, ANN003
            return [XTrend(name="OpenAI", rank=1)]

    class PaidProvider:
        name = "getxapi"
        capabilities = XReadCapabilities(trends=True)

        def get_trends(self, *args, **kwargs):  # noqa: ANN002, ANN003
            nonlocal paid_calls
            paid_calls += 1
            return [XTrend(name="Paid result", rank=1)]

    settings = Settings(
        _env_file=None,
        KOL_DB_PATH=str(tmp_path / "free-first.db"),
        KOL_X_PROVIDER_CHAIN="fxembed,getxapi",
    )
    connector = XConnector(settings)
    providers = {"fxembed": FreeProvider(), "getxapi": PaidProvider()}
    monkeypatch.setattr(connector, "_provider", lambda name: providers[name])
    try:
        result = connector.get_trends(TrendQuery(limit=1))
    finally:
        connector.close()

    assert result.provider == "fxembed"
    assert result.attempted_providers == ("fxembed",)
    assert paid_calls == 0


def test_x_trends_use_paid_fallback_only_when_free_source_is_empty(
    monkeypatch, tmp_path
) -> None:
    paid_calls = 0

    class EmptyFreeProvider:
        name = "fxembed"
        capabilities = XReadCapabilities(trends=True)

        def get_trends(self, *args, **kwargs):  # noqa: ANN002, ANN003
            return []

    class PaidProvider:
        name = "getxapi"
        capabilities = XReadCapabilities(trends=True)

        def get_trends(self, *args, **kwargs):  # noqa: ANN002, ANN003
            nonlocal paid_calls
            paid_calls += 1
            return [XTrend(name="Paid result", rank=1)]

    settings = Settings(
        _env_file=None,
        KOL_DB_PATH=str(tmp_path / "empty-free-trends.db"),
        KOL_X_PROVIDER_CHAIN="fxembed,getxapi",
    )
    connector = XConnector(settings)
    providers = {"fxembed": EmptyFreeProvider(), "getxapi": PaidProvider()}
    monkeypatch.setattr(connector, "_provider", lambda name: providers[name])
    try:
        result = connector.get_trends(TrendQuery(limit=1))
    finally:
        connector.close()

    assert result.provider == "getxapi"
    assert result.attempted_providers == ("fxembed", "getxapi")
    assert paid_calls == 1
    assert "fxembed returned no native_trends items" in result.warnings


def test_x_trend_categories_share_one_provider_snapshot(monkeypatch, tmp_path) -> None:
    calls = 0

    class CountingProvider:
        name = "getxapi"
        capabilities = XReadCapabilities(trends=True)

        def get_trends(self, *args, **kwargs):  # noqa: ANN002, ANN003
            nonlocal calls
            calls += 1
            return [XTrend(name="OpenAI", rank=1)]

    settings = Settings(
        _env_file=None,
        KOL_DB_PATH=str(tmp_path / "shared-trends.db"),
        KOL_X_PROVIDER_CHAIN="getxapi",
        KOL_X_TREND_CACHE_SECONDS=1800,
    )
    connector = XConnector(settings)
    provider = CountingProvider()
    monkeypatch.setattr(connector, "_provider", lambda name: provider)
    try:
        ai = connector.get_trends(TrendQuery(category="ai", locale="global", limit=10))
        crypto = connector.get_trends(
            TrendQuery(category="crypto", locale="global", limit=10)
        )
    finally:
        connector.close()

    assert calls == 1
    assert ai.items[0].category == "ai"
    assert crypto.items[0].category == "crypto"
    assert "served from backend snapshot cache" in crypto.warnings


def test_x_account_discovery_caches_identical_internal_requests(
    monkeypatch, tmp_path
) -> None:
    class CountingProvider(MockTwitterClient):
        search_calls = 0

        def search_tweets(self, *args, **kwargs):  # noqa: ANN002, ANN003
            self.search_calls += 1
            return super().search_tweets(*args, **kwargs)

    settings = Settings(
        _env_file=None,
        KOL_DB_PATH=str(tmp_path / "account-cache.db"),
        KOL_X_PROVIDER_CHAIN="mock",
        KOL_ENABLE_MOCK_BACKEND=True,
        KOL_X_KOL_DISCOVERY_CACHE_SECONDS=300,
    )
    connector = XConnector(settings)
    provider = CountingProvider()
    monkeypatch.setattr(connector, "_provider", lambda name: provider)
    query = AccountSearchQuery(query="DeFi", limit=3, min_engagement=0)

    first = connector.search_accounts(query)
    second = connector.search_accounts(query)

    assert first.items == second.items
    assert first.items
    assert "served from backend snapshot cache" in second.warnings
    assert provider.search_calls == 1


def test_x_content_snapshot_survives_connector_restart(monkeypatch, tmp_path) -> None:
    class CountingProvider(MockTwitterClient):
        search_calls = 0

        def search_tweets(self, *args, **kwargs):  # noqa: ANN002, ANN003
            self.search_calls += 1
            return super().search_tweets(*args, **kwargs)

    settings = Settings(
        _env_file=None,
        KOL_DB_PATH=str(tmp_path / "persistent-cache.db"),
        KOL_X_PROVIDER_CHAIN="mock",
        KOL_ENABLE_MOCK_BACKEND=True,
        KOL_X_CONTENT_CACHE_SECONDS=300,
    )
    query = ContentSearchQuery(query="DeFi", sort="top", limit=5)
    first_connector = XConnector(settings)
    first_provider = CountingProvider()
    monkeypatch.setattr(first_connector, "_provider", lambda name: first_provider)
    first = first_connector.search_content(query)
    first_connector.close()

    second_connector = XConnector(settings)

    def unexpected_provider(name):  # noqa: ANN001, ANN202
        raise AssertionError(f"provider should not be called: {name}")

    monkeypatch.setattr(second_connector, "_provider", unexpected_provider)
    try:
        second = second_connector.search_content(query)
    finally:
        second_connector.close()

    assert first.items == second.items
    assert first_provider.search_calls == 1
    assert "served from backend snapshot cache" in second.warnings


def test_x_kol_score_caps_sparse_evidence_and_uses_public_signals() -> None:
    observed_at = datetime(2026, 7, 26, tzinfo=timezone.utc)
    account = XAccount(
        id="7",
        username="alice",
        name="Alice AI",
        description="AI agents researcher",
        followers_count=2_000_000,
        listed_count=10_000,
    )
    post = XTweet(
        id="42",
        author_id="7",
        author_username="alice",
        text="A practical AI agents evaluation framework",
        created_at="Sun Jul 26 09:00:00 +0000 2026",
        like_count=10_000,
        retweet_count=2_000,
    )

    sparse = score_x_account(
        account,
        query="AI agents",
        domain_posts=[post],
        recent_posts=[post],
        observed_at=observed_at,
    )
    supported = score_x_account(
        account,
        query="AI agents",
        domain_posts=[
            post,
            post.model_copy(update={"id": "43"}),
            post.model_copy(update={"id": "44"}),
        ],
        recent_posts=[
            post,
            post.model_copy(update={"id": "43"}),
            post.model_copy(update={"id": "44"}),
        ],
        observed_at=observed_at,
    )

    assert sparse.score <= 64
    assert sparse.confidence == "Medium"
    assert supported.score > sparse.score
    assert supported.confidence == "High"
    assert len(supported.components) == 5


def test_x_kol_score_does_not_match_ai_inside_unrelated_words() -> None:
    from kol_search.backend.kol_scoring import domain_terms, text_relevance

    terms = domain_terms("AI agents")

    assert text_relevance("Maritime research and autonomous shipping", terms) == 0
    assert text_relevance("Practical AI-agent research", terms) == 1


def test_twscrape_uses_cookie_session_without_password_login(monkeypatch, tmp_path) -> None:
    class FakePool:
        def __init__(self) -> None:
            self.cookie_value = None

        async def get_account(self, username: str):  # noqa: ANN202
            return None

        async def add_account_cookies(self, username: str, cookies: str) -> None:
            self.cookie_value = (username, cookies)

        async def get_all(self) -> list[object]:
            return [object()]

    class FakeApi:
        latest = None

        def __init__(self, pool, raise_when_no_account=False):  # noqa: ANN001, FBT002
            self.pool = FakePool()
            FakeApi.latest = self

        async def search(self, query: str, limit: int):  # noqa: ANN202
            yield SimpleNamespace(
                id=42,
                user=SimpleNamespace(id=7, username="alice"),
                rawContent="hello",
                likeCount=3,
            )

        async def user_by_login(self, username: str):  # noqa: ANN202
            return SimpleNamespace(id=1, username=username)

        async def tweet_details(self, tweet_id: int):  # noqa: ANN202
            return SimpleNamespace(
                id=tweet_id,
                user=SimpleNamespace(id=7, username="alice"),
                rawContent="hello",
                likeCount=3,
            )

    import twscrape

    monkeypatch.setattr(twscrape, "API", FakeApi)
    client = TwscrapeTwitterClient(
        accounts_db=str(tmp_path / "accounts.db"),
        username="test-account",
        auth_token="test-auth-token",
        ct0="test-csrf-token",
    )

    posts = client.search_tweets("AI", 1)
    post = client.get_tweet("42")

    assert posts[0].source_provider == "twscrape"
    assert post is not None and post.author_username == "alice"
    assert FakeApi.latest.pool.cookie_value == (
        "test-account",
        "auth_token=test-auth-token; ct0=test-csrf-token",
    )


def test_twscrape_empty_health_probe_is_cached_as_unavailable(
    monkeypatch, tmp_path
) -> None:
    class FakePool:
        async def get_account(self, username: str):  # noqa: ANN202
            return None

        async def add_account_cookies(self, username: str, cookies: str) -> None:
            return None

        async def get_all(self) -> list[object]:
            return [object()]

    class EmptyProbeApi:
        probe_calls = 0

        def __init__(self, pool, raise_when_no_account=False):  # noqa: ANN001, FBT002
            self.pool = FakePool()

        async def user_by_login(self, username: str):  # noqa: ANN202
            return SimpleNamespace(id=1, username=username)

        async def search(self, query: str, limit: int):  # noqa: ANN202
            if query == "from:X":
                EmptyProbeApi.probe_calls += 1
            if False:
                yield None

    import twscrape

    monkeypatch.setattr(twscrape, "API", EmptyProbeApi)
    client = TwscrapeTwitterClient(
        accounts_db=str(tmp_path / "accounts.db"),
        auth_token="test-auth-token",
        ct0="test-csrf-token",
    )

    with pytest.raises(TwitterBackendError, match="probe returned no data"):
        client.search_tweets("AI", 1)
    with pytest.raises(TwitterBackendError, match="probe returned no data"):
        client.search_tweets("AI", 1)

    assert EmptyProbeApi.probe_calls == 1


def test_discover_api_exposes_versioned_platform_contract(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("KOL_DB_PATH", str(tmp_path / "discover-api.db"))
    monkeypatch.setenv("KOL_ENABLE_SIGNAL_SCAN", "false")
    monkeypatch.setenv("KOL_X_PROVIDER_CHAIN", "mock")
    monkeypatch.setenv("KOL_ENABLE_MOCK_BACKEND", "true")
    monkeypatch.setenv("DEEP_SEEK_API_KEY", "")

    with TestClient(app) as client:
        platforms = client.get("/api/discover/v1/platforms")
        domains = client.get("/api/discover/v1/domains")
        usage = client.get("/api/discover/v1/platforms/x/providers/usage")
        trends = client.get("/api/discover/v1/platforms/x/trends?limit=2")
        search = client.get(
            "/api/discover/v1/platforms/x/content/search?q=DeFi&sort=top&limit=5"
        )
        accounts = client.get(
            "/api/discover/v1/platforms/x/accounts/search"
            "?q=DeFi&limit=5&sample_size=20&recent_posts_per_account=3"
            "&min_engagement=0"
        )
        tracked = client.post(
            "/api/discover/v1/platforms/x/accounts/"
            f"{accounts.json()['items'][0]['native_id']}/tracking",
            json={"enabled": True},
        )
        active_accounts = client.get(
            "/api/discover/v1/platforms/x/accounts/tracking?status=active"
        )
        timeline = client.get(
            "/api/discover/v1/platforms/x/accounts/10/content?limit=5"
        )
        lookup = client.get("/api/discover/v1/platforms/x/content/t100")
        hot_content = client.get(
            "/api/discover/v1/platforms/x/hot-content?source=discover&limit=5"
        )
        reply_unconfigured = client.post(
            "/api/discover/v1/platforms/x/content/t100/reply-draft",
            json={"tone": "thoughtful", "language": "auto"},
        )
        unsupported = client.get("/api/discover/v1/platforms/youtube/trends")

    assert platforms.status_code == 200
    assert platforms.json()["items"][0]["platform"] == "x"
    assert domains.status_code == 200 and domains.json()["total"] == 3
    assert usage.status_code == 200 and usage.json()["items"] == []
    assert trends.status_code == 200 and trends.json()["total"] == 2
    assert search.status_code == 200 and search.json()["meta"]["provider"] == "mock"
    assert accounts.status_code == 200 and accounts.json()["total"] > 0
    assert accounts.json()["items"][0]["native_id"]
    assert len(accounts.json()["items"][0]["score_components"]) == 5
    assert accounts.json()["items"][0]["score_version"] == "x-kol-internal-v1"
    assert tracked.status_code == 200
    assert tracked.json()["item"]["status"] == "active"
    assert active_accounts.status_code == 200
    assert active_accounts.json()["items"][0]["username"]
    assert timeline.status_code == 200 and timeline.json()["items"][0]["author"]["native_id"] == "10"
    assert lookup.status_code == 200 and lookup.json()["items"][0]["native_id"] == "t100"
    assert hot_content.status_code == 200
    assert hot_content.json()["meta"]["provider"] == "backend_repository"
    assert reply_unconfigured.status_code == 503
    assert unsupported.status_code == 404

    with sqlite3.connect(tmp_path / "discover-api.db") as connection:
        snapshot_count = connection.execute(
            "SELECT COUNT(*) FROM x_kol_domain_score_snapshots"
        ).fetchone()[0]
        response_snapshot_count = connection.execute(
            "SELECT COUNT(*) FROM discover_response_snapshots"
        ).fetchone()[0]
    assert snapshot_count > 0
    assert response_snapshot_count >= 4


def test_discover_api_requires_service_token_when_externally_bound(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("KOL_DB_PATH", str(tmp_path / "discover-auth.db"))
    monkeypatch.setenv("KOL_ENABLE_SIGNAL_SCAN", "false")
    monkeypatch.setenv("KOL_X_PROVIDER_CHAIN", "mock")
    monkeypatch.setenv("KOL_ENABLE_MOCK_BACKEND", "true")
    monkeypatch.setenv("KOL_WEB_HOST", "0.0.0.0")
    monkeypatch.setenv("KOL_ADMIN_PASSWORD", "test-password")
    monkeypatch.setenv("KOL_SESSION_SECRET", "test-session-secret")
    monkeypatch.setenv("KOL_BACKEND_API_TOKEN", "test-service-token")

    with TestClient(app) as client:
        denied = client.get("/api/discover/v1/platforms")
        accepted = client.get(
            "/api/discover/v1/platforms",
            headers={"Authorization": "Bearer test-service-token"},
        )

    assert denied.status_code == 401
    assert accepted.status_code == 200


def test_standalone_backend_app_has_no_dashboard_dependency(monkeypatch) -> None:
    monkeypatch.setenv("KOL_BACKEND_HOST", "127.0.0.1")
    monkeypatch.setenv("KOL_X_PROVIDER_CHAIN", "mock")
    monkeypatch.setenv("KOL_ENABLE_MOCK_BACKEND", "true")

    with TestClient(backend_app) as client:
        health = client.get("/health")
        platforms = client.get("/api/discover/v1/platforms")
        dashboard = client.get("/hot-content")

    assert health.json()["service"] == "trend-kol-discover-backend"
    assert platforms.status_code == 200
    assert dashboard.status_code == 404


def test_frontend_gateway_uses_authenticated_versioned_backend_contract() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "meta": {
                    "platform": "x",
                    "provider": "fxembed",
                    "attempted_providers": ["twscrape", "fxembed"],
                    "observed_at": "2026-07-26T10:00:00Z",
                    "warnings": [],
                },
                "items": [
                    {
                        "platform": "x",
                        "native_id": "42",
                        "provider": "fxembed",
                        "author": {
                            "native_id": "7",
                            "username": "alice",
                            "display_name": "Alice",
                            "profile_url": "https://x.com/alice",
                        },
                        "text": "A live X post",
                        "canonical_url": "https://x.com/alice/status/42",
                        "published_at": "2026-07-26T09:00:00Z",
                        "observed_at": "2026-07-26T10:00:00Z",
                        "metrics": {"likes": 120, "views": 5000},
                    }
                ],
                "total": 1,
            },
        )

    settings = Settings(
        _env_file=None,
        KOL_BACKEND_URL="https://discover-backend.example",
        KOL_BACKEND_API_TOKEN="service-secret",
    )
    gateway = DiscoverGateway(settings, ConnectorRegistry())
    assert gateway._client is not None  # noqa: SLF001
    gateway._client.close()  # noqa: SLF001
    gateway._client = httpx.Client(  # noqa: SLF001
        base_url="https://discover-backend.example",
        headers={"Authorization": "Bearer service-secret"},
        transport=httpx.MockTransport(handler),
    )
    try:
        response = gateway.account_content(
            "x",
            AccountContentQuery(account_ref="alice/team", limit=3),
        )
    finally:
        gateway.close()

    assert response.items[0].native_id == "42"
    assert response.items[0].metrics.views == 5000
    assert requests[0].headers["authorization"] == "Bearer service-secret"
    assert requests[0].url.path == (
        "/api/discover/v1/platforms/x/accounts/alice/team/content"
    )
    assert requests[0].url.params["limit"] == "3"


def test_frontend_gateway_supports_remote_account_discovery_and_tracking() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        meta = {
            "platform": "x",
            "provider": "fxembed",
            "attempted_providers": ["twscrape", "fxembed"],
            "observed_at": "2026-07-26T10:00:00Z",
            "warnings": [],
        }
        if request.method == "POST":
            return httpx.Response(
                200,
                json={
                    "meta": {**meta, "provider": "backend_repository"},
                    "item": {
                        "platform": "x",
                        "native_id": "7",
                        "status": "active",
                        "updated_at": "2026-07-26T10:01:00Z",
                    },
                },
            )
        if request.url.path.endswith("/accounts/tracking"):
            return httpx.Response(
                200,
                json={
                    "meta": {**meta, "provider": "backend_repository"},
                    "items": [
                        {
                            "platform": "x",
                            "native_id": "7",
                            "username": "alice",
                            "display_name": "Alice",
                            "profile_url": "https://x.com/alice",
                            "status": "active",
                            "score": 0.72,
                        }
                    ],
                    "total": 1,
                },
            )
        return httpx.Response(
            200,
            json={
                "meta": meta,
                "items": [
                    {
                        "platform": "x",
                        "native_id": "7",
                        "provider": "fxembed",
                        "username": "alice",
                        "display_name": "Alice",
                        "profile_url": "https://x.com/alice",
                        "followers_count": 1000,
                        "score": 72,
                        "confidence": "High",
                        "score_version": "x-kol-internal-v1",
                        "score_components": [],
                        "evidence": [],
                        "recent_content": [],
                        "observed_at": "2026-07-26T10:00:00Z",
                    }
                ],
                "total": 1,
            },
        )

    settings = Settings(
        _env_file=None,
        KOL_BACKEND_URL="https://discover-backend.example",
        KOL_BACKEND_API_TOKEN="service-secret",
    )
    gateway = DiscoverGateway(settings, ConnectorRegistry())
    assert gateway._client is not None  # noqa: SLF001
    gateway._client.close()  # noqa: SLF001
    gateway._client = httpx.Client(  # noqa: SLF001
        base_url="https://discover-backend.example",
        headers={"Authorization": "Bearer service-secret"},
        transport=httpx.MockTransport(handler),
    )
    try:
        accounts = gateway.search_accounts(
            "x", AccountSearchQuery(query="AI agents", limit=5)
        )
        tracked = gateway.set_account_tracking("x", "7", enabled=True)
        active = gateway.tracked_accounts("x", status="active", limit=50)
    finally:
        gateway.close()

    assert accounts.items[0].username == "alice"
    assert tracked.item.status == "active"
    assert active.items[0].username == "alice"
    assert requests[0].url.path.endswith("/platforms/x/accounts/search")
    assert requests[0].url.params["q"] == "AI agents"
    assert requests[1].url.path.endswith("/platforms/x/accounts/7/tracking")
    assert requests[1].read() == b'{"enabled":true}'
    assert requests[2].url.path.endswith("/platforms/x/accounts/tracking")
    assert requests[2].url.params["status"] == "active"
    assert all(request.headers["authorization"] == "Bearer service-secret" for request in requests)


def test_split_frontend_is_stateless_and_hides_backend_api(monkeypatch, tmp_path) -> None:
    database_path = tmp_path / "should-not-exist.db"
    monkeypatch.setenv("KOL_DB_PATH", str(database_path))
    monkeypatch.setenv("KOL_BACKEND_URL", "https://discover-backend.example")
    monkeypatch.setenv("KOL_BACKEND_API_TOKEN", "service-secret")
    monkeypatch.setenv("KOL_WEB_HOST", "0.0.0.0")
    monkeypatch.setenv("KOL_ADMIN_USERNAME", "admin")
    monkeypatch.setenv("KOL_ADMIN_PASSWORD", "admin-secret")
    monkeypatch.setenv("KOL_SESSION_SECRET", "session-secret")

    with TestClient(app) as client:
        health = client.get("/health")
        hidden_api = client.get("/api/discover/v1/platforms")
        data_sources = client.get("/platforms", follow_redirects=False)
        protected = client.get("/trends", follow_redirects=False)

    assert health.json()["mode"] == "remote-backend"
    assert hidden_api.status_code == 404
    assert data_sources.status_code == 303
    assert protected.headers["location"] == "/login"
    assert not database_path.exists()


def test_frontend_gateway_sanitizes_remote_failures() -> None:
    settings = Settings(
        _env_file=None,
        KOL_BACKEND_URL="https://discover-backend.example",
    )
    gateway = DiscoverGateway(settings, ConnectorRegistry())
    assert gateway._client is not None  # noqa: SLF001
    gateway._client.close()  # noqa: SLF001
    gateway._client = httpx.Client(  # noqa: SLF001
        base_url="https://discover-backend.example",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                502,
                text="upstream secret details",
            )
        ),
    )
    try:
        with pytest.raises(DiscoverGatewayError, match="HTTP 502") as failure:
            gateway.search_content("x", ContentSearchQuery(query="AI"))
    finally:
        gateway.close()

    assert "secret details" not in str(failure.value)
