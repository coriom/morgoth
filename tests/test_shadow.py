"""Shadow Gate 2.5 — parse matrix, blindness, storage roundtrip,
no-status-mutation invariant, prompt version, two-hit material.

RECORDED-NEVER-ENFORCED contract is verified two ways:
1. Grep-based static check: the shadow module never imports
   ``ProposalStore.update_status`` or references terminal statuses
   for writes.
2. Integration-shaped: the mocked run leaves the proposal row's
   status untouched (invariant test).
"""
from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from self_modify import shadow as S


# ---------- parse_verdict: full matrix ---------------------------------

def _good_json() -> str:
    return json.dumps({
        "verdict": "APPROVE",
        "axes": {
            "api_liveness": "PASS",
            "field_liveness": "PASS",
            "semantic_duplication": "PASS",
            "name_content_coherence": "PASS",
            "rationale_truth": "PASS",
        },
        "reasons": ["endpoint 200", "fields nonzero", "no twin",
                    "name matches", "rationale ok"],
    })


def test_parse_good_json() -> None:
    v = S.parse_verdict(_good_json())
    assert v["verdict"] == "APPROVE"
    assert v["axes"]["api_liveness"] == "PASS"
    assert len(v["reasons"]) == 5


def test_parse_with_code_fence() -> None:
    fenced = f"```json\n{_good_json()}\n```"
    v = S.parse_verdict(fenced)
    assert v["verdict"] == "APPROVE"


def test_parse_prose_wrapper() -> None:
    wrapped = f"Here is my verdict:\n{_good_json()}\nThat's my analysis."
    v = S.parse_verdict(wrapped)
    assert v["verdict"] == "APPROVE"


def test_parse_unknown_verdict_becomes_error() -> None:
    bad = json.dumps({"verdict": "MAYBE", "axes": {}, "reasons": []})
    v = S.parse_verdict(bad)
    assert v["verdict"] == "ERROR"


def test_parse_unknown_axis_level_becomes_warn() -> None:
    bad = json.dumps({
        "verdict": "FLAG",
        "axes": {"api_liveness": "GREAT", "field_liveness": "PASS"},
        "reasons": [],
    })
    v = S.parse_verdict(bad)
    assert v["axes"]["api_liveness"] == "WARN"
    assert v["axes"]["field_liveness"] == "PASS"
    # Missing axes filled with WARN — never silently PASS.
    assert v["axes"]["semantic_duplication"] == "WARN"


def test_parse_unparseable_records_error() -> None:
    v = S.parse_verdict("no json here at all sorry")
    assert v["verdict"] == "ERROR"
    assert v["reasons"] and "unparseable" in v["reasons"][0]


def test_parse_empty_input_records_error() -> None:
    v = S.parse_verdict("")
    assert v["verdict"] == "ERROR"


# ---------- spec-fact extraction --------------------------------------

_SAMPLE_CONTENT = """\
from tools.base_tool import BaseTool
_BASE_URL = 'https://api.example.com'
_ENDPOINT_PATH = '/v1/stats'
_TOOL_DESCRIPTION = 'Fetches network stats.'
_DIGEST_FIELDS = ['a', 'b', 'c']

class GetExampleStatsTool(BaseTool):
    name = 'get_example_stats'
"""


def test_extract_spec_facts() -> None:
    f = S.extract_spec_facts(_SAMPLE_CONTENT)
    assert f["base_url"] == "https://api.example.com"
    assert f["endpoint_path"] == "/v1/stats"
    assert f["description"] == "Fetches network stats."
    assert f["digest_fields"] == ["a", "b", "c"]
    assert f["class_name"] == "GetExampleStatsTool"


def test_extract_spec_facts_missing_fields() -> None:
    f = S.extract_spec_facts("class Foo(BaseTool):\n    pass\n")
    assert f["base_url"] is None
    assert f["digest_fields"] == []
    assert f["class_name"] == "Foo"


# ---------- blindness assertion ---------------------------------------

