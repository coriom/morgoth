"""Trusted scorer implementations selected by Domain data.

Pack identifiers never become import paths. Domain packs with no declared scorer
never import a specialist module or select a default crypto implementation.
"""
from __future__ import annotations

from importlib import import_module
from typing import Any, Callable

_IMPLEMENTATIONS = {
    "crypto_descriptive": ("analysis.thesis_backtest_descriptive", "triage"),
    "crypto_directional": ("analysis.thesis_backtest", "resolve_theses"),
    "crypto_campaign_quality": ("analysis.campaign_quality", "score_campaign"),
}


def registered_scorers() -> frozenset[str]:
    """Return names allowed in declarative packs; no specialist is imported."""
    return frozenset(_IMPLEMENTATIONS)


def resolve_scorer(domain: Any, role: str) -> Callable | None:
    """Load only the trusted scorer explicitly declared for a Domain role."""
    identifier = domain.scorers.get(role)
    if identifier is None:
        return None
    module, symbol = _IMPLEMENTATIONS[identifier]
    return getattr(import_module(module), symbol)
