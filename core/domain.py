"""Domain-pack loader.

A domain pack (``domains/<name>/domain.yaml``) is PURE DATA. Every
crypto-specific constant that used to live sprinkled across
core/, analysis/, self_modify/ now lives in ONE YAML file per
domain — the code that consumed those constants imports them
via ``current_domain()`` here.

Domain owns vocabulary, field mappings, source/tool semantics and scoring data.
Project owns instance/storage/runtime state; see core.project.current_namespace.
The namespace fields below remain only as backward-compatible input to the
built-in legacy Project adapter. New projects never inherit them.

MORGOTH_PROJECT selects a project and its Domain. MORGOTH_DOMAIN remains a
standalone semantic-pack test/inspection hook; runtime configuration rejects a
conflicting override. No pack knowledge is duplicated into Project.

INVARIANT — ONE DOMAIN PER PROCESS. The pack is bound at
IMPORT time: every downstream constant (RAIL_TOOL_FIELDS,
SOURCE_CACHE_CONFIG, METRIC_FIELD_MAP, …) reads
``current_domain()`` once at module load and freezes the
values into module-level names. ``reset_domain_cache()``
drops the memoized pack, but does NOT rebind those module-
level constants — that would require a fresh Python process.
Domain-switching is therefore a RESTART operation, never a
runtime toggle. Locked by
``tests/test_domain_pack_locks.py::test_one_domain_per_process_invariant``.
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


class DomainPackError(ValueError):
    """Raised when a domain pack fails validation OR does not exist.

    Distinct from generic ValueError so the process-entry code can
    catch it and print a helpful message before exiting."""


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
    # Deprecated compatibility fields: ONLY legacy_project() imports these.
    # Storage consumers must use current_namespace(), never current_domain().
    postgres_schema: str
    chroma_prefix: str
    vault_dir: str
    # Measurement metadata only; Project storage/selection stays unchanged.
    tool_sources: dict[str, str] = field(default_factory=dict)
    source_aliases: dict[str, tuple[str, ...]] = field(default_factory=dict)
    field_units: dict[str, dict[str, str]] = field(default_factory=dict)
    field_contexts: dict[str, dict[str, str]] = field(default_factory=dict)
    coverage_exemptions: dict[str, dict[str, str]] = field(default_factory=dict)



# ─── STRICT VALIDATORS ──────────────────────────────────────────────
# YAML 1.1 silently coerces bare tokens: ``on`` → True, ``off`` → False,
# ``yes`` / ``no`` → bool, ``null`` → None, ``1.5e10`` → float. The
# grep-lock tests do NOT catch that because both sides read the same
# corrupt pack. Every field-level validator below therefore REJECTS
# non-string leaves where a string was declared, naming the field and
# the offending value in the error. The rule: quote it in YAML, or the
# pack refuses to load.


def _fail(path: str, expected: str, got: Any) -> None:
    raise DomainPackError(
        f"domain pack: field {path!r} expected {expected}, "
        f"got {type(got).__name__} = {got!r} "
        f"(hint: YAML 1.1 tokens like on/off/yes/no/null and numbers "
        f"become bool/None/int/float — quote the value with \"…\")"
    )


def _str(x: Any, path: str) -> str:
    if not isinstance(x, str):
        _fail(path, "str", x)
    return x


def _str_or_empty(x: Any, path: str) -> str:
    if x is None:
        return ""
    return _str(x, path)


def _str_list(x: Any, path: str) -> tuple[str, ...]:
    if x is None:
        return ()
    if not isinstance(x, list):
        _fail(path, "list[str]", x)
    return tuple(_str(v, f"{path}[{i}]") for i, v in enumerate(x))


def _str_map(x: Any, path: str) -> dict[str, str]:
    if x is None:
        return {}
    if not isinstance(x, dict):
        _fail(path, "dict[str, str]", x)
    return {_str(k, f"{path}.<key>"): _str(v, f"{path}.{k}") for k, v in x.items()}


def _str_list_map(x: Any, path: str) -> dict[str, tuple[str, ...]]:
    if x is None:
        return {}
    if not isinstance(x, dict):
        _fail(path, "dict[str, list[str]]", x)
    return {_str(k, f"{path}.<key>"): _str_list(v, f"{path}.{k}") for k, v in x.items()}


def _str_map_map(x: Any, path: str) -> dict[str, dict[str, str]]:
    """Validate nested measurement maps without coercing YAML scalar types."""
    if x is None:
        return {}
    if not isinstance(x, dict):
        _fail(path, "dict[str, dict[str, str]]", x)
    return {_str(k, f"{path}.<key>"): _str_map(v, f"{path}.{k}") for k, v in x.items()}


def _str_list_map_map(
    x: Any, path: str,
) -> dict[str, dict[str, tuple[str, ...]]]:
    if x is None:
        return {}
    if not isinstance(x, dict):
        _fail(path, "dict[str, dict[str, list[str]]]", x)
    return {
        _str(k, f"{path}.<key>"): _str_list_map(v, f"{path}.{k}")
        for k, v in x.items()
    }


def _int_pair_map(x: Any, path: str) -> dict[str, tuple[int, int]]:
    if x is None:
        return {}
    if not isinstance(x, dict):
        _fail(path, "dict[str, [int, int]]", x)
    out: dict[str, tuple[int, int]] = {}
    for k, v in x.items():
        pk = _str(k, f"{path}.<key>")
        # A YAML sequence of two ints. Booleans are rejected because
        # ``bool`` is a subclass of ``int`` in Python — enforce here.
        if (not isinstance(v, list) or len(v) != 2
                or any(isinstance(el, bool) or not isinstance(el, int) for el in v)):
            _fail(f"{path}.{pk}", "[int, int]", v)
        out[pk] = (int(v[0]), int(v[1]))
    return out


def _any_map_map(x: Any, path: str) -> dict[str, dict[str, Any]]:
    """dict[str, dict[str, Any]] — the inner values are tool arguments
    whose types are the tool's contract, not the pack's. We keep them
    as-is, but still enforce the outer shape and string keys."""
    if x is None:
        return {}
    if not isinstance(x, dict):
        _fail(path, "dict[str, dict[str, Any]]", x)
    out: dict[str, dict[str, Any]] = {}
    for k, v in x.items():
        pk = _str(k, f"{path}.<key>")
        if not isinstance(v, dict):
            _fail(f"{path}.{pk}", "dict[str, Any]", v)
        # Keys of the inner dict MUST be strings (tool arg names).
        inner: dict[str, Any] = {}
        for ik, iv in v.items():
            inner[_str(ik, f"{path}.{pk}.<key>")] = iv
        out[pk] = inner
    return out


def _load(name: str) -> Domain:
    from core.storage_namespace import validate_identifier
    try:
        validate_identifier(name)
    except ValueError:
        raise DomainPackError("invalid domain identifier") from None
    path = _DOMAINS_ROOT / name / "domain.yaml"
    if not path.exists():
        raise DomainPackError(
            f"domain pack {name!r} not found at {path}. "
            f"Existing packs: {sorted(p.name for p in _DOMAINS_ROOT.iterdir()) if _DOMAINS_ROOT.exists() else []}"
        )
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise DomainPackError(
            f"domain pack {name!r} at {path}: top-level YAML must be a "
            f"mapping, got {type(raw).__name__}"
        )
    from core.storage_namespace import validate_namespace
    try:
        validate_namespace(raw.get("postgres_schema", "public"), raw.get("chroma_prefix") or "", legacy=True)
    except ValueError as exc:
        raise DomainPackError(str(exc)) from None
    return Domain(
        name=_str(raw.get("name") or name, "name"),
        tagline=_str_or_empty(raw.get("tagline"), "tagline"),
        generic_subject_tokens=_str_list(raw.get("generic_subject_tokens"), "generic_subject_tokens"),
        price_class_tokens=_str_list(raw.get("price_class_tokens"), "price_class_tokens"),
        subject_prefix_stopwords=_str_list(raw.get("subject_prefix_stopwords"), "subject_prefix_stopwords"),
        name_stopwords=_str_list(raw.get("name_stopwords"), "name_stopwords"),
        phrase_stopwords=_str_list(raw.get("phrase_stopwords"), "phrase_stopwords"),
        campaign_title_stopwords=_str_list(raw.get("campaign_title_stopwords"), "campaign_title_stopwords"),
        tool_served_phrases=_str_list_map(raw.get("tool_served_phrases"), "tool_served_phrases"),
        rail_tool_fields=_str_list_map(raw.get("rail_tool_fields"), "rail_tool_fields"),
        tool_sources=_str_map(raw.get("tool_sources"), "tool_sources"),
        source_aliases=_str_list_map(raw.get("source_aliases"), "source_aliases"),
        field_units=_str_map_map(raw.get("field_units"), "field_units"),
        field_contexts=_str_map_map(raw.get("field_contexts"), "field_contexts"),
        coverage_exemptions=_str_map_map(raw.get("coverage_exemptions"), "coverage_exemptions"),
        bullish_words=_str_list(raw.get("bullish_words"), "bullish_words"),
        bearish_words=_str_list(raw.get("bearish_words"), "bearish_words"),
        metric_families=_str_list_map(raw.get("metric_families"), "metric_families"),
        scope_asset_metric_tokens=_str_list(raw.get("scope_asset_metric_tokens"), "scope_asset_metric_tokens"),
        scope_asset_subject_tokens=_str_list(raw.get("scope_asset_subject_tokens"), "scope_asset_subject_tokens"),
        scope_dominance_exception_tokens=_str_list(raw.get("scope_dominance_exception_tokens"), "scope_dominance_exception_tokens"),
        scope_source_tool=_str_or_empty(raw.get("scope_source_tool"), "scope_source_tool"),
        field_phrases=_str_list_map_map(raw.get("field_phrases"), "field_phrases"),
        ambiguous_phrases=_str_list(raw.get("ambiguous_phrases"), "ambiguous_phrases"),
        single_numeric_field=_str_map(raw.get("single_numeric_field"), "single_numeric_field"),
        timestamp_like_fields=_str_list(raw.get("timestamp_like_fields"), "timestamp_like_fields"),
        metric_names=_str_map(raw.get("metric_names"), "metric_names"),
        metric_field_map=_str_map(raw.get("metric_field_map"), "metric_field_map"),
        metric_source_tool=_str_or_empty(raw.get("metric_source_tool"), "metric_source_tool"),
        backtest_subject_markers=_str_list_map(raw.get("backtest_subject_markers"), "backtest_subject_markers"),
        prompt_bootstrap_snippet=_str_or_empty(raw.get("prompt_bootstrap_snippet"), "prompt_bootstrap_snippet"),
        test_bootstrap_tool_defaults=_any_map_map(raw.get("test_bootstrap_tool_defaults"), "test_bootstrap_tool_defaults"),
        source_cache_config=_int_pair_map(raw.get("source_cache_config"), "source_cache_config"),
        source_cache_default_args=_any_map_map(raw.get("source_cache_default_args"), "source_cache_default_args"),
        postgres_schema=_str(raw.get("postgres_schema", "public"), "postgres_schema"),
        chroma_prefix=_str_or_empty(raw.get("chroma_prefix"), "chroma_prefix"),
        vault_dir=_str(raw.get("vault_dir") or str(Path.home() / "Morgoth" / "vault"), "vault_dir"),
    )


@lru_cache(maxsize=1)
def current_domain() -> Domain:
    """Return the loaded domain pack (memoized).

    Explicit Project selection owns the Domain. Standalone pack inspection can
    still use MORGOTH_DOMAIN; a running engine validates this against Project.
    Missing or malformed packs fail closed, with no semantic fallback.
    """
    if "MORGOTH_PROJECT" in os.environ:
        from core.project import current_project
        name = current_project().domain
    else:
        name = os.environ.get("MORGOTH_DOMAIN", DEFAULT_DOMAIN).strip() or DEFAULT_DOMAIN
    pack = _load(name)
    logger.debug("domain pack loaded: {}", pack.name)
    return pack


def reset_domain_cache() -> None:
    """Test hook — drop the memoized pack so a monkeypatched
    ``MORGOTH_DOMAIN`` is picked up on the next ``current_domain()``
    call. Does NOT rebind module-level constants that have already
    captured the pack at import (see the ONE-DOMAIN-PER-PROCESS
    invariant in the module docstring)."""
    current_domain.cache_clear()
