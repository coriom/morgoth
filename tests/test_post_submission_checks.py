"""Full-submission-path coverage for the post-submission checks.

Motivating regression: 9f446bb4 (2026-09-29). Reflect autonomously
submitted a path-digest tool. Gate_tests passed. Then the checks
after gate_tests crashed on ``set(spec["digest_fields"])`` (dict
entries are unhashable) — reflect unwound, the proposal sat at
pending_approval with liveness/overlap/shadow SILENTLY UNEVALUATED.
The gate-selftest of 6bbfb9f only exercised gate_zone + gate_tests;
this suite adds the missing path-digest coverage AND the
crashed-check-holds invariant.

Contract locked by this suite:

  · normalize_digest_fields accepts BOTH strings and {name,path}
    dicts and returns a canonical list of dicts.
  · The full run_reflection path with a path-digest spec reaches
    ``submitted`` and every post-submission check runs.
  · A crash in ANY post-submission check → STATUS_CHECKS_INCOMPLETE,
    the check name recorded in status_reason so ``morgoth show``
    surfaces it, and delegation IS NOT allowed to flip the proposal
    under this condition (crashed shadow != REJECT verdict).
  · The status is registered in ALL_STATUSES so the state machine
    can accept it.
"""

from __future__ import annotations

import json as _json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from self_modify import reflect
from self_modify import proposals as P
from self_modify import post_submission_checks as pchecks
from self_modify.digest_path import (
    normalize_digest_fields, digest_field_names,
)


PATH_DIGEST_SPEC = {
    "tool_name": "get_test_stablecoins",
    "api_base_url": "https://stablecoins.example.com",
    "endpoint_path": "/stablecoins",
    "digest_fields": [
        {"name": "total_supply",
         "path": "sum(peggedAssets[*].circulating.peggedUSD)"},
        "usdt_symbol",   # legacy string form — must coexist with dict entries
        {"name": "asset_count", "path": "count(peggedAssets[*])"},
    ],
    "description": (
        "Fetch aggregate stablecoin issuance test digest for the rail."
    ),
    "rationale": (
        "test-suite path-digest coverage; total_supply is the "
        "concrete field that measures the claimed gap."
    ),
}


# The response body every fake HTTP hit returns — a nested structure
# that ONLY the path-digest resolver can extract. The legacy top-level
# scalar name (usdt_symbol) is also present so the shape gate passes.
BODY = {
    "peggedAssets": [
        {"symbol": "USDT", "circulating": {"peggedUSD": 100_000_000_000}},
        {"symbol": "USDC", "circulating": {"peggedUSD":  40_000_000_000}},
    ],
    "usdt_symbol": "USDT",
}


