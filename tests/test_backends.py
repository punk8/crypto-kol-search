import subprocess
from unittest.mock import patch

import httpx
import pytest

from kol_search.settings import Settings
from kol_search.twitter.base import TwitterBackendError
from kol_search.twitter.factory import create_x_read_provider
from kol_search.twitter.mock import MockTwitterClient
from kol_search.twitter.official import API_BASE, OfficialTwitterClient, _parse_account, _parse_post
from kol_search.twitter.third_party import ThirdPartyTwitterClient
from kol_search.twitter.twitterapi_io import TwitterApiIoClient


def test_official_parser_preserves_entities_and_metrics():
    account = _parse_account({
        "id": "1",
        "username": "alice",
        "name": "Alice",
        "description": "Researcher",
        "location": "Hong Kong",
        "protected": False,
        "entities": {"url": {"urls": [{"expanded_url": "https://example.org"}]}},
        "public_metrics": {"followers_count": 123, "following_count": 4, "tweet_count": 5, "listed_count": 6},
    })
    assert account.followers_count == 123
    assert account.source_provider == "official"
    assert account.listed_count == 6
    assert account.entities["url"]["urls"][0]["expanded_url"] == "https://example.org"

    post = _parse_post({
        "id": "p1",
        "author_id": "1",
        "text": "hello @bob",
        "conversation_id": "c1",
        "in_reply_to_user_id": "2",
        "referenced_tweets": [{"type": "replied_to", "id": "p0"}],
        "entities": {"mentions": [{"username": "bob"}]},
        "public_metrics": {
            "like_count": 2,
            "retweet_count": 3,
            "impression_count": 100,
            "bookmark_count": 4,
        },
    }, {"1": account})
    assert post.author_username == "alice"
    assert post.mentioned_usernames == ["bob"]
    assert post.engagement == 5
    assert post.url == "https://x.com/alice/status/p1"
    assert post.conversation_id == "c1"
    assert post.in_reply_to_user_id == "2"
    assert post.referenced_post_id == "p0"
    assert post.reference_type == "replied_to"
    assert post.view_count == 100
    assert post.bookmark_count == 4
    assert post.source_provider == "official"


def test_official_client_reads_trends_and_applies_incremental_time_filters():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/2/trends/by/woeid/23424977":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"trend_name": "#AI", "tweet_count": 250000},
                        {"trend_name": "Bitcoin", "tweet_count": 180000},
                    ]
                },
            )
        if request.url.path == "/2/tweets/search/recent":
            return httpx.Response(200, json={"data": []})
        if request.url.path == "/2/users/123/tweets":
            return httpx.Response(200, json={"data": []})
        return httpx.Response(404, json={"detail": "unexpected path"})

    client = OfficialTwitterClient("test-token", trend_woeid=23424977)
    client._client.close()
    client._client = httpx.Client(  # noqa: SLF001
        base_url=API_BASE,
        transport=httpx.MockTransport(handler),
    )
    try:
        trends = client.get_trends(2)
        client.search_tweets(
            "#AI",
            max_results=10,
            start_time="2026-07-22T10:00:00Z",
        )
        client.get_user_tweets(
            "123",
            max_results=5,
            username="alice",
            start_time="2026-07-22T11:00:00Z",
        )
    finally:
        client.close()

    assert client.capabilities.trends is True
    assert [(trend.name, trend.rank, trend.post_count) for trend in trends] == [
        ("#AI", 1, 250000),
        ("Bitcoin", 2, 180000),
    ]
    assert trends[0].raw["source_provider"] == "official"
    assert requests[0].url.params["max_trends"] == "2"
    assert requests[1].url.params["start_time"] == "2026-07-22T10:00:00Z"
    assert requests[2].url.params["start_time"] == "2026-07-22T11:00:00Z"


def test_third_party_user_search_is_explicit_capability():
    client = ThirdPartyTwitterClient(
        base_url="https://api.example.org",
        api_key="test",
        supports_user_search=False,
    )
    try:
        assert client.capabilities.user_search is False
        assert client.search_users("defi") == []
    finally:
        client.close()


