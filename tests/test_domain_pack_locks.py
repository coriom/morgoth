"""Grep-locks for the domain-pack refactor (chantier 1 of 5).

Every DATA constant from the coupling-map audit was moved out of
core/ / analysis/ / self_modify/ into ``domains/crypto/domain.yaml``.
These tests refuse to let those literals reappear outside
``domains/`` — a future edit that inlines "btc dominance" or
"bitcoin_dominance_percentage" back into code fails here first.

Docstrings, comments, tests, and the tools/data_feeds/ manifest are
excluded from the grep (they legitimately mention crypto strings).
The rule targets executable constant tables: any {"…", "…"},
frozenset({…}), or `= "btc"` reintroducing an audited literal in a
non-test, non-docstring line.
"""

from __future__ import annotations

import ast
import pathlib
from typing import Iterable

import pytest


ROOT = pathlib.Path(__file__).resolve().parent.parent
DOMAINS_DIR = ROOT / "domains"

# Directories that legitimately hold crypto strings.
_ALLOWED_DIRS = {
    ROOT / "domains",
    ROOT / "tests",
    ROOT / "tools" / "data_feeds",   # data-feed tools; extracted separately.
    ROOT / ".git",
    ROOT / ".venv",
    ROOT / "logs",
    ROOT / "data",
    ROOT / "vault",
    ROOT / "backups",
    ROOT / "docs",
    ROOT / "morgoth_ui",
    ROOT / "analysis" / "__pycache__",
}


def _iter_source_files() -> Iterable[pathlib.Path]:
    for py in ROOT.rglob("*.py"):
        try:
            py.resolve().relative_to(ROOT)
        except ValueError:
            continue
        if any(str(py).startswith(str(d) + "/") or py == d for d in _ALLOWED_DIRS):
            continue
        yield py


