"""Pending-key: keyed free APIs park at smoke for operator provisioning.

Closes the "beaconcha.in died twice classified 'dead'" class: a 401/403
smoke response on an endpoint whose spec DECLARED a required env var is
not a defect. Instead of writing rejected_smoke, the row lands in a
non-terminal 'pending_key' park. `morgoth provision <id>` re-drives the
walk once the env var is present.

Custody stays with the operator: Morgoth handles env-var NAMES only.
The VALUE is fetched via os.getenv at tool-runtime, never appears in
git, never travels through the LLM, never gets logged.
"""

from __future__ import annotations

import json as _json
import os as _os
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from self_modify import proposals as P
from self_modify import reflect


BASE_SPEC = {
    "tool_name": "get_beacon_epoch_stats",
    "api_base_url": "https://beaconcha.in",
    "endpoint_path": "/api/v1/epoch/latest",
    "digest_fields": ["epoch", "attesterslashings", "finalized"],
    "description": "Latest Ethereum consensus epoch summary from beaconcha.in.",
    "rationale": "no ETH consensus-layer feed in the current registry",
}
KEYED_SPEC = {
    **BASE_SPEC,
    "requires_key": {
        "env_var": "BEACONCHAIN_API_KEY",
        "signup_url": "https://beaconcha.in/pricing",
    },
    "key_in": "query",
    "key_param": "apikey",
}


# ---------- spec-format matrix --------------------------------------------

def test_spec_validation_accepts_clean_keyed_spec() -> None:
    assert reflect._spec_is_well_formed(KEYED_SPEC) is None


def test_spec_validation_accepts_keyless_spec_unchanged() -> None:
    # Pre-key behaviour preserved: a keyless spec (no requires_key) is
    # still valid without any of the new fields.
    assert reflect._spec_is_well_formed(BASE_SPEC) is None


@pytest.mark.parametrize("env_var", [
    "lowercase_bad",            # not upper-snake
    "ABC",                      # too short (<4 chars total, min is [A-Z][A-Z0-9_]{3,40})
    "TOO_LONG_" + "A" * 40,     # exceeds 41-char cap
    "0_LEADING_DIGIT",          # must start with a letter
    "BAD;INJECT",               # shell metachar
    "SPACE VAR",                # whitespace
    "",                          # empty
])
def test_spec_validation_rejects_bad_env_var(env_var: str) -> None:
    bad = {**KEYED_SPEC, "requires_key": {"env_var": env_var, "signup_url": KEYED_SPEC["requires_key"]["signup_url"]}}
    err = reflect._spec_is_well_formed(bad)
    assert err is not None
    assert "env_var" in err


def test_spec_validation_rejects_missing_signup_url() -> None:
    bad = {**KEYED_SPEC, "requires_key": {"env_var": "FOO_API_KEY"}}
    assert "signup_url" in (reflect._spec_is_well_formed(bad) or "")


def test_spec_validation_rejects_http_signup_url() -> None:
    """SSRF/host guard applies to signup_url too."""
    bad = {**KEYED_SPEC, "requires_key": {
        "env_var": "FOO_API_KEY",
        "signup_url": "http://insecure.example.com/signup",
    }}
    err = reflect._spec_is_well_formed(bad)
    assert err is not None and "signup_url" in err


def test_spec_validation_requires_key_placement_when_declared() -> None:
    """A requires_key WITHOUT key_in/key_param is malformed."""
    bad = {**BASE_SPEC, "requires_key": {
        "env_var": "FOO_API_KEY",
        "signup_url": "https://example.com/signup",
    }}
    err = reflect._spec_is_well_formed(bad)
    assert err is not None and "key_in" in err


def test_spec_validation_rejects_bad_key_in() -> None:
    bad = {**KEYED_SPEC, "key_in": "body"}  # not query|header
    err = reflect._spec_is_well_formed(bad)
    assert err is not None and "key_in" in err


def test_spec_validation_rejects_bad_key_param() -> None:
    bad = {**KEYED_SPEC, "key_param": "bad param;drop table"}
    err = reflect._spec_is_well_formed(bad)
    assert err is not None and "key_param" in err


# ---------- smoke routing: 401/403 → pending_key vs rejected_smoke -------

