"""Freshness gate tests.

Proposal 7c31c9c8 (rejected at gate 3) exposed the silent-staleness
class: a chronologically-ascending array passes the shape gate but
the template extracts data[0] = the OLDEST element. The applied
tool would return years-old values on every call, poisoning
findings → syntheses → theses. This suite pins:

- Retro fixture from the real DefiLlama TVL response shape.
- Skew inequality and direction (ascending rejects, descending
  passes, within-skew passes).
- Field-detection heuristics: name-based first, value-based fallback.
- Fail-open on no date-like field OR unparseable date.
- Retry wiring: rejected_stale triggers a corrective attempt with
  both parsed dates in the failure reason.
- Lifecycle registration in proposals.py.
"""

from __future__ import annotations

import json as _json
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from self_modify import proposals as P
from self_modify import reflect


# ---------- pure date-value parsing --------------------------------------

def test_parse_epoch_seconds_int() -> None:
    dt = reflect._parse_date_value(1_704_000_000)  # 2023-12-31
    assert dt is not None
    assert dt.year == 2023


def test_parse_epoch_milliseconds_int() -> None:
    dt = reflect._parse_date_value(1_704_000_000_000)
    assert dt is not None
    assert dt.year == 2023


def test_parse_epoch_seconds_float() -> None:
    dt = reflect._parse_date_value(1_704_000_000.5)
    assert dt is not None
    assert dt.year == 2023


def test_parse_iso_string() -> None:
    dt = reflect._parse_date_value("2024-05-13T12:00:00+00:00")
    assert dt is not None
    assert dt.year == 2024 and dt.month == 5


def test_parse_iso_string_with_Z() -> None:
    dt = reflect._parse_date_value("2024-05-13T12:00:00Z")
    assert dt is not None
    assert dt.year == 2024


def test_parse_numeric_string_epoch() -> None:
    dt = reflect._parse_date_value("1704000000")
    assert dt is not None
    assert dt.year == 2023


def test_parse_rejects_small_integers() -> None:
    """Small ints (< 1e9) are NOT epoch — a field ``count=42`` must
    not be treated as a date."""
    assert reflect._parse_date_value(42) is None
    assert reflect._parse_date_value(1000) is None


def test_parse_rejects_booleans() -> None:
    assert reflect._parse_date_value(True) is None
    assert reflect._parse_date_value(False) is None


def test_parse_rejects_empty_and_junk_strings() -> None:
    assert reflect._parse_date_value("") is None
    assert reflect._parse_date_value("   ") is None
    assert reflect._parse_date_value("not a date") is None


# ---------- _find_date_field: name-first, value-fallback ----------------

def test_find_date_field_prefers_name_match() -> None:
    """A field literally named ``date`` wins even if other fields
    could parse as dates."""
    element = {"count": 42, "date": "2024-01-01", "other": 1_704_000_000}
    name, dt = reflect._find_date_field(element)
    assert name == "date"
    assert dt is not None


def test_find_date_field_case_insensitive_name_match() -> None:
    element = {"count": 42, "Timestamp": 1_704_000_000}
    name, dt = reflect._find_date_field(element)
    assert name == "Timestamp"


def test_find_date_field_falls_back_to_value_scan() -> None:
    """No name-match — the first field whose value parses wins."""
    element = {"count": 42, "when": 1_704_000_000, "note": "hi"}
    name, _ = reflect._find_date_field(element)
    assert name == "when"


def test_find_date_field_none_when_nothing_parses() -> None:
    element = {"count": 42, "note": "hi", "flag": True}
    name, dt = reflect._find_date_field(element)
    assert name is None
    assert dt is None


# ---------- ascending vs descending vs within-skew ----------------------

def _epoch_days_ago(days: int, now: float = 1_724_500_000.0) -> int:
    """Return an epoch-seconds int ``days`` days before ``now``."""
    return int(now - days * 86400)


def test_ascending_daily_array_rejected_with_both_dates_and_count() -> None:
    """The retro shape from the real DefiLlama /historicalChainTvl
    response: ~1500 daily entries, chronological ascending, first
    element from 2018."""
    items = [
        {"date": 1_524_787_200, "tvl": 100.0},   # 2018-04-27
        {"date": 1_524_873_600, "tvl": 101.0},
        {"date": 1_724_371_200, "tvl": 89000.0},  # 2024-08-23
    ]
    reason = reflect._list_freshness_reject_reason(items, "list[0]")
    assert reason is not None
    assert "2018-04-27" in reason
    assert "2024-08-23" in reason
    assert "3 elements" in reason
    assert "list[0]" in reason


