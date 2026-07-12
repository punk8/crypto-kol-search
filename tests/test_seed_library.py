import csv
from collections import Counter
from pathlib import Path

from kol_search.config_loader import load_seed_handles
from kol_search.discovery.seed_pipeline import language_bucket, type_bucket, validate_seed_quotas


ROOT = Path(__file__).resolve().parents[1]


def test_seed_library_runtime_list_contains_only_approved_people():
    with (ROOT / "seeds" / "crypto_seed_library.csv").open(encoding="utf-8", newline="") as file:
        rows = list(csv.DictReader(file))

    handles = load_seed_handles("seeds/crypto_handles.txt")
    approved_people = [
        row["handle"]
        for row in rows
        if row["status"] == "approved" and row["account_type"] == "person"
    ]

    assert handles == approved_people
    assert len(handles) == 80
    assert len({handle.lower() for handle in handles}) == len(handles)
    assert all(row["source_url"] == f"https://x.com/{row['handle']}" for row in rows)
    assert all(row["verified_at"] == "" for row in rows)
    assert all("live_validation_required" in row["risk_flags"] for row in rows if row["status"] == "approved")
    assert not ({row["handle"] for row in rows if row["status"] == "review"} & set(handles))


def test_seed_library_keeps_institutions_and_review_queue_separate():
    with (ROOT / "seeds" / "crypto_seed_library.csv").open(encoding="utf-8", newline="") as file:
        rows = list(csv.DictReader(file))

    institutions = [row for row in rows if row["seed_tier"] == "institution"]
    review = [row for row in rows if row["status"] == "review"]

    assert len(institutions) == 20
    assert all(row["account_type"] != "person" for row in institutions)
    assert len(review) == 40
    assert all(row["risk_flags"] for row in review)


def test_approved_catalog_matches_fixed_quotas():
    with (ROOT / "seeds" / "crypto_seed_library.csv").open(encoding="utf-8", newline="") as file:
        approved = [row for row in csv.DictReader(file) if row["status"] == "approved"]
    assert validate_seed_quotas(approved) == []
    assert Counter(type_bucket(row["account_type"]) for row in approved) == {
        "person": 80,
        "organization": 20,
    }
    assert Counter(language_bucket(row["languages"]) for row in approved) == {
        "en": 70,
        "zh": 20,
        "bilingual": 10,
    }
