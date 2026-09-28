"""Domain-pack loader.

A domain pack (``domains/<name>/domain.yaml``) is PURE DATA. Every
crypto-specific constant that used to live sprinkled across
core/, analysis/, self_modify/ now lives in ONE YAML file per
domain — the code that consumed those constants imports them
via ``current_domain()`` here.

Contract for chantier 1 of the domain-pack refactor:

  · The loader is READ-ONLY at import time (cached).
  · Crypto stays in the current Postgres schema + Chroma
    collections. Only NEW domains would get their own
    namespaces (chantier 2/3). No production data migration
    happens in this commit.
  · Every literal moved into the YAML is grep-locked in
    tests/test_domain_pack_locks.py: the literal MUST NOT
    appear outside ``domains/`` (with a small allowlist for
    docstrings/comments/tests).
  · Behaviour preservation is verified by running the full
    pytest suite, the campaign-quality scorer, and the
    session report before + after the refactor and comparing
    outputs byte-for-byte (values are wrapped, not changed).

Env override: ``MORGOTH_DOMAIN=<name>`` selects the pack
(default = crypto). A future ``morgoth domain <name>`` CLI
just sets that env var.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from loguru import logger


DEFAULT_DOMAIN: str = "crypto"
_DOMAINS_ROOT: Path = Path(__file__).resolve().parent.parent / "domains"


@dataclass(frozen=True)
class Domain:
    """A loaded domain pack. Every field is DATA sourced from YAML.

    Names mirror the constant names they replace so grep-locks and
    call-sites stay legible. Tuples over sets/frozensets — YAML
    scalars are ordered; consumers wrap in ``frozenset(...)`` where
    unordered semantics matter.
    """
    name: str
    tagline: str
    # -- subject / prompt vocab --------------------------------------------
    generic_subject_tokens: tuple[str, ...]
    price_class_tokens: tuple[str, ...]
    subject_prefix_stopwords: tuple[str, ...]
    name_stopwords: tuple[str, ...]
    phrase_stopwords: tuple[str, ...]
    campaign_title_stopwords: tuple[str, ...]
    # -- served / registry ------------------------------------------------
    tool_served_phrases: dict[str, tuple[str, ...]]
    rail_tool_fields: dict[str, tuple[str, ...]]
    # -- classifier tables ------------------------------------------------
    bullish_words: tuple[str, ...]
    bearish_words: tuple[str, ...]
    metric_families: dict[str, tuple[str, ...]]
    scope_asset_metric_tokens: tuple[str, ...]
    scope_asset_subject_tokens: tuple[str, ...]
    scope_dominance_exception_tokens: tuple[str, ...]
    scope_source_tool: str
    # -- field-confusion --------------------------------------------------
    field_phrases: dict[str, dict[str, tuple[str, ...]]]
    ambiguous_phrases: tuple[str, ...]
    single_numeric_field: dict[str, str]
    timestamp_like_fields: tuple[str, ...]
    # -- metric recorder --------------------------------------------------
    metric_names: dict[str, str]      # canonical → stored name
    metric_field_map: dict[str, str]  # payload_field → canonical
    metric_source_tool: str
    # -- backtest descriptive markers ------------------------------------
    backtest_subject_markers: dict[str, tuple[str, ...]]
    # -- reflect / brain prompt snippets ---------------------------------
    prompt_bootstrap_snippet: str
    test_bootstrap_tool_defaults: dict[str, dict[str, Any]]
    # -- source cache polling schedule -----------------------------------
    source_cache_config: dict[str, tuple[int, int]]
    source_cache_default_args: dict[str, dict[str, Any]]


def _as_tuple(x: Any) -> tuple[str, ...]:
    if x is None:
        return ()
    if isinstance(x, (list, tuple)):
        return tuple(str(v) for v in x)
    if isinstance(x, str):
        return (x,)
    raise TypeError(f"expected list or str, got {type(x).__name__}")


def _dict_of_tuple(x: Any) -> dict[str, tuple[str, ...]]:
    return {str(k): _as_tuple(v) for k, v in (x or {}).items()}


def _dict_of_dict_of_tuple(x: Any) -> dict[str, dict[str, tuple[str, ...]]]:
    return {str(k): _dict_of_tuple(v) for k, v in (x or {}).items()}


def _dict_of_str(x: Any) -> dict[str, str]:
    return {str(k): str(v) for k, v in (x or {}).items()}


def _dict_of_dict(x: Any) -> dict[str, dict[str, Any]]:
    return {str(k): dict(v) for k, v in (x or {}).items()}


def _dict_of_pair(x: Any) -> dict[str, tuple[int, int]]:
    return {str(k): (int(v[0]), int(v[1])) for k, v in (x or {}).items()}


def _load(name: str) -> Domain:
    path = _DOMAINS_ROOT / name / "domain.yaml"
    if not path.exists():
        raise FileNotFoundError(
            f"domain pack {name!r} not found at {path}. "
            f"Existing packs: {sorted(p.name for p in _DOMAINS_ROOT.iterdir()) if _DOMAINS_ROOT.exists() else []}"
        )
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return Domain(
        name=str(raw.get("name") or name),
        tagline=str(raw.get("tagline") or ""),
        generic_subject_tokens=_as_tuple(raw.get("generic_subject_tokens")),
        price_class_tokens=_as_tuple(raw.get("price_class_tokens")),
        subject_prefix_stopwords=_as_tuple(raw.get("subject_prefix_stopwords")),
        name_stopwords=_as_tuple(raw.get("name_stopwords")),
        phrase_stopwords=_as_tuple(raw.get("phrase_stopwords")),
        campaign_title_stopwords=_as_tuple(raw.get("campaign_title_stopwords")),
        tool_served_phrases=_dict_of_tuple(raw.get("tool_served_phrases")),
        rail_tool_fields=_dict_of_tuple(raw.get("rail_tool_fields")),
        bullish_words=_as_tuple(raw.get("bullish_words")),
        bearish_words=_as_tuple(raw.get("bearish_words")),
        metric_families=_dict_of_tuple(raw.get("metric_families")),
        scope_asset_metric_tokens=_as_tuple(raw.get("scope_asset_metric_tokens")),
        scope_asset_subject_tokens=_as_tuple(raw.get("scope_asset_subject_tokens")),
        scope_dominance_exception_tokens=_as_tuple(raw.get("scope_dominance_exception_tokens")),
        scope_source_tool=str(raw.get("scope_source_tool") or ""),
        field_phrases=_dict_of_dict_of_tuple(raw.get("field_phrases")),
        ambiguous_phrases=_as_tuple(raw.get("ambiguous_phrases")),
        single_numeric_field=_dict_of_str(raw.get("single_numeric_field")),
        timestamp_like_fields=_as_tuple(raw.get("timestamp_like_fields")),
        metric_names=_dict_of_str(raw.get("metric_names")),
        metric_field_map=_dict_of_str(raw.get("metric_field_map")),
        metric_source_tool=str(raw.get("metric_source_tool") or ""),
        backtest_subject_markers=_dict_of_tuple(raw.get("backtest_subject_markers")),
        prompt_bootstrap_snippet=str(raw.get("prompt_bootstrap_snippet") or ""),
        test_bootstrap_tool_defaults=_dict_of_dict(raw.get("test_bootstrap_tool_defaults")),
        source_cache_config=_dict_of_pair(raw.get("source_cache_config")),
        source_cache_default_args=_dict_of_dict(raw.get("source_cache_default_args")),
    )


@lru_cache(maxsize=1)
def current_domain() -> Domain:
    """Return the loaded domain pack (memoized).

    Selection: env var ``MORGOTH_DOMAIN`` → default ``crypto``.
    """
    name = os.environ.get("MORGOTH_DOMAIN", DEFAULT_DOMAIN).strip() or DEFAULT_DOMAIN
    try:
        pack = _load(name)
    except FileNotFoundError:
        logger.warning(
            "domain pack {!r} missing; falling back to {!r}", name, DEFAULT_DOMAIN,
        )
        pack = _load(DEFAULT_DOMAIN)
    logger.debug("domain pack loaded: {}", pack.name)
    return pack


def reset_domain_cache() -> None:
    """Test hook — drop the memoized pack so a monkeypatched
    ``MORGOTH_DOMAIN`` is picked up on the next call."""
    current_domain.cache_clear()
