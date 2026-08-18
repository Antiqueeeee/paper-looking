"""Public data structures for interest classification."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol

InterestLabel = Literal["interested", "maybe", "not_interested"]


@dataclass(frozen=True)
class InterestProfile:
    """A named, portable definition of one person's current interests."""

    id: str
    name: str
    description: str = ""
    include_tags: tuple[str, ...] = ()
    exclude_tags: tuple[str, ...] = ()
    rules: dict[str, tuple[str, ...]] = field(default_factory=dict)
    negative_rules: tuple[str, ...] = ()
    llm_enabled: bool = False
    llm_review_all: bool = False
    llm_candidate_min_matches: int = 1


@dataclass(frozen=True)
class InterestDecision:
    """A reproducible decision suitable for storage by any database backend."""

    paper_id: str
    profile_id: str
    label: InterestLabel
    score: float
    matched_tags: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    method: str = "rules"
    model: str = ""

    def as_dict(self) -> dict:
        return {
            "paper_id": self.paper_id,
            "profile_id": self.profile_id,
            "label": self.label,
            "score": self.score,
            "matched_tags": list(self.matched_tags),
            "reasons": list(self.reasons),
            "method": self.method,
            "model": self.model,
        }


class InterestDecisionStore(Protocol):
    """Adapter point for the future database-specific decision repository."""

    def save(self, decision: InterestDecision) -> None: ...


__all__ = ["InterestDecision", "InterestDecisionStore", "InterestLabel", "InterestProfile"]
