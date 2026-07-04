"""Injection audit — regression-locks the hardening of self_modify.reflect.

Every attack vector from the Phase B red-team is exercised here. Vectors
that were blocked pre-hardening (identifier regexes) are asserted to
still fail at spec validation; vectors that WERE live (description
docstring breakout, URL quote breakout) are asserted to either fail
spec validation OR render as inert string literals.

No test in this file ever executes a rendered file or submits a
proposal. The audit is pure string / AST analysis on the render output.
"""

from __future__ import annotations

import ast
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlparse
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from self_modify import reflect


DANGEROUS_CALL_TARGETS = {"__import__", "system", "eval", "exec", "compile"}
"""Names that MUST NOT appear as function calls anywhere in a rendered file."""

BASE_SPEC = {
    # tool_name tokens ("benign", "payload") intentionally match
    # description words so the coherence gate passes — this file's
    # subject is INJECTION, not name/content coherence.
    "tool_name": "get_benign_payload",
    "api_base_url": "https://api.example.com",
    "endpoint_path": "/v1/data",
    "digest_fields": ["field_a", "field_b", "field_c"],
    "description": "A benign description with no injection payload, over ten chars.",
    "rationale": "gap in current tools",
}


def _render(spec: dict[str, Any]) -> str:
    """Mirror the render call in reflect.run_reflection."""
    tool_name = spec["tool_name"]
    class_name = reflect._snake_to_class_name(tool_name)
    source_label = urlparse(spec["api_base_url"]).hostname or ""
    return reflect.TOOL_TEMPLATE.format(
        tool_name=tool_name,
        class_name=class_name,
        tool_name_repr=repr(tool_name),
        base_url_repr=repr(spec["api_base_url"]),
        endpoint_path_repr=repr(spec["endpoint_path"]),
        digest_fields_repr=repr(list(spec["digest_fields"])),
        description_repr=repr(spec["description"]),
        source_label_repr=repr(source_label),
    )


def _assert_no_dangerous_calls_in(code: str, marker: str | None = None) -> None:
    """Parse the rendered code and walk the AST looking for injection.

    Fails if the AST contains a Call whose target is one of the
    DANGEROUS_CALL_TARGETS names — those cannot appear in a
    correctly-rendered file, so their presence is a smoking gun.

    Optionally verify a specific attack-marker substring landed inside a
    string literal (not as executable code).
    """
    tree = ast.parse(code)
    dangerous: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = None
            if isinstance(func, ast.Name):
                name = func.id
            elif isinstance(func, ast.Attribute):
                name = func.attr
            if name in DANGEROUS_CALL_TARGETS:
                dangerous.append(ast.unparse(node))
    assert not dangerous, (
        f"rendered file contains dangerous calls: {dangerous!r}"
    )
    if marker is not None:
        # The payload text is present *only* inside a Constant string node.
        in_string = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if marker in node.value:
                    in_string = True
                    break
        assert in_string, (
            f"marker {marker!r} did not land inside a string literal — "
            "it may have escaped into executable position"
        )


# ---------- vectors already blocked at spec validation --------------------

def test_B1_digest_field_quote_breakout_rejected_at_spec() -> None:
    spec = dict(BASE_SPEC, digest_fields=['x"] = None\nimport os  # ', "y_ok", "z_ok"])
    err = reflect._spec_is_well_formed(spec)
    assert err is not None and "digest field" in err


def test_B2_digest_field_json_injection_rejected_at_spec() -> None:
    spec = dict(BASE_SPEC, digest_fields=[
        'a", "b": __import__("os").system("id"), "c', "y_ok", "z_ok"
    ])
    err = reflect._spec_is_well_formed(spec)
    assert err is not None and "digest field" in err


def test_B3_tool_name_breakout_rejected_at_spec() -> None:
    spec = dict(BASE_SPEC, tool_name='evil"; import os  # ')
    err = reflect._spec_is_well_formed(spec)
    assert err is not None and "tool_name" in err


def test_B3b_tool_name_uppercase_rejected_at_spec() -> None:
    """The SNAKE_CASE_RE explicitly disallows uppercase — regression-lock."""
    spec = dict(BASE_SPEC, tool_name="GetX")
    assert reflect._spec_is_well_formed(spec) is not None


# ---------- vectors that were LIVE — hardening test -----------------------

def test_B4a_description_docstring_breakout_neutralized_at_render() -> None:
    """Pre-hardening: this vector escaped into module-level code.
    Post-hardening: description goes through repr(); the payload lands
    inside a string literal and AST parses without any dangerous calls.
    """
    payload = 'hello"""; __import__("os").system("id"); """extra padding text'
    spec = dict(BASE_SPEC, description=payload)
    # Spec validation must pass (this is a legitimate-looking string —
    # the render layer is what neutralizes it).
    assert reflect._spec_is_well_formed(spec) is None
    rendered = _render(spec)
    _assert_no_dangerous_calls_in(rendered, marker="__import__")


def test_B4b_description_newline_injection_neutralized_at_render() -> None:
    """Description with real newlines is neutralized by repr — the
    resulting string literal uses escaped `\\n` and the file parses."""
    payload = 'outer\nimport os\nos.system("id")\ntrailing'
    spec = dict(BASE_SPEC, description=payload)
    assert reflect._spec_is_well_formed(spec) is None
    rendered = _render(spec)
    _assert_no_dangerous_calls_in(rendered, marker="import os")


