from __future__ import annotations

from paperbase.config import load_config
from paperbase.interest import classify_paper, classify_papers, profile_from_config
from paperbase.llm import LLMResponse, MockLLMClient


def _paper(title: str, abstract: str = "") -> dict:
    return {"id": "p1", "title": title, "abstract": abstract}


def test_default_profile_matches_existing_rule_baseline():
    profile = profile_from_config(load_config())
    decision = classify_paper(_paper("GraphRAG for Knowledge Graph Question Answering"), profile)
    assert decision.label == "interested"
    assert decision.matched_tags == ("kg", "kbqa", "rag")
    assert decision.method == "rules"


def test_profiles_are_independent_and_allow_custom_rules():
    config = {
        "interest": {
            "default_profile": "nlp",
            "profiles": {
                "nlp": {
                    "name": "NLP", "include_tags": ["survey"],
                    "rules": {"survey": [r"\bsurvey\b"]}, "negative_rules": [r"\btutorial\b"],
                },
            },
        },
    }
    profile = profile_from_config(config)
    assert classify_paper(_paper("A Survey of Retrieval"), profile).label == "maybe"
    assert classify_paper(_paper("A Survey Tutorial"), profile).label == "not_interested"


def test_llm_review_overrides_rule_candidate_and_failure_falls_back():
    profile = profile_from_config({
        "interest": {"default_profile": "p", "profiles": {"p": {
            "name": "P", "include_tags": ["rag"],
            "llm": {"enabled": True, "candidate_min_matches": 1},
        }}},
    })
    client = MockLLMClient([LLMResponse(content='{"label":"not_interested","score":0.1,"reason":"only a passing mention"}')])
    decision = classify_paper(_paper("Retrieval-Augmented Generation for Cooking"), profile, client=client)
    assert decision.label == "not_interested"
    assert decision.method == "hybrid"
    assert client.calls[0]["budget_tag"] == "interest_classify"

    fallback = classify_paper(_paper("Retrieval-Augmented Generation for Cooking"), profile, client=MockLLMClient([LLMResponse(content="not json")]))
    assert fallback.label == "maybe"
    assert fallback.method == "rules"


def test_classify_papers_can_store_without_database_coupling():
    class Store:
        saved = []

        def save(self, decision):
            self.saved.append(decision)

    store = Store()
    profile = profile_from_config(load_config())
    decisions = classify_papers([_paper("Knowledge Graph Embedding")], profile, store=store)
    assert store.saved == decisions
    assert decisions[0].as_dict()["profile_id"] == "research"
