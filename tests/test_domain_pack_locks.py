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