def test_descending_newest_first_array_passes() -> None:
    items = [
        {"date": 1_724_371_200, "tvl": 89000.0},   # today-ish
        {"date": 1_524_787_200, "tvl": 100.0},     # 2018
    ]
    assert reflect._list_freshness_reject_reason(items, "list[0]") is None


def test_within_skew_passes() -> None:
    """Two-day span → below 30-day skew → pass."""
    items = [
        {"date": _epoch_days_ago(2)},
        {"date": _epoch_days_ago(0)},
    ]
    assert reflect._list_freshness_reject_reason(items, "list[0]") is None


def test_single_element_skips() -> None:
    assert reflect._list_freshness_reject_reason(
        [{"date": 1_524_787_200}], "list[0]",
    ) is None


def test_empty_list_skips() -> None:
    assert reflect._list_freshness_reject_reason([], "list[0]") is None


def test_no_date_like_field_skips_fail_open() -> None:
    """A response with no date field passes the freshness check —
    the shape/scalarity gates are still expected to catch content
    issues."""
    items = [
        {"count": 100, "name": "a"},
        {"count": 200, "name": "b"},
    ]
    assert reflect._list_freshness_reject_reason(items, "list[0]") is None


def test_iso_string_ascending_rejected() -> None:
    items = [
        {"timestamp": "2018-04-27T00:00:00Z"},
        {"timestamp": "2024-08-23T00:00:00Z"},
    ]
    reason = reflect._list_freshness_reject_reason(items, "list[0]")
    assert reason is not None
    assert "2018-04-27" in reason


def test_ms_epoch_ascending_rejected() -> None:
    items = [
        {"time": 1_524_787_200_000},
        {"time": 1_724_371_200_000},
    ]
    reason = reflect._list_freshness_reject_reason(items, "list[0]")
    assert reason is not None


# ---------- _freshness_check dispatch -----------------------------------

def test_freshness_check_top_level_dict_is_na() -> None:
    """A snapshot dict has no ordering — check must not fire."""
    body = {"tvl": 89000.0, "date": "2018-04-27"}
    assert reflect._freshness_check(body) is None


def test_freshness_check_nested_data_list_dispatched() -> None:
    body = {
        "meta": {"info": "x"},
        "data": [
            {"date": 1_524_787_200, "tvl": 100.0},
            {"date": 1_724_371_200, "tvl": 89000.0},
        ],
    }
    reason = reflect._freshness_check(body)
    assert reason is not None
    assert "data[0] (nested)" in reason


def test_freshness_check_top_level_list_dispatched() -> None:
    body = [
        {"date": 1_524_787_200, "tvl": 100.0},
        {"date": 1_724_371_200, "tvl": 89000.0},
    ]
    reason = reflect._freshness_check(body)
    assert reason is not None
    assert "list[0] (top-level list)" in reason


# ---------- env override for skew ---------------------------------------

def test_skew_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FRESHNESS_SKEW_DAYS", raising=False)
    assert reflect._resolve_freshness_skew_days() == reflect._FRESHNESS_SKEW_DAYS


def test_skew_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FRESHNESS_SKEW_DAYS", "7")
    assert reflect._resolve_freshness_skew_days() == 7.0


def test_skew_env_invalid_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FRESHNESS_SKEW_DAYS", "not-a-float")
    assert reflect._resolve_freshness_skew_days() == reflect._FRESHNESS_SKEW_DAYS
    monkeypatch.setenv("FRESHNESS_SKEW_DAYS", "0")
    assert reflect._resolve_freshness_skew_days() == reflect._FRESHNESS_SKEW_DAYS
    monkeypatch.setenv("FRESHNESS_SKEW_DAYS", "-1")
    assert reflect._resolve_freshness_skew_days() == reflect._FRESHNESS_SKEW_DAYS