def _fake_config() -> SimpleNamespace:
    return SimpleNamespace(
        permissions=SimpleNamespace(
            permissions=SimpleNamespace(can_self_modify=True),
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


def _mock_env() -> tuple[MagicMock, AsyncMock]:
    """Return (store_mock, fake_http_client) wired for the happy path."""
    store_mock = MagicMock()
    store_mock.count_by_status_and_author = AsyncMock(return_value=0)
    store_mock.submit = AsyncMock(return_value="prop-path-1")
    _row = {"proposal_id": "prop-path-1", "status_reason": ""}
    async def _get(_pid: str):
        return _row
    async def _update(pid: str, status: str, reason: str = "") -> None:
        _row["status"] = status
        _row["status_reason"] = reason
    store_mock.get = AsyncMock(side_effect=_get)
    store_mock.update_status = AsyncMock(side_effect=_update)
    fake_resp = SimpleNamespace(status_code=200, json=MagicMock(return_value=BODY))
    fake_client = AsyncMock()
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)
    fake_client.get = AsyncMock(return_value=fake_resp)
    return store_mock, fake_client


# --------- unit: normalize + names ----------------------------------------

def test_normalize_upgrades_strings_and_preserves_dicts() -> None:
    entries = ["total_supply",
               {"name": "asset_count", "path": "count(peggedAssets[*])"}]
    out = normalize_digest_fields(entries)
    assert out == [
        {"name": "total_supply", "path": "total_supply"},
        {"name": "asset_count", "path": "count(peggedAssets[*])"},
    ]
    assert digest_field_names(entries) == ["total_supply", "asset_count"]


def test_normalize_never_raises_on_bad_entry() -> None:
    # Malformed entries are DROPPED with a WARN — pre-submit validation
    # is the authoritative rejection path; normalize is defence-in-depth.
    out = normalize_digest_fields([{"name": "TotallyWrong", "path": "x"}])
    assert out == []


# --------- crashed-check contract ------------------------------------------

@pytest.mark.asyncio
async def test_liveness_crash_holds_at_checks_incomplete() -> None:
    """A liveness crash MUST NOT let the proposal reach pending_approval
    silently. The row lands at STATUS_CHECKS_INCOMPLETE with the check
    name in status_reason."""
    store, _ = _mock_env()

    def _boom(probe, spec):
        raise TypeError("unhashable type: 'dict'")

    with patch.object(pchecks, "_liveness_check", _boom):
        results, final = await pchecks.run_post_submission_checks(
            store=store, proposal_id="prop-path-1",
            spec=PATH_DIGEST_SPEC, probe={"hits": []},
            registered_field_names=set(),
            config=_fake_config(), pm=MagicMock(),
            delegation_enabled=False,
        )
    assert final == P.STATUS_CHECKS_INCOMPLETE
    # Named in results and in the persisted status_reason.
    crashed = [r for r in results if r.status == "crashed"]
    assert any(r.name == pchecks.CHECK_LIVENESS for r in crashed)
    reason = store.update_status.await_args.args[2]
    assert "checks_incomplete" in reason
    assert "liveness" in reason
    assert "unhashable" in reason


@pytest.mark.asyncio
async def test_shadow_crash_holds_at_checks_incomplete() -> None:
    store, _ = _mock_env()
    with patch("self_modify.shadow.run_shadow_verdict",
               AsyncMock(side_effect=RuntimeError("model unreachable"))):
        results, final = await pchecks.run_post_submission_checks(
            store=store, proposal_id="prop-path-1",
            spec=PATH_DIGEST_SPEC, probe={"hits": []},
            registered_field_names=set(),
            config=_fake_config(), pm=MagicMock(),
            delegation_enabled=True,   # even under delegation on
        )
    assert final == P.STATUS_CHECKS_INCOMPLETE
    assert any(r.name == pchecks.CHECK_SHADOW and r.status == "crashed"
               for r in results)
    # Delegation MUST NOT flip on a crashed shadow — the verdict is
    # not "REJECT", it's absent. The row is held for operator review.
    assert not any(r.name == pchecks.CHECK_DELEGATION for r in results)


@pytest.mark.asyncio
async def test_overlap_note_names_registered_field() -> None:
    """Mixed digest_fields (strings + dicts) intersect cleanly against
    the registered-name set — the crash-site pattern of 9f446bb4."""
    store, _ = _mock_env()
    with patch("self_modify.shadow.run_shadow_verdict",
               AsyncMock(return_value={
                   "verdict": "APPROVE", "axes": {}, "reasons": [],
                   "engine": "t", "prompt_version": "t",
               })):
        results, final = await pchecks.run_post_submission_checks(
            store=store, proposal_id="prop-path-1",
            spec=PATH_DIGEST_SPEC, probe={"hits": []},
            registered_field_names={"total_supply", "unrelated"},
            config=_fake_config(), pm=MagicMock(),
            delegation_enabled=False,
        )
    assert final == P.STATUS_PENDING_APPROVAL
    overlap_r = next(r for r in results if r.name == pchecks.CHECK_OVERLAP)
    assert overlap_r.status == "warn"
    assert "total_supply" in overlap_r.message


# --------- end-to-end: reflect with path-digest spec ----------------------

@pytest.mark.asyncio
async def test_run_reflection_end_to_end_path_digest_spec_reaches_submitted(
    monkeypatch,
) -> None:
    """The 9f446bb4-shape regression: run_reflection with a path-digest
    spec whose digest_fields mixes string and dict entries MUST reach
    outcome=submitted and pending_approval. The pre-fix crash at
    ``set(spec['digest_fields'])`` would have raised TypeError here."""
    # Explicit — belt+braces with tests/fixtures/test.env — so the
    # test never spawns a real gate-selftest sandbox even under a
    # polluted-env parallel worker.
    monkeypatch.setenv("MORGOTH_REFLECT_GATE_PREFLIGHT", "0")
    pm = MagicMock()
    store, fake_client = _mock_env()

    with patch("self_modify.reflect.P.ProposalStore", return_value=store), \
         patch("self_modify.reflect._build_context",
               AsyncMock(return_value={
                   "tools_block": "", "objectives_block": "",
                   "theses_block": "",
               })), \
         patch("self_modify.reflect._registered_endpoints", return_value={}), \
         patch("self_modify.reflect._registered_digest_fields",
               return_value=set()), \
         patch("self_modify.reflect.tool_name_collides", return_value=False), \
         patch.object(reflect, "socket", MagicMock(
             getaddrinfo=MagicMock(return_value=[
                 (None, None, None, None, ("104.16.0.1", 0))
             ])
         )), \
         patch.object(reflect.httpx, "AsyncClient", return_value=fake_client), \
         patch("self_modify.reflect.gates.run_pipeline",
               AsyncMock(return_value=P.STATUS_PENDING_APPROVAL)), \
         patch("self_modify.shadow.run_shadow_verdict",
               AsyncMock(return_value={
                   "verdict": "APPROVE", "axes": {}, "reasons": [],
                   "engine": "t", "prompt_version": "t",
               })), \
         patch("self_modify.reflect.liveness.run_liveness_probe",
               AsyncMock(return_value={
                   "url": "x", "hits": [
                       {"i": i, "ok": True,
                        "vals": {"total_supply": 140_000_000_000,
                                 "usdt_symbol": "USDT",
                                 "asset_count": 2}}
                       for i in range(4)
                   ],
                   "n_hits": 4,
                   "digest_fields": ["total_supply", "usdt_symbol",
                                     "asset_count"],
               })):
        result = await reflect.run_reflection(
            _fake_config(), pm, _fake_llm(_json.dumps(PATH_DIGEST_SPEC)),
        )

    assert result["outcome"] == "submitted", result
    assert result["pipeline_status"] == P.STATUS_PENDING_APPROVAL


# --------- state-machine lock ---------------------------------------------

def test_checks_incomplete_registered_in_all_statuses() -> None:
    assert P.STATUS_CHECKS_INCOMPLETE == "checks_incomplete"
    assert P.STATUS_CHECKS_INCOMPLETE in P.ALL_STATUSES


def test_recheck_cli_registered() -> None:
    """Grep-lock: the recheck subcommand MUST be wired so operators can
    resurrect a proposal from checks_incomplete without invoking reflect."""
    import pathlib
    from self_modify import cli
    src = pathlib.Path(cli.__file__).read_text(encoding="utf-8")
    assert '"recheck"' in src
    assert "_cmd_recheck" in src