def test_B5_url_quote_breakout_rejected_at_spec() -> None:
    """URL charset check catches `"` in the URL before render is reached."""
    spec = dict(BASE_SPEC, api_base_url='https://example.com"; import os; x = "')
    err = reflect._spec_is_well_formed(spec)
    assert err is not None and "RFC 3986" in err


def test_B5b_url_whitespace_rejected_at_spec() -> None:
    spec = dict(BASE_SPEC, api_base_url="https://example.com/ path")
    assert reflect._spec_is_well_formed(spec) is not None


def test_B5c_url_backtick_rejected_at_spec() -> None:
    spec = dict(BASE_SPEC, api_base_url="https://example.com/`x`")
    assert reflect._spec_is_well_formed(spec) is not None


def test_B5d_url_newline_rejected_at_spec() -> None:
    spec = dict(BASE_SPEC, api_base_url="https://example.com/x\ny")
    assert reflect._spec_is_well_formed(spec) is not None


def test_B6_endpoint_path_quote_breakout_rejected_at_spec() -> None:
    """endpoint_path is subject to the same charset check."""
    spec = dict(BASE_SPEC, endpoint_path='/api"; import os; x = "')
    assert reflect._spec_is_well_formed(spec) is not None


# ---------- clean-spec render sanity --------------------------------------

def test_clean_spec_renders_correctly_and_is_valid_python() -> None:
    rendered = _render(BASE_SPEC)
    tree = ast.parse(rendered)
    # Class exists.
    class_defs = [n for n in tree.body if isinstance(n, ast.ClassDef)]
    assert len(class_defs) == 1
    assert class_defs[0].name == "GetBenignPayloadTool"
    # Load-bearing constants are present.
    for expected in [
        "_BASE_URL", "_ENDPOINT_PATH", "_SOURCE_LABEL",
        "_TOOL_DESCRIPTION", "_DIGEST_FIELDS",
    ]:
        assert expected in rendered, f"constant {expected} missing from rendered file"
    # No dangerous calls.
    _assert_no_dangerous_calls_in(rendered)


def test_digest_fields_appear_in_rendered_constant() -> None:
    rendered = _render(BASE_SPEC)
    for field in BASE_SPEC["digest_fields"]:
        assert field in rendered


# ---------- HTML-entity normalization -------------------------------------

def test_html_normalize_decodes_amp_entity() -> None:
    assert reflect._html_normalize("a&amp;b=1&amp;c=2") == "a&b=1&c=2"


def test_html_normalize_survives_none_and_non_string() -> None:
    assert reflect._html_normalize(None) is None  # type: ignore[arg-type]
    assert reflect._html_normalize(42) == 42  # type: ignore[arg-type]


def test_spec_validation_accepts_url_with_amp_entity() -> None:
    """After normalization the URL is legal; the raw &amp; form would
    otherwise pass the charset regex too but the smoke test would 301.
    This test just confirms validation is entity-aware."""
    spec = dict(
        BASE_SPEC,
        api_base_url="https://api.example.com",
        endpoint_path="/v1?q=x&amp;y=1",
    )
    assert reflect._spec_is_well_formed(spec) is None


# ---------- name collision -------------------------------------------------

@pytest.mark.asyncio
async def test_tool_name_collision_rejected() -> None:
    """A spec whose tool_name matches an already-registered tool is
    rejected at run_reflection before smoke or submit is reached."""
    import json as _json

    pm = MagicMock()
    store_mock = MagicMock()
    store_mock.count_by_status_and_author = AsyncMock(return_value=0)

    # Description mentions "price" so the coherence gate (which runs
    # before collision) passes — we're testing collision, not coherence.
    colliding = dict(
        BASE_SPEC,
        tool_name="get_crypto_price",
        description="Fetch the current USD price of a crypto asset.",
    )

    class _FakeTool:
        def __init__(self, name: str) -> None:
            self.name = name

    with patch("self_modify.reflect.P.ProposalStore", return_value=store_mock), \
         patch("self_modify.reflect._build_context",
               AsyncMock(return_value={"tools_block": "", "objectives_block": "", "theses_block": ""})), \
         patch("scripts.compile_wiki._registered_tools_offline",
               return_value=[_FakeTool("get_crypto_price"), _FakeTool("get_news")]):
        llm = MagicMock()
        llm.chat = AsyncMock(return_value=SimpleNamespace(
            message=SimpleNamespace(content=_json.dumps(colliding))
        ))
        result = await reflect.run_reflection(
            SimpleNamespace(permissions=SimpleNamespace(
                permissions=SimpleNamespace(can_self_modify=True))),
            pm, llm,
        )
    assert result["outcome"] == "rejected_collision"
    assert "get_crypto_price" in result["reason"]


# ---------- digest uniqueness ---------------------------------------------

def test_duplicate_digest_fields_rejected() -> None:
    spec = dict(BASE_SPEC, digest_fields=["a", "a", "b"])
    err = reflect._spec_is_well_formed(spec)
    assert err is not None and "unique" in err
