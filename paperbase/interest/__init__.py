"""Configurable, profile-based paper-interest classification."""

from .engine import classify_paper, classify_papers, profile_from_config
from .models import InterestDecision, InterestProfile

__all__ = [
    "InterestDecision",
    "InterestProfile",
    "classify_paper",
    "classify_papers",
    "profile_from_config",
]