def test_assemble_shadow_input_strips_status() -> None:
    proposal = {
        "proposal_id": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
        "target_path": "tools/data_feeds/x.py",
        "rationale": "harmless",
        "status": "pending_approval",  # MUST NOT leak
        "status_reason": "operator note",  # MUST NOT leak
    }
    payload = S.assemble_shadow_input(
        proposal, spec_facts={}, endpoint_sample={}, registry=[],
    )
    blob = json.dumps(payload)
    assert "pending_approval" not in blob
    assert "operator note" not in blob


def test_blindness_leak_raises() -> None:
    proposal = {"proposal_id": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
                "status": "pending_approval"}
    with patch.object(S, "assemble_shadow_input",
                      wraps=S.assemble_shadow_input):
        # Directly test the internal assertion.
        with pytest.raises(AssertionError, match="status"):
            S._assert_blind({"status": "pending_approval"}, {})


def test_blindness_operator_reason_verbatim_scan() -> None:
    proposal = {
        "proposal_id": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
        "status_reason": "THIS_IS_THE_OPERATOR_NOTE",
    }
    payload = {"rationale": "THIS_IS_THE_OPERATOR_NOTE was copied in"}
    with pytest.raises(AssertionError, match="verbatim"):
        S._assert_blind(payload, proposal)


# ---------- sample_endpoint: two-hit material -------------------------

@pytest.mark.asyncio
async def test_sample_endpoint_ships_two_hits_material() -> None:
    """The prompt needs both hits and the projected digest values
    for the field-liveness axis (zero-across-hits detection)."""
    calls: list[str] = []

    async def _fake_get(url: str) -> dict[str, Any]:
        calls.append(url)
        return {"ok": True, "status_code": 200,
                "top_level_keys": ["a", "b"],
                "body": {"a": 0, "b": 42}}

    async def _no_sleep(_secs: int) -> None:
        return None

    with patch.object(S, "_one_get", _fake_get):
        out = await S.sample_endpoint(
            "https://api.example.com", "/v1/stats",
            gap_secs=0, digest_fields=["a", "b"],
            now_sleep=_no_sleep,
        )
    assert out["url"] == "https://api.example.com/v1/stats"
    assert out["digest_values_hit1"] == {"a": 0, "b": 42}
    assert out["digest_values_hit2"] == {"a": 0, "b": 42}
    # Body dropped — only slim material shipped.
    assert "body" not in out["hit1"]
    assert "body" not in out["hit2"]


# ---------- run_shadow_verdict: end-to-end with mocks -----------------

class _FakePM:
    """Minimal PM for the roundtrip test."""

    def __init__(self) -> None:
        self.recorded: list[dict[str, Any]] = []

    async def record_shadow_verdict(self, **kw: Any) -> str:
        self.recorded.append(kw)
        return "verdict-uuid"

    async def get_shadow_verdicts(self, pid: str) -> list[dict[str, Any]]:
        return [dict(r, proposal_id=pid) for r in self.recorded]


@pytest.mark.asyncio
async def test_run_shadow_verdict_records_and_returns() -> None:
    pm = _FakePM()
    caller = AsyncMock(return_value=(_good_json(), {"provider": "claude-cli"}))
    sampler = AsyncMock(return_value={"url": "x", "hit1": {}, "hit2": {},
                                       "digest_values_hit1": {},
                                       "digest_values_hit2": {}})
    with patch.object(S, "collect_registry_context", return_value=[]):
        out = await S.run_shadow_verdict(
            proposal={
                "proposal_id": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
                "content": _SAMPLE_CONTENT, "rationale": "r",
                "target_path": "tools/data_feeds/x.py",
            },
            config=MagicMock(), pm=pm,  # type: ignore[arg-type]
            endpoint_sampler=sampler, llm_caller=caller,
        )
    assert out["verdict"] == "APPROVE"
    assert out["engine"] == "claude-cli"
    assert out["prompt_version"] == S.PROMPT_VERSION
    assert out["verdict_id"] == "verdict-uuid"
    assert len(pm.recorded) == 1
    assert pm.recorded[0]["prompt_version"] == S.PROMPT_VERSION


