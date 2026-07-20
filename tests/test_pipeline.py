from pathlib import Path

from kol_search.db import Store
from kol_search.discovery.pipeline import DiscoveryPipeline
from kol_search.settings import Settings


def test_mock_pipeline_end_to_end(tmp_path: Path):
    settings = Settings(
        _env_file=None,
        TWITTER_BACKEND="mock",
        KOL_ENABLE_MOCK_BACKEND=True,
        KOL_DB_PATH=str(tmp_path / "kol.db"),
        KOL_MAX_CONTACT_ACCOUNTS=30,
    )
    store = Store(settings.db_path())
    run_id = store.create_run(
        query="DeFi, 去中心化金融",
        backend="mock",
        result_limit=30,
        min_followers=1000,
    )
    run = store.get_run(run_id)
    candidates = DiscoveryPipeline(store, settings).run(run)
    usernames = {candidate.account.username for candidate in candidates}
    assert "alpha_defi_researcher" in usernames
    assert "random_foodie" not in usernames
    assert all(candidate.rank for candidate in candidates)
    assert any(candidate.enrichment.account_type == "person" for candidate in candidates)
    assert any(candidate.enrichment.account_type != "person" for candidate in candidates)

    contacts = store.contacts_for_accounts([candidate.account.id for candidate in candidates])
    assert any(item["status"] == "pending" for values in contacts.values() for item in values)
    assert all(item["source_url"] and item["evidence"] for values in contacts.values() for item in values)


def test_global_refresh_covers_multiple_topic_families(tmp_path: Path):
    settings = Settings(
        _env_file=None,
        TWITTER_BACKEND="mock",
        KOL_ENABLE_MOCK_BACKEND=True,
        KOL_DB_PATH=str(tmp_path / "global.db"),
        KOL_MAX_CONTACT_ACCOUNTS=30,
    )
    store = Store(settings.db_path())
    run_id = store.create_run(
        query="global crypto library",
        backend="mock",
        kind="global",
        result_limit=100,
    )
    candidates = DiscoveryPipeline(store, settings).run(store.get_run(run_id))
    topics = {topic for candidate in candidates for topic in candidate.enrichment.topics}
    assert {"bitcoin", "defi", "solana", "regulation", "nft_gamefi"} <= topics
