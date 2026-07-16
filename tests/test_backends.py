import json
import subprocess
from unittest.mock import patch

import httpx
import pytest

from kol_search.models import Account, BackendCapabilities
from kol_search.settings import Settings
from kol_search.twitter.base import TwitterBackendError
from kol_search.twitter.failover import FailoverTwitterClient
from kol_search.twitter.official import _parse_account, _parse_post
from kol_search.twitter.opencli import OpenCliTwitterClient
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


def test_opencli_backend_maps_read_only_commands():
    calls: list[list[str]] = []

    def runner(command, **kwargs):
        calls.append(command)
        operation = command[2]
        if operation == "search":
            payload = [{
                "id": "t1", "author": "alice", "bio": "DeFi researcher",
                "text": "hello @bob", "likes": 7, "views": 100,
                "created_at": "Wed Jul 01 00:00:00 +0000 2026",
            }]
        elif operation == "profile":
            payload = [{
                "screen_name": "alice",
                "name": "Alice", "bio": "DeFi researcher",
                "followers": 1234, "following": 50, "tweets": 99,
                "verified": True, "url": "https://alice.example",
            }]
        elif operation == "tweets":
            payload = [{
                "id": "t2", "author": "alice", "text": "latest",
                "likes": 3, "retweets": 2, "replies": 1,
            }]
        elif operation == "following":
            payload = [{
                "screen_name": "carol", "name": "Carol", "bio": "Onchain", "followers": 50
            }]
        elif operation == "trending":
            payload = [{"name": "DeFi", "rank": 1, "post_count": 12000}]
        else:
            raise AssertionError(operation)
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(payload), stderr="")

    client = OpenCliTwitterClient(
        command="/usr/bin/true",
        profile="ddd",
        runner=runner,
        resolve_account_id=lambda username, proposed: "1" if username == "alice" else proposed,
    )
    posts = client.search_tweets("defi", max_results=1)
    account = client.get_user_by_username("alice")
    timeline = client.get_user_tweets("1", max_results=1, username="alice")
    followings = client.get_followings("alice", max_results=1)
    trends = client.get_trends(max_results=1)

    assert posts[0].author_id == "1"
    assert posts[0].created_at == "2026-07-01T00:00:00+00:00"
    assert posts[0].mentioned_usernames == ["bob"]
    assert account and account.id == "1" and account.followers_count == 1234
    assert timeline[0].engagement == 6
    assert followings[0].id == "opencli:carol"
    assert trends[0].name == "DeFi"
    assert all("--profile" not in command for command in calls)
    assert calls[0][1:7] == [
        "twitter", "search", "defi", "--product", "live", "--limit"
    ]
    assert ["profile", "alice"] == calls[1][2:4]
    assert ["tweets", "alice", "--limit", "1"] == calls[2][2:6]
    assert ["following", "alice"] == calls[3][2:4]


def test_failover_is_sticky_and_preserves_diagnostics():
    class Client:
        capabilities = BackendCapabilities(
            user_search=True, followings=True, verified_followers=True
        )

        def __init__(self, name: str, fail: bool = False):
            self.name = name
            self.fail = fail
            self.calls = 0

        def search_users(self, query: str, max_results: int = 100):
            self.calls += 1
            if self.fail:
                raise TwitterBackendError("credits exhausted", status_code=402)
            return [Account(id="1", username="alice")]

        def get_verified_followers(
            self, user_id: str, max_results: int = 20, *, username=None
        ):
            self.calls += 1
            return []

        def close(self):
            return None

    primary = Client("twitterapi_io", fail=True)
    fallback = Client("opencli")
    fallback.capabilities = BackendCapabilities(user_search=True, verified_followers=False)
    client = FailoverTwitterClient(primary, fallback)

    assert client.search_users("defi")[0].username == "alice"
    assert client.search_users("bitcoin")[0].username == "alice"
    assert client.get_verified_followers("1", username="alice") == []
    assert primary.calls == 1
    assert fallback.calls == 2
    assert client.diagnostics["active_backend"] == "opencli"
    assert client.diagnostics["fallback_count"] == 1
    assert any("verified followers" in warning for warning in client.warnings)


def test_failover_does_not_hide_bad_request():
    class Primary:
        name = "twitterapi_io"
        capabilities = BackendCapabilities()

        def search_users(self, query: str, max_results: int = 100):
            raise TwitterBackendError("bad query", status_code=400)

        def close(self):
            return None

    class Fallback:
        name = "opencli"
        capabilities = BackendCapabilities()

        def search_users(self, query: str, max_results: int = 100):
            raise AssertionError("must not fallback")

        def close(self):
            return None

    client = FailoverTwitterClient(Primary(), Fallback())
    with pytest.raises(TwitterBackendError):
        client.search_users("bad")