def test_skew_override_changes_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    """5-day span: fails at skew=1, passes at skew=30."""
    items = [
        {"date": _epoch_days_ago(5)},
        {"date": _epoch_days_ago(0)},
    ]
    monkeypatch.setenv("FRESHNESS_SKEW_DAYS", "30")
    assert reflect._list_freshness_reject_reason(items, "list[0]") is None
    monkeypatch.setenv("FRESHNESS_SKEW_DAYS", "1")
    assert reflect._list_freshness_reject_reason(items, "list[0]") is not None


# ---------- rejected_stale is a first-class terminal status --------------

def test_rejected_stale_registered() -> None:
    assert P.STATUS_REJECTED_STALE == "rejected_stale"
    assert P.STATUS_REJECTED_STALE in P.ALL_STATUSES
    assert P.STATUS_REJECTED_STALE in P.NEGATIVE_LIST_STATUSES
    assert P.STATUS_REJECTED_STALE in P._PRE_SUBMIT_TERMINAL_STATUSES


def test_rejected_stale_is_retry_eligible() -> None:
    assert "rejected_stale" in reflect._RETRY_ELIGIBLE_OUTCOMES
    assert reflect._OUTCOME_TO_STATUS["rejected_stale"] == P.STATUS_REJECTED_STALE


# ---------- end-to-end wiring: rejected_stale triggers retry ------------

STALE_SPEC = {
    # Coherence-clean tokens ("chain", "tvl") in description.
    "tool_name": "get_chain_tvl_history",
    "api_base_url": "https://api.llama.fi",
    "endpoint_path": "/v1/historicalChainTvl/Ethereum",
    # Three fields — meets the well-formed 3-6 count. All three
    # present as scalars in the fixture body.
    "digest_fields": ["date", "tvl", "a"],
    "description": "Fetch Ethereum chain historical TVL from DefiLlama.",
    "rationale": "gap",
}

FRESH_SPEC = {
    "tool_name": "get_ethereum_tvl_current",
    "api_base_url": "https://api.llama.fi",
    "endpoint_path": "/v2/chains/Ethereum",
    "digest_fields": ["a", "b", "c"],
    "description": "Fetch current Ethereum TVL snapshot from DefiLlama.",
    "rationale": "gap",
}


def _mock_store() -> MagicMock:
    store = MagicMock()
    store.count_by_status_and_author = AsyncMock(return_value=0)
    store.list_recent_rejections = AsyncMock(return_value=[])
    store.submit_terminal = AsyncMock(return_value="reject-1")
    store.submit = AsyncMock(return_value="prop-1")
    store.get = AsyncMock(return_value={
        "proposal_id": "prop-1",
        "status_reason": "gate_tests: pytest passed",
    })
    store.update_status = AsyncMock(return_value=True)
    return store


def _http_stack(bodies: list[Any]) -> Any:
    responses = [
        SimpleNamespace(status_code=200, json=MagicMock(return_value=b))
        for b in bodies
    ]
    fake = AsyncMock()
    fake.__aenter__ = AsyncMock(return_value=fake)
    fake.__aexit__ = AsyncMock(return_value=None)
    fake.get = AsyncMock(side_effect=responses)
    return fake


def _fake_config() -> SimpleNamespace:
    return SimpleNamespace(
        permissions=SimpleNamespace(
            permissions=SimpleNamespace(can_self_modify=True),
        ),
    )


async def _drive(
    specs: list[dict[str, Any]],
    bodies: list[Any],
    store: MagicMock,
) -> tuple[dict[str, Any], list[str]]:
    calls: list[str] = []

    async def _rc(prompt: str, *_a: Any, **_kw: Any) -> tuple[str, dict[str, Any]]:
        calls.append(prompt)
        return _json.dumps(specs[len(calls) - 1]), {}

    pm = MagicMock()
    with patch("self_modify.reflect.P.ProposalStore", return_value=store), \
         patch("self_modify.reflect._build_context",
               AsyncMock(return_value={
                   "tools_block": "", "objectives_block": "",
                   "theses_block": "", "rejections_block": "",
               })), \
         patch("self_modify.reflect._registered_endpoints", return_value={}), \
         patch("self_modify.reflect._registered_digest_fields", return_value=set()), \
         patch("self_modify.reflect.tool_name_collides", return_value=False), \
         patch("self_modify.reflect.reflect_chat", side_effect=_rc), \
         patch.object(reflect, "socket", MagicMock(
             getaddrinfo=MagicMock(return_value=[
                 (None, None, None, None, ("104.16.0.1", 0))
             ])
         )), \
         patch.object(reflect.httpx, "AsyncClient", return_value=_http_stack(bodies)), \
         patch("self_modify.reflect.gates.run_pipeline",
               AsyncMock(return_value="pending_approval")), \
         patch("self_modify.shadow.run_shadow_verdict",
               AsyncMock(return_value={
                   "verdict": "APPROVE", "axes": {}, "reasons": [],
                   "engine": "test", "prompt_version": "test",
               })):
        result = await reflect.run_reflection(
            _fake_config(), pm, MagicMock(), provider="claude-cli",
        )
    return result, calls


