"""Hybrid rule and LLM classification for user-specific interest profiles.

The module deliberately has no SQL. Database implementations can persist the
returned :class:`InterestDecision` through ``InterestDecisionStore`` once the
active storage backend is selected.
"""
from __future__ import annotations

import json
import re
from collections.abc import Iterable
from typing import Any

from paperbase.models import LLMMessage
from paperbase.pipeline.filter import DEFAULT_RULES, match_text

from .models import InterestDecision, InterestDecisionStore, InterestProfile


def profile_from_config(config: dict, profile_id: str | None = None) -> InterestProfile:
    """Read and validate one named profile from the ``[interest]`` config."""
    interest = config.get("interest", {}) or {}
    selected_id = profile_id or str(interest.get("default_profile", "research"))
    profiles = interest.get("profiles", {}) or {}
    raw = profiles.get(selected_id)
    if not isinstance(raw, dict):
        raise ValueError(f"interest profile {selected_id!r} is not configured")

    def strings(value: Any, key: str) -> tuple[str, ...]:
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ValueError(f"interest profile {selected_id!r}: {key} must be a list of strings")
        return tuple(item for item in value if item.strip())

    raw_rules = raw.get("rules", {}) or {}
    if not isinstance(raw_rules, dict):
        raise ValueError(f"interest profile {selected_id!r}: rules must be a table")
    rules = {str(tag): strings(patterns, f"rules.{tag}") for tag, patterns in raw_rules.items()}
    llm = raw.get("llm", {}) or {}
    if not isinstance(llm, dict):
        raise ValueError(f"interest profile {selected_id!r}: llm must be a table")
    candidate_min_matches = int(llm.get("candidate_min_matches", 1))
    if candidate_min_matches < 0:
        raise ValueError("candidate_min_matches must be non-negative")
    return InterestProfile(
        id=selected_id,
        name=str(raw.get("name") or selected_id),
        description=str(raw.get("description") or ""),
        include_tags=strings(raw.get("include_tags", []), "include_tags"),
        exclude_tags=strings(raw.get("exclude_tags", []), "exclude_tags"),
        rules=rules,
        negative_rules=strings(raw.get("negative_rules", []), "negative_rules"),
        llm_enabled=bool(llm.get("enabled", False)),
        llm_review_all=bool(llm.get("review_all", False)),
        llm_candidate_min_matches=candidate_min_matches,
    )


def _validate_patterns(patterns: Iterable[str]) -> list[re.Pattern[str]]:
    return [re.compile(pattern, re.IGNORECASE) for pattern in patterns]


def _rule_decision(paper: dict, profile: InterestProfile) -> InterestDecision:
    title = str(paper.get("title") or "")
    abstract = str(paper.get("abstract") or "")
    text = f"{title}\n{abstract}"
    paper_id = str(paper.get("id") or "")
    if not paper_id:
        raise ValueError("paper id is required for interest classification")

    negative = _validate_patterns(profile.negative_rules)
    if any(pattern.search(text) for pattern in negative):
        return InterestDecision(
            paper_id=paper_id,
            profile_id=profile.id,
            label="not_interested",
            score=0.0,
            reasons=("matched a profile exclusion rule",),
        )

    rules = dict(DEFAULT_RULES)
    rules.update({tag: list(patterns) for tag, patterns in profile.rules.items()})
    matched = match_text(title, abstract, {tag: _validate_patterns(patterns) for tag, patterns in rules.items()})
    selected = [tag for tag in matched if tag in profile.include_tags and tag not in profile.exclude_tags]
    if not selected:
        return InterestDecision(paper_id, profile.id, "not_interested", 0.0)

    score = min(0.85, 0.55 + 0.10 * (len(selected) - 1))
    return InterestDecision(
        paper_id=paper_id,
        profile_id=profile.id,
        label="interested" if len(selected) > 1 else "maybe",
        score=score,
        matched_tags=tuple(selected),
        reasons=tuple(f"matched topic: {tag}" for tag in selected),
    )


def _llm_decision(client, paper: dict, profile: InterestProfile, rule_decision: InterestDecision) -> InterestDecision | None:
    """Ask an LLM for a constrained JSON verdict; malformed output is ignored."""
    prompt = {
        "profile": {"name": profile.name, "description": profile.description},
        "paper": {"title": paper.get("title", ""), "abstract": str(paper.get("abstract", ""))[:12000]},
        "rule_result": rule_decision.as_dict(),
        "instruction": (
            "Classify relevance to this profile. Return JSON only with label "
            "(interested, maybe, or not_interested), score (0 to 1), and reason."
        ),
    }
    try:
        response = client.chat(
            [LLMMessage(role="user", content=json.dumps(prompt, ensure_ascii=False))],
            temperature=0,
            max_tokens=180,
            budget_tag="interest_classify",
        )
        data = json.loads(response.content)
        label = data.get("label")
        score = float(data.get("score"))
        reason = str(data.get("reason") or "LLM profile review")
        if label not in {"interested", "maybe", "not_interested"} or not 0 <= score <= 1:
            return None
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
        return None
    return InterestDecision(
        paper_id=rule_decision.paper_id,
        profile_id=profile.id,
        label=label,
        score=score,
        matched_tags=rule_decision.matched_tags,
        reasons=rule_decision.reasons + (reason,),
        method="hybrid",
        model=str(getattr(client, "model", "")),
    )


def classify_paper(paper: dict, profile: InterestProfile, *, client=None) -> InterestDecision:
    """Classify one paper without writing state.

    Rules always run first. An enabled LLM reviews matching candidates, or all
    papers when ``llm_review_all`` is configured. Network/model failures retain
    the deterministic rule result.
    """
    rule_decision = _rule_decision(paper, profile)
    should_review = profile.llm_enabled and client is not None and (
        profile.llm_review_all or len(rule_decision.matched_tags) >= profile.llm_candidate_min_matches
    )
    reviewed = _llm_decision(client, paper, profile, rule_decision) if should_review else None
    return reviewed or rule_decision


def classify_papers(
    papers: Iterable[dict],
    profile: InterestProfile,
    *,
    client=None,
    store: InterestDecisionStore | None = None,
) -> list[InterestDecision]:
    """Classify papers and optionally pass each immutable decision to a store."""
    decisions = [classify_paper(paper, profile, client=client) for paper in papers]
    if store is not None:
        for decision in decisions:
            store.save(decision)
    return decisions


__all__ = ["classify_paper", "classify_papers", "profile_from_config"]