def _executable_source(text: str) -> str:
    """Strip module + function + class docstrings so the grep does
    not self-trip on rationale wording (comments removed via ast is
    non-trivial — we skip lines starting with ``#`` in the walk)."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return text
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Module, ast.ClassDef)
        ):
            if (
                node.body
                and isinstance(node.body[0], ast.Expr)
                and isinstance(node.body[0].value, ast.Constant)
                and isinstance(node.body[0].value.value, str)
            ):
                node.body = node.body[1:]  # type: ignore[attr-defined]
    return ast.unparse(tree)


def _lines_without_comments(src: str) -> list[str]:
    out: list[str] = []
    for line in src.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        out.append(line)
    return out


# ---------- individual locks ------------------------------------------------

def _grep_for(needle: str) -> list[tuple[str, str]]:
    hits: list[tuple[str, str]] = []
    for py in _iter_source_files():
        text = py.read_text(encoding="utf-8")
        body = _executable_source(text)
        for line in _lines_without_comments(body):
            if needle in line:
                hits.append((str(py.relative_to(ROOT)), line.strip()[:140]))
    return hits


@pytest.mark.parametrize("literal", [
    # generic_subject_tokens
    '"cryptocurrency"',
    # price_class_tokens fingerprint — the 4-tuple never appears outside the pack.
    # subject_prefix_stopwords fingerprint
    '("global", "crypto", "the")',
    # scope inline tokens
    '"trading volume", "market_cap", "price"',
    # metric_field_map source keys
    '"bitcoin_dominance_percentage": ',
    # ambiguous_phrases fingerprint
    '{"value", "rate", "change", "price", "volume"}',
    # timestamp_like_fields fingerprint
    '"nextFundingTime"',
    # tool_served_phrases fingerprint (get_bitcoin_futures_funding entry)
    '"perpetual funding"',
])
def test_domain_literal_absent_from_code(literal: str) -> None:
    hits = _grep_for(literal)
    assert not hits, (
        f"literal {literal!r} moved to domains/crypto/domain.yaml — "
        f"any code reintroducing it must go through core.domain.current_domain(): {hits}"
    )


def test_domain_pack_exists_and_loads() -> None:
    from core.domain import current_domain, DEFAULT_DOMAIN
    d = current_domain()
    assert d.name == DEFAULT_DOMAIN
    # Non-empty sanity: every field we rely on has at least one entry.
    assert d.generic_subject_tokens, d
    assert d.price_class_tokens, d
    assert d.tool_served_phrases, d
    assert d.rail_tool_fields, d
    assert d.field_phrases, d
    assert d.metric_field_map, d
    assert d.source_cache_config, d


def test_downstream_constants_route_through_domain() -> None:
    """The bound constants MUST equal the pack values — a future edit
    that hard-codes a different value here silently would drift from
    the pack and break domain switching."""
    from core.domain import current_domain
    from analysis.campaign_quality import (
        TOOL_SERVED_PHRASES, RAIL_TOOL_FIELDS, METRIC_FAMILIES,
        _BULLISH_WORDS, _BEARISH_WORDS, _PHRASE_STOPWORDS,
    )
    from core.campaign import _GENERIC_SUBJECT_TOKENS, _STOPWORDS
    from core.contradictions import PRICE_CLASS_TOKENS, _SUBJECT_PREFIX_STOPWORDS
    from core.field_confusion import (
        FIELD_PHRASES, AMBIGUOUS_PHRASES,
        SINGLE_NUMERIC_FIELD, TIMESTAMP_LIKE_FIELDS,
    )
    from self_modify.reflect import _NAME_STOPWORDS
    from core.source_cache import SOURCE_CACHE_CONFIG, SOURCE_DEFAULT_ARGS
    d = current_domain()

    assert dict(TOOL_SERVED_PHRASES) == dict(d.tool_served_phrases)
    assert dict(METRIC_FAMILIES) == dict(d.metric_families)
    assert _BULLISH_WORDS == frozenset(d.bullish_words)
    assert _BEARISH_WORDS == frozenset(d.bearish_words)
    assert _PHRASE_STOPWORDS == frozenset(d.phrase_stopwords)
    assert _GENERIC_SUBJECT_TOKENS == frozenset(d.generic_subject_tokens)
    assert _STOPWORDS == frozenset(d.campaign_title_stopwords)
    assert PRICE_CLASS_TOKENS == frozenset(d.price_class_tokens)
    assert _SUBJECT_PREFIX_STOPWORDS == tuple(d.subject_prefix_stopwords)
    assert AMBIGUOUS_PHRASES == frozenset(d.ambiguous_phrases)
    assert TIMESTAMP_LIKE_FIELDS == frozenset(d.timestamp_like_fields)
    assert SINGLE_NUMERIC_FIELD == dict(d.single_numeric_field)
    assert _NAME_STOPWORDS == frozenset(d.name_stopwords)
    assert dict(SOURCE_CACHE_CONFIG) == dict(d.source_cache_config)
    assert dict(SOURCE_DEFAULT_ARGS) == dict(d.source_cache_default_args)
    # Field phrases: nested dict compare.
    for tool, fields in d.field_phrases.items():
        assert dict(FIELD_PHRASES[tool]) == dict(fields), tool
    # RAIL_TOOL_FIELDS: values are frozenset, pack gives tuple.
    for tool, cols in d.rail_tool_fields.items():
        assert RAIL_TOOL_FIELDS[tool] == frozenset(cols), tool


# ---------- one-domain-per-process invariant --------------------------------

def test_one_domain_per_process_invariant(monkeypatch) -> None:
    """The pack is read at IMPORT time and its values are bound to
    module-level constants (``analysis.campaign_quality.RAIL_TOOL_FIELDS``,
    ``core.source_cache.SOURCE_CACHE_CONFIG``, …). Those bindings do
    NOT observe subsequent changes to ``MORGOTH_DOMAIN`` — even after
    ``reset_domain_cache()``. Switching domain therefore REQUIRES a
    fresh process. This test locks that shape.

    Rationale: a runtime domain-switch would leave half the process on
    the old pack (already-imported modules) and half on the new pack
    (freshly-imported ones), a class of bug that is silently wrong
    rather than loudly broken. Better to enforce fresh-process.
    """
    from core.domain import (
        current_domain,
        reset_domain_cache,
        DomainPackError,
    )
    from analysis import campaign_quality as cq

    d0 = current_domain()
    baseline_rail = dict(cq.RAIL_TOOL_FIELDS)
    baseline_pack_rail = {k: frozenset(v) for k, v in d0.rail_tool_fields.items()}
    assert baseline_rail == baseline_pack_rail

    # Switch the env AND reset the cache — mimic a would-be runtime
    # switch. reset_domain_cache() drops the memoized pack, so the
    # NEXT current_domain() call would try to load a different pack.
    monkeypatch.setenv("MORGOTH_DOMAIN", "__nonexistent_test_domain__")
    reset_domain_cache()
    try:
        with pytest.raises(DomainPackError):
            current_domain()  # strict — no fallback
        # …but module-level constants are STILL bound to the pre-swap
        # values. A live scorer using cq.RAIL_TOOL_FIELDS would not see
        # any change — this is the invariant.
        assert cq.RAIL_TOOL_FIELDS is baseline_rail or \
            dict(cq.RAIL_TOOL_FIELDS) == baseline_rail, (
                "module-level constant unexpectedly rebound after "
                "reset_domain_cache — the invariant is that changing "
                "domains requires a fresh process"
            )
    finally:
        monkeypatch.delenv("MORGOTH_DOMAIN", raising=False)
        reset_domain_cache()


# ---------- strict validation + unknown domain refusal ----------------------

def _write_pack(root, name: str, body: str) -> None:
    d = root / "domains" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "domain.yaml").write_text(body, encoding="utf-8")


def _fresh_loader(tmp_path, monkeypatch):
    """Return a ``core.domain`` module bound to a ``domains/`` root
    under ``tmp_path`` — lets each test author its own pack."""
    import importlib
    import sys as _sys
    monkeypatch.setenv("MORGOTH_DOMAIN", "test_pack")
    if "core.domain" in _sys.modules:
        del _sys.modules["core.domain"]
    import core.domain as m
    importlib.reload(m)
    monkeypatch.setattr(m, "_DOMAINS_ROOT", tmp_path / "domains")
    m.reset_domain_cache()
    return m


def test_unknown_domain_refuses_to_start(tmp_path, monkeypatch) -> None:
    """A fresh process with ``MORGOTH_DOMAIN=<missing>`` MUST raise
    DomainPackError — never silently fall back to crypto. Silent
    fallback would leave a typo running the wrong domain's stopwords
    and metric names while the underlying Postgres / Chroma remain
    scoped elsewhere. Refusing to boot is the loud-and-correct
    behaviour."""
    m = _fresh_loader(tmp_path, monkeypatch)
    # Note: NO pack is written under tmp_path/domains — the lookup
    # must fail even though crypto EXISTS in the real repo. Import
    # the exception class from the RELOADED module — importlib.reload
    # produces a new class object; the pre-reload class won't match.
    with pytest.raises(m.DomainPackError) as ei:
        m.current_domain()
    assert "test_pack" in str(ei.value)
    assert "not found" in str(ei.value)


def _minimal_valid_body() -> str:
    return (
        "name: test_pack\n"
        "rail: {tools: [t]}\n"
        "generic_subject_tokens: [x]\n"
        "price_class_tokens: [x]\n"
        "subject_prefix_stopwords: [x]\n"
        "name_stopwords: [x]\n"
        "phrase_stopwords: [x]\n"
        "campaign_title_stopwords: [x]\n"
        "tool_served_phrases: {t: [p]}\n"
        "rail_tool_fields: {t: [c]}\n"
        "bullish_words: [x]\n"
        "bearish_words: [x]\n"
        "metric_families: {m: [k]}\n"
        "scope_asset_metric_tokens: [x]\n"
        "scope_asset_subject_tokens: [x]\n"
        "scope_dominance_exception_tokens: [x]\n"
        "scope_source_tool: t\n"
        "field_phrases: {t: {f: [p]}}\n"
        "ambiguous_phrases: [x]\n"
        "single_numeric_field: {t: f}\n"
        "timestamp_like_fields: [x]\n"
        "metric_names: {a: b}\n"
        "metric_field_map: {a: b}\n"
        "metric_source_tool: t\n"
        "backtest_subject_markers: {m: [k]}\n"
        "source_cache_config: {t: [60, 120]}\n"
        "source_cache_default_args: {t: {a: 1}}\n"
    )


@pytest.mark.parametrize("mutation,field_name", [
    # Norway problem — literal `on` becomes True.
    ("phrase_stopwords:\n  - on\n", "phrase_stopwords"),
    # yes → True.
    ("phrase_stopwords:\n  - yes\n", "phrase_stopwords"),
    # off → False.
    ("phrase_stopwords:\n  - off\n", "phrase_stopwords"),
    # bare null token.
    ("phrase_stopwords:\n  - null\n", "phrase_stopwords"),
    # An integer where a string was declared.
    ("phrase_stopwords:\n  - 42\n", "phrase_stopwords"),
    # bool in map value.
    ("metric_names:\n  a: yes\n", "metric_names"),
    # non-list where list expected.
    ("bullish_words: not_a_list\n", "bullish_words"),
    # source_cache_config with a bool masquerading as int.
    ("source_cache_config:\n  t: [true, 60]\n", "source_cache_config.t"),
])
def test_strict_type_validation_rejects_coerced_scalars(
    tmp_path, monkeypatch, mutation, field_name,
) -> None:
    """Every pack field has a declared type. YAML 1.1 auto-coercions
    (on/off/yes/no/null and bare numerics) MUST NOT survive the
    validator. The error names the field and the offending value."""
    m = _fresh_loader(tmp_path, monkeypatch)
    from core import tool_rail
    installed = tool_rail.installed_catalog()
    monkeypatch.setattr(tool_rail, "installed_catalog", lambda: {
        **installed, "t": tool_rail.ToolKind(False, True, True),
    })
    body = _minimal_valid_body()
    # Replace the field being mutated so the injected mutation is the
    # only source of that field.
    key = mutation.split(":", 1)[0]
    lines = [ln for ln in body.splitlines()
             if not ln.startswith(key + ":") and not ln.startswith(key + "\n")]
    body = "\n".join(lines) + "\n" + mutation
    _write_pack(tmp_path, "test_pack", body)
    with pytest.raises(m.DomainPackError) as ei:
        m.current_domain()
    assert field_name in str(ei.value), (
        f"error message must name the offending field {field_name!r}, "
        f"got: {ei.value}"
    )


def test_strict_validator_accepts_current_crypto_pack() -> None:
    """The current crypto pack must load cleanly through the strict
    validator — nothing regresses."""
    from core.domain import current_domain, reset_domain_cache
    reset_domain_cache()
    d = current_domain()
    assert d.name == "crypto"
