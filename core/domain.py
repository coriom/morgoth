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
import math
import re
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
class MetricCollection:
    """One validated tool poll and its logical metric field references."""

    source: str
    interval_secs: int
    fields: tuple[str, ...]
    args: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FactCapture:
    """Declarative extraction of one scalar temporal fact from a tool result."""

    kind: str
    metric: str
    field: str
    entity: str
    valid_at: str
    dimensions: dict[str, str]
    records: str = ""
    source_updated_at: str = ""
    source_record_id: str = ""


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
    entities: dict[str, tuple[str, ...]] = field(default_factory=dict)
    entity_required_phrases: tuple[str, ...] = ()
    entity_excluded_phrases: tuple[str, ...] = ()
    semantic_classes: dict[str, tuple[str, ...]] = field(default_factory=dict)
    semantic_windows_hours: dict[str, float] = field(default_factory=dict)
    semantic_window_env: dict[str, str] = field(default_factory=dict)
    metric_collections: dict[str, MetricCollection] = field(default_factory=dict)
    fact_captures: dict[str, tuple[FactCapture, ...]] = field(default_factory=dict)
    scorers: dict[str, str] = field(default_factory=dict)
    bootstrap_recurring_task: dict[str, str] = field(default_factory=dict)
    thesis_phantom_example: str = ""
    thesis_subject_example: str = ""
    rail_tools: tuple[str, ...] = ()



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


def _positive_float_map(x: Any, path: str) -> dict[str, float]:
    if x is None:
        return {}
    if not isinstance(x, dict):
        _fail(path, "dict[str, positive hours]", x)
    out = {}
    for key, value in x.items():
        name = _str(key, f"{path}.<key>")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            _fail(f"{path}.{name}", "positive finite hours", value)
        out[name] = float(value)
    return out


def _metric_collections(x: Any, field_map: dict[str, str],
                        metric_names: dict[str, str]) -> dict[str, MetricCollection]:
    if x is None:
        return {}
    if not isinstance(x, dict):
        _fail("metric_collections", "mapping of tool specifications", x)
    result: dict[str, MetricCollection] = {}
    assigned: set[str] = set()
    for key, item in x.items():
        tool = _str(key, "metric_collections.<tool>")
        if not tool or not isinstance(item, dict) or set(item) != {"source", "interval_secs", "fields", "args"}:
            _fail(f"metric_collections.{tool}", "{source, interval_secs, fields, args}", item)
        source = _str(item["source"], f"metric_collections.{tool}.source")
        interval = item["interval_secs"]
        fields = _str_list(item["fields"], f"metric_collections.{tool}.fields")
        args = _any_map_map({tool: item["args"]}, "metric_collections.args")[tool]
        if not source or type(interval) is not int or interval < 60 or not fields or len(set(fields)) != len(fields):
            _fail(f"metric_collections.{tool}", "nonempty source, interval >= 60, unique fields", item)
        for name in fields:
            if name not in field_map or field_map[name] not in metric_names or name in assigned:
                raise DomainPackError(f"metric_collections.{tool}: unknown or duplicate metric field {name!r}")
            assigned.add(name)
        result[tool] = MetricCollection(source, interval, fields, args)
    if set(field_map) != assigned:
        raise DomainPackError("metric_collections must assign every metric_field_map field once")
    return result


_FACT_NAME = re.compile(r"[a-z][a-z0-9_]*\Z")


