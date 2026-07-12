from kol_search.config_loader import load_domain_config
from kol_search.discovery.classify import classify_account, detect_languages
from kol_search.discovery.query import plan_queries
from kol_search.models import Account, Post


def test_bilingual_query_plan_keeps_chinese_posts_and_ascii_user_search():
    plan = plan_queries("DeFi, 去中心化金融", load_domain_config("crypto"))
    assert "defi" in [query.lower() for query in plan.user_queries]
    assert all(query.isascii() for query in plan.user_queries)
    assert any("去中心化金融" in query for query in plan.post_queries)


def test_account_classification_separates_person_and_media():
    config = load_domain_config("crypto")
    person = Account(id="1", username="alice", name="Alice", description="DeFi researcher and writer")
    media = Account(id="2", username="chainnews", name="Chain News", description="Crypto media and daily news")
    post = Post(id="p", author_id="1", text="以太坊 DeFi 研究")
    assert classify_account(person, [post], config).account_type == "person"
    assert classify_account(media, [], config).account_type == "media"
    assert detect_languages("DeFi 研究") == ["zh", "en"]