def test_twitterapi_io_key_alias_and_readiness():
    settings = Settings(_env_file=None, API_KEY="test-key", TWITTER_BACKEND="twitterapi_io")
    assert settings.twitterapi_io_api_key == "test-key"
    assert settings.backend_ready("twitterapi_io") == (True, None)


def test_getxapi_key_and_provider_chain_readiness():
    settings = Settings(
        _env_file=None,
        GET_X_API_KEY="test-key",
        KOL_X_PROVIDER_CHAIN="getxapi,fxembed",
    )
    assert settings.get_x_api_key == "test-key"
    assert settings.backend_ready("getxapi") == (True, None)
    assert settings.x_provider_names() == ("getxapi", "fxembed")


def test_default_x_chain_and_budget_are_starter_safe():
    settings = Settings(_env_file=None)

    assert settings.x_provider_names() == ("fxembed", "getxapi")
    assert settings.get_x_api_daily_call_limit == 120
    assert settings.get_x_api_min_credits == 0.05
    assert settings.x_content_cache_seconds == 1800
    assert settings.x_kol_discovery_cache_seconds == 3600
    assert settings.x_kol_discovery_limit == 8
    assert settings.x_kol_candidate_sample_size == 20
    assert settings.x_kol_recent_posts_per_account == 2


def test_official_token_can_be_loaded_from_keychain():
    completed = subprocess.CompletedProcess(
        ["security"], 0, stdout="keychain-token\n", stderr=""
    )
    with patch("kol_search.settings.subprocess.run", return_value=completed) as run:
        settings = Settings(
            _env_file=None,
            X_BEARER_TOKEN_KEYCHAIN_SERVICE="kol-search-x-bearer-token",
        )

        assert settings.x_api_bearer_token() == "keychain-token"
        assert settings.backend_ready("official") == (True, None)
        assert run.call_args.args[0][-3:] == [
            "kol-search-x-bearer-token",
            "-a",
            "kol-search",
        ]


def test_real_backend_is_default_and_mock_requires_explicit_opt_in():
    settings = Settings(_env_file=None)
    assert settings.twitter_backend == "twitterapi_io"
    assert settings.backend_ready("mock")[0] is False
    with pytest.raises(TwitterBackendError, match="Mock backend is disabled"):
        create_x_read_provider("mock", settings)

    test_settings = Settings(_env_file=None, KOL_ENABLE_MOCK_BACKEND=True)
    assert isinstance(create_x_read_provider("mock", test_settings), MockTwitterClient)


def test_mock_backend_preserves_content_timestamps_across_client_restarts():
    first = MockTwitterClient()
    second = MockTwitterClient()

    first_posts = first.search_tweets("crypto", max_results=100)
    second_posts = second.search_tweets("crypto", max_results=100)

    assert {post.id: post.created_at for post in first_posts} == {
        post.id: post.created_at for post in second_posts
    }