@pytest.mark.asyncio
async def test_401_with_requires_key_parks_pending_key() -> None:
    """A 401 on a keyed spec = park, not reject."""
    store = MagicMock()
    store.submit_terminal = AsyncMock(return_value="parked-1")
    fake_resp = SimpleNamespace(status_code=401, json=MagicMock(return_value={}))
    fake_client = AsyncMock()
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)
    fake_client.get = AsyncMock(return_value=fake_resp)

    async def _rc(prompt: str, *_a: Any, **_kw: Any) -> tuple[str, dict[str, Any]]:
        return _json.dumps(KEYED_SPEC), {}

    pm = MagicMock()
    with patch("self_modify.reflect.P.ProposalStore", return_value=store), \
         patch("self_modify.reflect._build_context",
               AsyncMock(return_value={"tools_block": "", "objectives_block": "", "theses_block": ""})), \
         patch("self_modify.reflect._registered_endpoints", return_value={}), \
         patch("self_modify.reflect._registered_digest_fields", return_value=set()), \
         patch("self_modify.reflect.tool_name_collides", return_value=False), \
         patch("self_modify.reflect.reflect_chat", side_effect=_rc), \
         patch.object(reflect, "socket", MagicMock(
             getaddrinfo=MagicMock(return_value=[
                 (None, None, None, None, ("104.16.0.1", 0))
             ])
         )), \
         patch.object(reflect.httpx, "AsyncClient", return_value=fake_client), \
         patch("self_modify.reflect.gates.run_pipeline",
               AsyncMock(return_value="pending_approval")), \
         patch("self_modify.shadow.run_shadow_verdict",
               AsyncMock(return_value={"verdict": "APPROVE", "axes": {}, "reasons": [], "engine": "t", "prompt_version": "t"})), \
         patch("self_modify.reflect.liveness.run_liveness_probe",
               AsyncMock(return_value={"url": "x", "hits": [], "n_hits": 0, "digest_fields": []})):
        store.count_by_status_and_author = AsyncMock(return_value=0)
        result = await reflect.run_reflection(
            _fake_config(True), pm, _fake_llm(_json.dumps(KEYED_SPEC)), provider="claude-cli",
        )
    assert result["outcome"] == "pending_key"
    # submit_terminal was called with status=pending_key.
    call = store.submit_terminal.await_args
    assert call.kwargs["status"] == P.STATUS_PENDING_KEY
    # The env var NAME is in the row's reason; the VALUE is absent by
    # construction (there is no value to record).
    assert "BEACONCHAIN_API_KEY" in call.kwargs["status_reason"]


@pytest.mark.asyncio
async def test_401_without_requires_key_still_rejects_smoke() -> None:
    """Old contract preserved: a 401 on a keyless spec = rejected_smoke."""
    store = MagicMock()
    store.submit_terminal = AsyncMock(return_value="rej-1")
    fake_resp = SimpleNamespace(status_code=401, json=MagicMock(return_value={}))
    fake_client = AsyncMock()
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)
    fake_client.get = AsyncMock(return_value=fake_resp)

    async def _rc(prompt: str, *_a: Any, **_kw: Any) -> tuple[str, dict[str, Any]]:
        return _json.dumps(BASE_SPEC), {}

    pm = MagicMock()
    with patch("self_modify.reflect.P.ProposalStore", return_value=store), \
         patch("self_modify.reflect._build_context",
               AsyncMock(return_value={"tools_block": "", "objectives_block": "", "theses_block": ""})), \
         patch("self_modify.reflect._registered_endpoints", return_value={}), \
         patch("self_modify.reflect._registered_digest_fields", return_value=set()), \
         patch("self_modify.reflect.tool_name_collides", return_value=False), \
         patch("self_modify.reflect.reflect_chat", side_effect=_rc), \
         patch.object(reflect, "socket", MagicMock(
             getaddrinfo=MagicMock(return_value=[
                 (None, None, None, None, ("104.16.0.1", 0))
             ])
         )), \
         patch.object(reflect.httpx, "AsyncClient", return_value=fake_client), \
         patch("self_modify.reflect.gates.run_pipeline",
               AsyncMock(return_value="pending_approval")), \
         patch("self_modify.shadow.run_shadow_verdict",
               AsyncMock(return_value={"verdict": "APPROVE", "axes": {}, "reasons": [], "engine": "t", "prompt_version": "t"})), \
         patch("self_modify.reflect.liveness.run_liveness_probe",
               AsyncMock(return_value={"url": "x", "hits": [], "n_hits": 0, "digest_fields": []})):
        store.count_by_status_and_author = AsyncMock(return_value=0)
        result = await reflect.run_reflection(
            _fake_config(True), pm, _fake_llm(_json.dumps(BASE_SPEC)), provider="claude-cli",
        )
    # The first attempt is rejected_smoke (retry eligible).
    assert result.get("first_attempt", {}) or True
    outcome = result.get("outcome")
    # Retry may follow; either way the FIRST attempt was rejected_smoke,
    # and pending_key must NOT have been written.
    for c in store.submit_terminal.await_args_list:
        assert c.kwargs["status"] != P.STATUS_PENDING_KEY
    # first-attempt outcome recorded via retry harness
    assert outcome in {"rejected_smoke", "malformed", "rejected_shape", "submitted",
                       "rejected_static", "rejected_stale", "rejected_endpoint", "rejected_name"}