def _fact_captures(raw: Any, rail: tuple[str, ...], sources: dict[str, str],
                   units: dict[str, dict[str, str]]) -> dict[str, tuple[FactCapture, ...]]:
    """Validate capture selectors and their existing source/unit authorities."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise DomainPackError("fact_captures must map active tools to rules")
    result: dict[str, tuple[FactCapture, ...]] = {}
    allowed = {"kind", "metric", "field", "entity", "valid_at", "dimensions",
               "records", "source_updated_at", "source_record_id"}
    for tool, rules in raw.items():
        if tool not in rail or tool not in sources or not isinstance(rules, list) or not rules:
            raise DomainPackError("fact_captures requires an active sourced tool and nonempty rules")
        parsed: list[FactCapture] = []
        seen: set[tuple[str, str, str]] = set()
        for rule in rules:
            if not isinstance(rule, dict) or not {"kind", "metric", "field", "entity", "valid_at", "dimensions"} <= set(rule) or set(rule) - allowed:
                raise DomainPackError("invalid fact capture rule")
            if any(not isinstance(v, str) or (v and not _FACT_NAME.fullmatch(v))
                   or (not v and k not in {"records", "source_updated_at", "source_record_id"})
                   for k, v in rule.items() if k != "dimensions"):
                raise DomainPackError("invalid fact capture identifier")
            if rule["kind"] not in {"prediction", "observation"} or rule["field"] not in units.get(tool, {}):
                raise DomainPackError("fact capture kind or unit field is unknown")
            dimensions = rule["dimensions"]
            if (not isinstance(dimensions, dict) or not dimensions or len(dimensions) > 12
                    or any(not isinstance(k, str) or not isinstance(v, str)
                           or not _FACT_NAME.fullmatch(k) or not _FACT_NAME.fullmatch(v)
                           for k, v in dimensions.items())):
                raise DomainPackError("invalid fact capture dimensions")
            key = (rule["kind"], rule["metric"], rule["field"])
            if key in seen:
                raise DomainPackError("duplicate fact capture rule")
            seen.add(key)
            parsed.append(FactCapture(**rule))
        result[tool] = tuple(parsed)
    return result


class _UniqueKeyLoader(yaml.SafeLoader):
    """Reject ambiguous YAML mappings before their duplicate keys disappear."""


def _unique_mapping(loader: _UniqueKeyLoader, node: yaml.MappingNode) -> dict:
    result: dict = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=True)
        if key in result:
            raise DomainPackError(f"duplicate domain mapping key: {key}")
        result[key] = loader.construct_object(value_node, deep=True)
    return result


_UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_mapping)


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
    raw = yaml.load(path.read_text(encoding="utf-8"), Loader=_UniqueKeyLoader) or {}
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
    price_tokens = _str_list(raw.get("price_class_tokens"), "price_class_tokens")
    semantic_classes = _str_list_map(raw.get("semantic_classes"), "semantic_classes")
    if price_tokens and "price" in semantic_classes:
        raise DomainPackError("price_class_tokens and semantic_classes.price duplicate authority")
    if price_tokens:
        semantic_classes["price"] = price_tokens  # old-pack input adapter only
    windows = _positive_float_map(raw.get("semantic_windows_hours"), "semantic_windows_hours")
    window_env = _str_map(raw.get("semantic_window_env"), "semantic_window_env")
    if raw.get("semantic_classes") is not None or windows or window_env:
        if "default" not in windows or "default" in semantic_classes:
            raise DomainPackError("semantic_windows_hours.default is required; default is not a class")
        if set(semantic_classes) - set(windows) or set(window_env) - set(windows):
            raise DomainPackError("each semantic class/environment override needs a declared window")
        for key, value in window_env.items():
            if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", value):
                raise DomainPackError(f"semantic_window_env.{key}: invalid environment name")
    entities = _str_list_map(raw.get("entities"), "entities")
    seen_aliases: set[str] = set()
    for entity, aliases in entities.items():
        if not entity or not aliases or any(not alias.strip() for alias in aliases):
            raise DomainPackError("entities need nonempty names and aliases")
        for alias in aliases:
            if alias.lower() in seen_aliases:
                raise DomainPackError("duplicate entity alias")
            seen_aliases.add(alias.lower())
    metric_names = _str_map(raw.get("metric_names"), "metric_names")
    metric_field_map = _str_map(raw.get("metric_field_map"), "metric_field_map")
    collections = _metric_collections(raw.get("metric_collections"), metric_field_map, metric_names)
    if collections and raw.get("metric_source_tool"):
        raise DomainPackError("metric_source_tool and metric_collections duplicate authority")
    scorers = _str_map(raw.get("scorers"), "scorers")
    if scorers:
        from analysis.scorer_registry import registered_scorers
        if any(not _FACT_NAME.fullmatch(role) for role in scorers):
            raise DomainPackError("invalid scorer role identifier")
        if not set(scorers.values()) <= registered_scorers():
            raise DomainPackError("unknown scorer implementation")
    recurring = _str_map(raw.get("bootstrap_recurring_task"), "bootstrap_recurring_task")
    if recurring and (set(recurring) != {"description", "cron"} or not all(recurring.values())):
        raise DomainPackError("bootstrap_recurring_task requires description and cron")
    source_cache_config = _int_pair_map(raw.get("source_cache_config"), "source_cache_config")
    source_cache_default_args = _any_map_map(raw.get("source_cache_default_args"), "source_cache_default_args")
    bootstrap_defaults = _any_map_map(raw.get("test_bootstrap_tool_defaults"), "test_bootstrap_tool_defaults")
    scope_source_tool = _str_or_empty(raw.get("scope_source_tool"), "scope_source_tool")
    legacy_metric_tool = _str_or_empty(raw.get("metric_source_tool"), "metric_source_tool")
    rail = raw.get("rail", {"tools": []})
    if not isinstance(rail, dict) or set(rail) != {"tools"}:
        raise DomainPackError("rail must contain only an explicit tools list")
    rail_tools = _str_list(rail["tools"], "rail.tools")
    from core.tool_rail import ToolRailError, installed_catalog, validate_declared_rail
    try:
        validate_declared_rail(rail_tools, installed_catalog())
    except ToolRailError as exc:
        raise DomainPackError(str(exc)) from None
    fact_captures = _fact_captures(raw.get("fact_captures"), rail_tools,
                                  _str_map(raw.get("tool_sources"), "tool_sources"),
                                  _str_map_map(raw.get("field_units"), "field_units"))
    acquisition_maps = {
        "source_cache_config": source_cache_config,
        "source_cache_default_args": source_cache_default_args,
        "metric_collections": collections,
        "test_bootstrap_tool_defaults": bootstrap_defaults,
    }
    for map_name, refs in acquisition_maps.items():
        inactive = set(refs) - set(rail_tools)
        if inactive:
            raise DomainPackError(f"{map_name} references inactive tools: {sorted(inactive)}")
    if scope_source_tool and scope_source_tool not in rail_tools:
        raise DomainPackError("scope_source_tool references inactive tool")
    if legacy_metric_tool and legacy_metric_tool not in rail_tools:
        raise DomainPackError("metric_source_tool references inactive tool")
    return Domain(
        name=_str(raw.get("name") or name, "name"),
        tagline=_str_or_empty(raw.get("tagline"), "tagline"),
        generic_subject_tokens=_str_list(raw.get("generic_subject_tokens"), "generic_subject_tokens"),
        price_class_tokens=semantic_classes.get("price", ()),
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
        scope_source_tool=scope_source_tool,
        field_phrases=_str_list_map_map(raw.get("field_phrases"), "field_phrases"),
        ambiguous_phrases=_str_list(raw.get("ambiguous_phrases"), "ambiguous_phrases"),
        single_numeric_field=_str_map(raw.get("single_numeric_field"), "single_numeric_field"),
        timestamp_like_fields=_str_list(raw.get("timestamp_like_fields"), "timestamp_like_fields"),
        metric_names=metric_names,
        metric_field_map=metric_field_map,
        metric_source_tool=next(iter(collections), "") if len(collections) == 1 else legacy_metric_tool,
        backtest_subject_markers=_str_list_map(raw.get("backtest_subject_markers"), "backtest_subject_markers"),
        prompt_bootstrap_snippet=_str_or_empty(raw.get("prompt_bootstrap_snippet"), "prompt_bootstrap_snippet"),
        test_bootstrap_tool_defaults=bootstrap_defaults,
        source_cache_config=source_cache_config,
        source_cache_default_args=source_cache_default_args,
        postgres_schema=_str(raw.get("postgres_schema", "public"), "postgres_schema"),
        chroma_prefix=_str_or_empty(raw.get("chroma_prefix"), "chroma_prefix"),
        vault_dir=_str(raw.get("vault_dir") or str(Path.home() / "Morgoth" / "vault"), "vault_dir"),
        entities=entities,
        entity_required_phrases=_str_list(raw.get("entity_required_phrases"), "entity_required_phrases"),
        entity_excluded_phrases=_str_list(raw.get("entity_excluded_phrases"), "entity_excluded_phrases"),
        semantic_classes=semantic_classes,
        semantic_windows_hours=windows,
        semantic_window_env=window_env,
        metric_collections=collections,
        fact_captures=fact_captures,
        scorers=scorers,
        bootstrap_recurring_task=recurring,
        thesis_phantom_example=_str_or_empty(raw.get("thesis_phantom_example"), "thesis_phantom_example"),
        thesis_subject_example=_str_or_empty(raw.get("thesis_subject_example"), "thesis_subject_example"),
        rail_tools=rail_tools,
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


def resolve_subject_entity(subject: str, domain: Domain | None = None) -> str | None:
    """Resolve a Domain-declared entity; no entity declaration means no match."""
    pack = domain or current_domain()
    if not isinstance(subject, str) or not subject:
        return None
    low = subject.lower()
    if (pack.entity_required_phrases and not any(p in low for p in pack.entity_required_phrases)):
        return None
    if any(p in low for p in pack.entity_excluded_phrases):
        return None
    return next((entity for entity, aliases in pack.entities.items()
                 if any(alias.lower() in low for alias in aliases)), None)


def subject_semantic_class(subject: str, domain: Domain | None = None) -> str:
    """Classify a subject by declared tokens, falling to declared default."""
    pack = domain or current_domain()
    low = subject.lower() if isinstance(subject, str) else ""
    return next((name for name, tokens in pack.semantic_classes.items()
                 if low and any(token.lower() in low for token in tokens)), "default")


def semantic_window_hours(semantic_class: str, domain: Domain | None = None) -> float:
    """Return a declared window; invalid class or override fails closed."""
    pack = domain or current_domain()
    try:
        base = pack.semantic_windows_hours[semantic_class]
    except KeyError:
        raise DomainPackError(f"undeclared semantic window {semantic_class!r}") from None
    env_name = pack.semantic_window_env.get(semantic_class)
    if not env_name or not os.environ.get(env_name):
        return base
    try:
        value = float(os.environ[env_name])
        if math.isfinite(value) and value > 0:
            return value
    except ValueError:
        pass
    raise DomainPackError(f"invalid semantic window override {env_name}") from None