def test_twitterapi_io_normalizes_users_posts_and_pagination():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["X-API-Key"] == "test-key"
        if request.url.path == "/twitter/user/search":
            cursor = request.url.params.get("cursor")
            if not cursor:
                return httpx.Response(200, json={
                    "users": [{
                        "id": "1",
                        "userName": "alice",
                        "name": "Alice",
                        "description": "DeFi researcher",
                        "followers": 1234,
                        "following": 50,
                        "statusesCount": 99,
                        "createdAt": "2020-01-01T00:00:00Z",
                        "profilePicture": "https://img.example/alice.jpg",
                        "profile_bio": {
                            "entities": {
                                "url": {"urls": [{"expanded_url": "https://alice.example"}]}
                            }
                        },
                    }],
                    "has_next_page": True,
                    "next_cursor": "page-2",
                    "status": "success",
                })
            return httpx.Response(200, json={
                "users": [{
                    "id": "2",
                    "userName": "bob",
                    "description": "Onchain analyst",
                    "followers": 987,
                }],
                "has_next_page": False,
                "status": "success",
            })
        if request.url.path == "/twitter/tweet/advanced_search":
            return httpx.Response(200, json={
                "tweets": [{
                    "id": "t1",
                    "text": "DeFi update @bob",
                    "createdAt": "2026-07-01T00:00:00Z",
                    "lang": "en",
                    "likeCount": 5,
                    "retweetCount": 3,
                    "replyCount": 2,
                    "quoteCount": 1,
                    "viewCount": 200,
                    "conversationId": "conversation-1",
                    "author": {"id": "1", "userName": "alice"},
                    "entities": {"user_mentions": [{"screen_name": "bob"}]},
                }],
                "status": "success",
            })
        if request.url.path == "/twitter/user/last_tweets":
            return httpx.Response(200, json={
                "data": {
                    "pin_tweet": None,
                    "tweets": [{
                        "id": "t2",
                        "text": "Latest research",
                        "author": {"id": "1", "userName": "alice"},
                        "likeCount": 2,
                    }],
                },
                "has_next_page": False,
                "status": "success",
            })
        if request.url.path == "/twitter/user/followings":
            assert request.url.params["userName"] == "alice"
            assert request.url.params["pageSize"] == "20"
            return httpx.Response(200, json={
                "data": {"followings": [{"id": "3", "userName": "carol", "followers": 3000}]},
                "has_next_page": False,
            })
        if request.url.path == "/twitter/user/verifiedFollowers":
            assert request.url.params["user_id"] == "1"
            return httpx.Response(200, json={
                "followers": [{"id": "4", "userName": "dave", "isBlueVerified": True}],
                "has_next_page": False,
            })
        raise AssertionError(f"unexpected request: {request.url}")

    client = TwitterApiIoClient(api_key="test-key")
    client._client.close()
    client._client = httpx.Client(
        transport=httpx.MockTransport(handler),
        headers={"X-API-Key": "test-key"},
    )
    try:
        users = client.search_users("defi", max_results=25)
        posts = client.search_tweets("defi", max_results=10)
        timeline = client.get_user_tweets("1", max_results=10)
        followings = client.get_followings("alice", max_results=20)
        verified_followers = client.get_verified_followers("1", max_results=20)
    finally:
        client.close()

    assert [user.username for user in users] == ["alice", "bob"]
    assert users[0].followers_count == 1234
    assert users[0].following_count == 50
    assert users[0].tweet_count == 99
    assert users[0].url == "https://alice.example"
    assert users[0].entities["url"]["urls"][0]["expanded_url"] == "https://alice.example"
    assert posts[0].author_username == "alice"
    assert posts[0].engagement == 11
    assert posts[0].lang == "en"
    assert posts[0].mentioned_usernames == ["bob"]
    assert posts[0].url == "https://x.com/alice/status/t1"
    assert posts[0].conversation_id == "conversation-1"
    assert posts[0].view_count == 200
    assert timeline[0].author_username == "alice"
    assert timeline[0].like_count == 2
    assert followings[0].username == "carol"
    assert verified_followers[0].username == "dave"
    assert [request.url.path for request in requests] == [
        "/twitter/user/search",
        "/twitter/user/search",
        "/twitter/tweet/advanced_search",
        "/twitter/user/last_tweets",
        "/twitter/user/followings",
        "/twitter/user/verifiedFollowers",
    ]


def test_twitterapi_io_retries_free_tier_rate_limit():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, json={
                "error": "Too Many Requests",
                "message": "For free-tier users, the QPS limit is one request every 5 seconds.",
            })
        return httpx.Response(200, json={
            "data": {"id": "1", "userName": "alice", "followers": 10},
            "status": "success",
        })

    client = TwitterApiIoClient(api_key="test-key")
    client._client.close()
    client._client = httpx.Client(
        transport=httpx.MockTransport(handler),
        headers={"X-API-Key": "test-key"},
    )
    try:
        with patch("kol_search.twitter.twitterapi_io.time.sleep"):
            account = client.get_user_by_username("alice")
    finally:
        client.close()

    assert calls == 2
    assert account is not None
    assert account.username == "alice"