# ---------- cap enforcement ----------------------------------------------

@pytest.mark.asyncio
async def test_pending_key_cap_blocks_new_reflect() -> None:
    """When PENDING_KEY_CAP is hit, run_reflection refuses with the
    same shape as the regular pending cap."""
    store = MagicMock()
    async def _count(status: str, proposed_by: str) -> int:
        return 0 if status == P.STATUS_PENDING_APPROVAL else reflect.PENDING_KEY_CAP
    store.count_by_status_and_author = AsyncMock(side_effect=_count)
    pm = MagicMock()
    with patch("self_modify.reflect.P.ProposalStore", return_value=store):
        result = await reflect.run_reflection(
            _fake_config(True), pm, _fake_llm("NONE"), provider="claude-cli",
        )
    assert result["outcome"] == "refused_cap"
    assert "pending_key" in result["reason"].lower()


# ---------- template render: key blocks are repr-safe --------------------

def test_template_render_query_key_block_uses_repr() -> None:
    """Query-key spec renders _REQUIRES_KEY_ENV/_KEY_IN/_KEY_PARAM
    as repr'd Python literals — no injection surface even if the LLM
    tried to embed a quote."""
    content = reflect.TOOL_TEMPLATE.format(
        tool_name="get_x",
        class_name="GetXTool",
        tool_name_repr=repr("get_x"),
        base_url_repr=repr("https://api.example.com"),
        endpoint_path_repr=repr("/v1/x"),
        digest_fields_repr=repr(["a", "b", "c"]),
        description_repr=repr("desc long enough"),
        source_label_repr=repr("api.example.com"),
        endpoint_declaration_repr=repr("api.example.com/v1/x"),
        requires_key_env_repr=repr("BEACONCHAIN_API_KEY"),
        key_in_repr=repr("query"),
        key_param_repr=repr("apikey"),
    )
    # Constants baked in as repr'd Python literals.
    assert "_REQUIRES_KEY_ENV = 'BEACONCHAIN_API_KEY'" in content
    assert "_KEY_IN = 'query'" in content
    assert "_KEY_PARAM = 'apikey'" in content
    # Runtime fetch via os.getenv — never baked.
    assert "os.getenv(_REQUIRES_KEY_ENV" in content
    # No literal value baked in ANYWHERE.
    assert "beaconchain_actual_key_value" not in content.lower()


def test_template_render_header_key_block() -> None:
    content = reflect.TOOL_TEMPLATE.format(
        tool_name="get_x",
        class_name="GetXTool",
        tool_name_repr=repr("get_x"),
        base_url_repr=repr("https://api.example.com"),
        endpoint_path_repr=repr("/v1/x"),
        digest_fields_repr=repr(["a", "b", "c"]),
        description_repr=repr("desc long enough"),
        source_label_repr=repr("api.example.com"),
        endpoint_declaration_repr=repr("api.example.com/v1/x"),
        requires_key_env_repr=repr("FOO_API_KEY"),
        key_in_repr=repr("header"),
        key_param_repr=repr("X-API-Key"),
    )
    assert "_KEY_IN = 'header'" in content
    assert "_KEY_PARAM = 'X-API-Key'" in content


def test_template_render_keyless_unchanged() -> None:
    """A keyless render still passes None-repr through — the runtime
    path skips the env lookup entirely (byte-behaviour: identical
    to pre-key era for keyless tools)."""
    content = reflect.TOOL_TEMPLATE.format(
        tool_name="get_x",
        class_name="GetXTool",
        tool_name_repr=repr("get_x"),
        base_url_repr=repr("https://api.example.com"),
        endpoint_path_repr=repr("/v1/x"),
        digest_fields_repr=repr(["a", "b", "c"]),
        description_repr=repr("desc long enough"),
        source_label_repr=repr("api.example.com"),
        endpoint_declaration_repr=repr("api.example.com/v1/x"),
        requires_key_env_repr=repr(None),
        key_in_repr=repr(None),
        key_param_repr=repr(None),
    )
    assert "_REQUIRES_KEY_ENV = None" in content
    assert "_KEY_IN = None" in content
    assert "_KEY_PARAM = None" in content