@pytest.mark.asyncio
async def test_stale_response_triggers_rejected_stale_and_retry() -> None:
    """DefiLlama-shaped ascending array → rejected_stale on first
    attempt; corrective prompt carries both dates; retry pivots to
    a fresh-endpoint spec that passes shape + freshness."""
    stale_body = [
        {"date": 1_524_787_200, "tvl": 100.0, "a": 1, "b": 2, "c": 3},   # 2018
        {"date": 1_724_371_200, "tvl": 89000.0, "a": 1, "b": 2, "c": 3},  # today
    ]
    fresh_body = {"a": 1, "b": 2, "c": 3}
    store = _mock_store()
    result, calls = await _drive(
        [STALE_SPEC, FRESH_SPEC],
        [stale_body, fresh_body],
        store,
    )
    assert result["outcome"] == "submitted"
    assert result["retried"] is True
    assert result["first_attempt"]["outcome"] == "rejected_stale"
    # First-attempt row persisted with the correct status.
    st_kwargs = store.submit_terminal.await_args.kwargs
    assert st_kwargs["status"] == P.STATUS_REJECTED_STALE
    # Both dates in the reason.
    assert "2018-04-27" in st_kwargs["status_reason"]
    assert "2024-08-23" in st_kwargs["status_reason"]
    # Corrective prompt carries them too.
    assert "2018-04-27" in calls[1]
    assert "2024-08-23" in calls[1]


@pytest.mark.asyncio
async def test_stale_x2_persisted_no_third_attempt() -> None:
    """Ascending body on both attempts → both rejected_stale,
    second linked to first via retry_of."""
    stale_body = [
        # Include field 'a' so shape passes → freshness fires.
        {"date": 1_524_787_200, "tvl": 100.0, "a": 1},
        {"date": 1_724_371_200, "tvl": 89000.0, "a": 1},
    ]
    store = _mock_store()
    store.submit_terminal = AsyncMock(
        side_effect=["stale-first", "stale-retry"],
    )
    result, calls = await _drive(
        [STALE_SPEC, STALE_SPEC],
        [stale_body, stale_body],
        store,
    )
    assert result["outcome"] == "rejected_stale"
    assert result["retried"] is True
    assert len(calls) == 2
    st_calls = store.submit_terminal.await_args_list
    assert st_calls[0].kwargs["retry_of"] is None
    assert st_calls[1].kwargs["retry_of"] == "stale-first"
    store.submit.assert_not_awaited()


@pytest.mark.asyncio
async def test_fresh_response_reaches_pending_approval_no_retry() -> None:
    """Descending array with a today-ish first element → passes
    freshness, all other gates green, submitted first-attempt."""
    fresh_body = [
        {"date": 1_724_371_200, "tvl": 89000.0, "a": 1, "b": 2, "c": 3},
        {"date": 1_524_787_200, "tvl": 100.0, "a": 1, "b": 2, "c": 3},
    ]
    store = _mock_store()
    result, calls = await _drive(
        [dict(FRESH_SPEC, digest_fields=["date", "tvl", "a"])],
        [fresh_body],
        store,
    )
    assert result["outcome"] == "submitted"
    assert result["retried"] is False
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_no_date_field_bypasses_freshness_check() -> None:
    """List response with no date-like field → freshness skipped,
    the shape gate governs (fail-open contract)."""
    body = [
        {"a": 1, "b": 2, "c": 3},
        {"a": 4, "b": 5, "c": 6},
    ]
    store = _mock_store()
    result, _calls = await _drive(
        [dict(FRESH_SPEC, digest_fields=["a", "b", "c"])],
        [body],
        store,
    )
    assert result["outcome"] == "submitted"
