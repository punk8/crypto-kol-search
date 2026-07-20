from pathlib import Path
from unittest.mock import patch

from kol_search.db import Store
from kol_search.discovery.seed_pipeline import (
    BudgetExceeded,
    CallBudget,
    SeedPipeline,
    TOPIC_QUOTAS,
    balanced_seed_sample,
    related_usernames,
)
from kol_search.settings import Settings
from kol_search.models import BackendCapabilities, Post
from kol_search.twitter.base import TwitterBackendError


def test_mock_seed_build_and_expand_is_exact_and_explainable(tmp_path: Path):
    path = tmp_path / "seed.db"
    store = Store(path)
    settings = Settings(
        _env_file=None,
        TWITTER_BACKEND="mock",
        KOL_ENABLE_MOCK_BACKEND=True,
        KOL_DB_PATH=str(path),
    )
    pipeline = SeedPipeline(store, settings)

    build_run_id = store.create_run(query="seed build", backend="mock", kind="seed_build", config={})
    base = pipeline.run(store.get_run(build_run_id))
    base_set = store.list_seed_sets()[0]
    assert len(base) == 100
    assert base_set["status"] == "ready"
    assert base_set["approved_count"] == 100

    expand_run_id = store.create_run(
        query="seed expand",
        backend="mock",
        kind="seed_expand",
        config={"seed_set_id": base_set["id"]},
    )
    expanded = pipeline.run(store.get_run(expand_run_id))
    expanded_set = store.list_seed_sets()[0]
    members = store.list_seed_members(expanded_set["id"])
    base_ids = {member["account_id"] for member in members if member["role"] == "base"}
    new_members = [member for member in members if member["role"] == "expanded"]

    assert len(expanded) == 100
    assert expanded_set["member_count"] == 200
    assert expanded_set["approved_count"] == 100
    assert expanded_set["pending_count"] == 100
    assert len(new_members) == 100
    assert not base_ids.intersection({member["account_id"] for member in new_members})
    assert all(member["discovery_paths"] for member in new_members)
    assert all(member["common_seed_count"] >= 1 for member in new_members)


def test_seed_review_status_and_handle_history(tmp_path: Path):
    store = Store(tmp_path / "status.db")
    store.upsert_account({"id": "123", "username": "old_handle"})
    store.upsert_account({"id": "123", "username": "new_handle"})
    seed_set_id = store.create_seed_set(
        name="test", version="v1", target_size=1, quotas={}, status="draft"
    )
    store.upsert_seed_member(
        seed_set_id,
        "123",
        role="expanded",
        review_status="pending",
        primary_topic="bitcoin",
        language_bucket="en",
        account_type_bucket="person",
    )
    store.update_seed_member_status(seed_set_id, "123", "approved")
    assert store.list_seed_members(seed_set_id)[0]["review_status"] == "approved"
    assert {row["username"] for row in store.get_account("123")["handles"]} == {
        "old_handle",
        "new_handle",
    }


def test_budget_optional_stop_and_hard_limit():
    budget = CallBudget(hard_limit_usd=0.001, optional_stop_ratio=0.8)
    budget.record("profile", 4)
    assert budget.optional_allowed
    budget.record("profile", 1)
    assert not budget.optional_allowed
    try:
        budget.record("profile", 1)
    except BudgetExceeded:
        pass
    else:
        raise AssertionError("hard budget must stop the request")


def test_following_seed_sample_balances_topics():
    members = [
        {"rank": rank, "primary_topic": topic}
        for topic, count in TOPIC_QUOTAS.items()
        for rank in range(count)
    ]
    selected = balanced_seed_sample(members, 40)
    assert len(selected) == 40
    assert {member["primary_topic"] for member in selected} == set(TOPIC_QUOTAS)


def test_relationship_author_extraction_includes_mentions_replies_and_quotes():
    post = Post(
        id="1",
        author_id="seed",
        mentioned_usernames=["Mentioned"],
        raw={
            "inReplyToUsername": "ReplyTarget",
            "quotedTweet": {"author": {"userName": "QuotedAuthor"}},
        },
    )
    assert related_usernames(post) == {"mentioned", "replytarget", "quotedauthor"}


def test_http_402_leaves_incomplete_set_without_members(tmp_path: Path):
    class NoCreditClient:
        name = "twitterapi_io"
        capabilities = BackendCapabilities(user_search=True)

        def get_user_by_username(self, username: str):
            raise TwitterBackendError("credits exhausted", status_code=402)

        def close(self):
            return None

    path = tmp_path / "no-credit.db"
    store = Store(path)
    settings = Settings(_env_file=None, TWITTER_BACKEND="twitterapi_io", API_KEY="test")
    pipeline = SeedPipeline(store, settings)
    run_id = store.create_run(query="seed build", backend="twitterapi_io", kind="seed_build")
    with patch(
        "kol_search.discovery.seed_pipeline.create_twitter_client",
        return_value=NoCreditClient(),
    ):
        try:
            pipeline.run(store.get_run(run_id))
        except RuntimeError as exc:
            assert "402" in str(exc)
        else:
            raise AssertionError("402 must fail the live build")
    seed_set = store.list_seed_sets()[0]
    assert seed_set["status"] == "incomplete"
    assert seed_set["member_count"] == 0