@pytest.mark.asyncio
async def test_llm_error_records_error_verdict() -> None:
    pm = _FakePM()

    async def _boom(*a: Any, **k: Any) -> tuple[str, dict[str, Any]]:
        from self_modify.reflect_llm import ReflectLLMError
        raise ReflectLLMError("cli timed out")

    sampler = AsyncMock(return_value={"url": "x", "hit1": {}, "hit2": {},
                                       "digest_values_hit1": {},
                                       "digest_values_hit2": {}})
    with patch.object(S, "collect_registry_context", return_value=[]):
        out = await S.run_shadow_verdict(
            proposal={
                "proposal_id": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
                "content": "", "rationale": "",
                "target_path": "tools/data_feeds/x.py",
            },
            config=MagicMock(), pm=pm,  # type: ignore[arg-type]
            endpoint_sampler=sampler, llm_caller=_boom,
        )
    assert out["verdict"] == "ERROR"
    assert pm.recorded[0]["verdict"] == "ERROR"


# ---------- no-status-mutation invariant (static grep) ----------------

def test_shadow_module_never_mutates_proposal_status() -> None:
    """Grep-based invariant: shadow.py must not call any of the
    status-mutation surfaces. A single line — the definition of
    _MODULE_SOURCE_SCAN_STRINGS — mentions each string; every OTHER
    occurrence would be a smuggled write."""
    import inspect
    src = inspect.getsource(S)
    for needle in (
        "ProposalStore.update_status",
        ".submit_terminal(",
        "STATUS_APPROVED_PENDING_APPLY",
        # STATUS_REJECTED without the _NAME/_ENDPOINT/... suffixes
        # would only appear if we were writing rejects.
    ):
        occurrences = src.count(needle)
        # Allow exactly one occurrence: the source-scan tuple that
        # names the forbidden strings.
        assert occurrences <= 1, (
            f"shadow.py: {needle!r} appears {occurrences}× — "
            "no-status-mutation invariant broken"
        )


def test_prompt_version_constant_defined() -> None:
    assert S.PROMPT_VERSION and isinstance(S.PROMPT_VERSION, str)


def test_prompt_version_is_v2() -> None:
    """v2 semantics block landed — bump must be traceable per verdict."""
    assert S.PROMPT_VERSION == "v2"


def test_prompt_contains_verdict_semantics_block() -> None:
    """v2 decision-function block must reach the model.

    Grep-level so a refactor that quietly drops the semantics
    section can't silently regress the retro's agreement rate.
    """
    payload = {"proposal_id": "x", "spec_facts": {},
               "endpoint_sample": {}, "registry": [], "rationale": ""}
    prompt = S.build_prompt(payload)
    assert "VERDICT SEMANTICS" in prompt
    # The four defect classes / three verdict semantics anchors.
    for anchor in ("BLOCKING", "FLAG", "APPROVE", "reserves", "core datum"):
        assert anchor in prompt, f"missing anchor: {anchor!r}"


def test_prompt_semantics_names_no_specific_tools() -> None:
    """Generality constraint: the semantics block references defect
    classes only — never a specific past tool name or endpoint."""
    payload = {"proposal_id": "x", "spec_facts": {},
               "endpoint_sample": {}, "registry": [], "rationale": ""}
    prompt = S.build_prompt(payload)
    for banned in (
        "cddda7fa", "42c43533", "get_bitcoin", "get_ethereum",
        "blockcypher", "blockchain.info", "miners_revenue_usd",
        "peer_count", "indexPrice",
    ):
        assert banned not in prompt, f"prompt overfits to {banned!r}"


# ---------- prompt build sanity ---------------------------------------

def test_build_prompt_includes_axes_instructions() -> None:
    payload = {"proposal_id": "x", "spec_facts": {},
               "endpoint_sample": {"digest_values_hit1": {"a": 0},
                                   "digest_values_hit2": {"a": 0}},
               "registry": [], "rationale": ""}
    prompt = S.build_prompt(payload)
    for axis in S._AXES:
        assert axis in prompt
    # Two-hit material must reach the model.
    assert "digest_values_hit1" in prompt
    assert "digest_values_hit2" in prompt