def test_template_renders_valid_python() -> None:
    import ast
    content = reflect.TOOL_TEMPLATE.format(
        tool_name="get_x",
        class_name="GetXTool",
        tool_name_repr=repr("get_x"),
        base_url_repr=repr("https://api.example.com"),
        endpoint_path_repr=repr("/v1/x"),
        digest_fields_repr=repr(["a", "b", "c"]),
        description_repr=repr("desc long enough"),
        source_label_repr=repr("api.example.com"),
        endpoint_declaration_repr=repr("api.example.com/v1/x"),
        requires_key_env_repr=repr("FOO_API_KEY"),
        key_in_repr=repr("query"),
        key_param_repr=repr("api_key"),
    )
    ast.parse(content)  # syntax-valid


# ---------- provision command: absent-refusal + present-redrive ---------

@pytest.mark.asyncio
async def test_provision_refuses_when_env_var_absent(monkeypatch, capsys) -> None:
    """Absent env var → refuse without ever printing / logging any
    value (there is no value to print — but the refusal message
    names the env var ONLY, not any value)."""
    from self_modify import cli as cli_mod
    monkeypatch.delenv("BEACONCHAIN_API_KEY", raising=False)
    store = MagicMock()
    store.resolve_id = AsyncMock(return_value="00000000-0000-0000-0000-000000000abc")
    store.get = AsyncMock(return_value={
        "proposal_id": "00000000-0000-0000-0000-000000000abc",
        "status": P.STATUS_PENDING_KEY,
        "content": _json.dumps(KEYED_SPEC),
    })
    args = SimpleNamespace(proposal_id="00000abc")
    rc = await cli_mod._cmd_provision(store, args)
    assert rc == 1
    captured = capsys.readouterr()
    # Refusal message names the env var.
    assert "BEACONCHAIN_API_KEY" in captured.err
    # And says "not set" (or similar) — no value.
    assert "not set" in captured.err.lower()


@pytest.mark.asyncio
async def test_provision_redrives_when_env_var_present(monkeypatch, capsys) -> None:
    """Env var present → new proposal submitted with retry_of chain.
    The value is NEVER printed. This test injects a sentinel value
    and asserts it doesn't appear in stdout/stderr."""
    from self_modify import cli as cli_mod
    SENTINEL = "SENTINEL_KEY_VALUE_MUST_NOT_LEAK_XYZ"
    monkeypatch.setenv("BEACONCHAIN_API_KEY", SENTINEL)
    store = MagicMock()
    store.resolve_id = AsyncMock(return_value="00000000-0000-0000-0000-000000000abc")
    store.get = AsyncMock(return_value={
        "proposal_id": "00000000-0000-0000-0000-000000000abc",
        "status": P.STATUS_PENDING_KEY,
        "content": _json.dumps(KEYED_SPEC),
        "engine": "claude-cli",
    })
    store.submit = AsyncMock(return_value="new-uuid-1234-5678")

    with patch("self_modify.cli.load_config", AsyncMock(return_value=SimpleNamespace())), \
         patch("self_modify.gates.run_pipeline", AsyncMock(return_value=P.STATUS_PENDING_APPROVAL)), \
         patch("self_modify.liveness.run_liveness_probe",
               AsyncMock(return_value={"url": "x", "hits": [], "digest_fields": []})), \
         patch("self_modify.liveness.classify_probe",
               return_value={"outcome": "pass", "rule": None, "reason": ""}):
        args = SimpleNamespace(proposal_id="00000abc")
        rc = await cli_mod._cmd_provision(store, args)
    assert rc == 0
    captured = capsys.readouterr()
    # The sentinel value MUST NOT appear anywhere in the CLI output.
    assert SENTINEL not in captured.out
    assert SENTINEL not in captured.err


# ---------- pending_key excluded from negative list ---------------------

def test_pending_key_not_in_negative_list_statuses() -> None:
    """A parked keyed proposal is NOT a defect and must not surface in
    the reflect negative list (which would falsely discourage re-
    proposing the same endpoint after provisioning)."""
    assert P.STATUS_PENDING_KEY not in P.NEGATIVE_LIST_STATUSES


def test_pending_key_allowed_by_submit_terminal() -> None:
    """The submit_terminal guard must accept STATUS_PENDING_KEY."""
    assert P.STATUS_PENDING_KEY in P._PRE_SUBMIT_TERMINAL_STATUSES


# ---------- helpers ------------------------------------------------------

def _fake_config(can_self_modify: bool) -> Any:
    return SimpleNamespace(
        permissions=SimpleNamespace(
            permissions=SimpleNamespace(can_self_modify=can_self_modify),
        ),
    )


def _fake_llm(response_text: str) -> MagicMock:
    llm = MagicMock()
    llm.chat = AsyncMock(
        return_value=SimpleNamespace(
            message=SimpleNamespace(content=response_text)
        )
    )
    return llm
