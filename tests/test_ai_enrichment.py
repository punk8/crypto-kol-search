import sys
import types

from kol_search.discovery.ai_enrichment import AIClassification, enrich_with_openai
from kol_search.models import Account, Post


def test_openai_enrichment_is_structured_and_cached(monkeypatch):
    calls: list[dict] = []

    class Responses:
        def parse(self, **kwargs):
            calls.append(kwargs)
            return types.SimpleNamespace(output_parsed=AIClassification(
                account_type="person",
                languages=["zh", "en"],
                topics=["defi"],
                summary="Bilingual DeFi researcher",
                relevance_score=0.91,
                confidence=0.95,
            ))

    class FakeOpenAI:
        def __init__(self, api_key):
            self.responses = Responses()

    module = types.ModuleType("openai")
    module.OpenAI = FakeOpenAI
    monkeypatch.setitem(sys.modules, "openai", module)

    cache: dict[str, dict] = {}
    account = Account(id="1", username="alice", description="DeFi 研究员")
    posts = [Post(id="p", author_id="1", text="Ethereum DeFi research")]
    first = enrich_with_openai(
        account,
        posts,
        "DeFi",
        api_key="test",
        model="test-model",
        cache_get=cache.get,
        cache_set=cache.__setitem__,
    )
    second = enrich_with_openai(
        account,
        posts,
        "DeFi",
        api_key="test",
        model="test-model",
        cache_get=cache.get,
        cache_set=cache.__setitem__,
    )
    assert first.account_type == "person"
    assert second == first
    assert len(calls) == 1
    assert calls[0]["text_format"] is AIClassification

