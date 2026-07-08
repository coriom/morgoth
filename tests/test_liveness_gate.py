"""Deterministic field-liveness gate — rules (a)/(b)/(c) + scheduler.

Mocked-hit fixtures from the three probed cases (BlockCypher,
blockchain.info, DefiLlama) each trip the gate. One live-moving
fixture passes. One non-rolling-static fires the WARN path.
"""
from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from self_modify import liveness as L


# ---------- rolling-name predicate ------------------------------------

def test_rolling_name_matches_windows() -> None:
    for f in ("total24h", "TOTAL_24H", "vol_7d", "volume_30d", "rolling_24h"):
        assert L.is_rolling_named(f), f

def test_rolling_name_matches_flow_and_rate() -> None:
    for f in ("miners_revenue_usd_flow", "trade_volume_usd", "change_1d",
              "funding_rate", "netflow_1d", "24h_change_percent"):
        assert L.is_rolling_named(f), f

def test_rolling_name_does_not_match_config() -> None:
    for f in ("peer_count", "height", "chain_id", "market_price_usd",
              "block_number", "difficulty", "timestamp"):
        assert not L.is_rolling_named(f), f


# ---------- classifier: (b) DEAD field --------------------------------

def _probe(hits_vals: list[dict[str, Any]]) -> dict[str, Any]:
    """Build a probe dict from a list of {field: value} dicts."""
    return {
        "url": "https://api.example.com/x",
        "hits": [{"i": i, "ok": True, "vals": v, "status": 200, "error": None}
                 for i, v in enumerate(hits_vals)],
        "n_hits": len(hits_vals),
        "digest_fields": list(hits_vals[0].keys()) if hits_vals else [],
    }


def test_blockcypher_peer_count_dead_trips_rule_b() -> None:
    """FIXTURE: BlockCypher /v1/eth/main peer_count observed at 0
    across 5 hits in an earlier operator probe. Rule (b) fires
    regardless of the height control moving."""
    probe = _probe([
        {"peer_count": 0, "height": 25474160},
        {"peer_count": 0, "height": 25474175},
        {"peer_count": 0, "height": 25474190},
        {"peer_count": 0, "height": 25474205},
    ])
    v = L.classify_probe(probe, ["peer_count", "height"])
    assert v["outcome"] == "reject"
    assert v["rule"] == "b"
    assert "peer_count" in v["dead_fields"]


def test_blockchain_info_miners_revenue_dead_trips_rule_b() -> None:
    """FIXTURE: blockchain.info /stats miners_revenue_usd=0.0 on all
    5 hits. Rule (b) fires — dead-field precedence over frozen-rolling
    (trade_volume_usd was also frozen but b > a)."""
    probe = _probe([
        {"miners_revenue_usd": 0.0, "trade_volume_usd": 209193740.89},
        {"miners_revenue_usd": 0.0, "trade_volume_usd": 209193740.89},
        {"miners_revenue_usd": 0.0, "trade_volume_usd": 209193740.89},
        {"miners_revenue_usd": 0.0, "trade_volume_usd": 209193740.89},
    ])
    v = L.classify_probe(probe, ["miners_revenue_usd", "trade_volume_usd"])
    assert v["outcome"] == "reject"
    assert v["rule"] == "b"


def test_defillama_rolling_frozen_trips_rule_a() -> None:
    """FIXTURE: api.llama.fi /overview/dexs total24h identical across
    5 hits — the operator's manual FROZEN verdict. Rule (a) fires;
    no field is zero so (b) doesn't preempt."""
    v = 6565792268
    probe = _probe([
        {"total24h": v, "total7d": v, "total30d": v, "change_1d": 40.32},
        {"total24h": v, "total7d": v, "total30d": v, "change_1d": 40.32},
        {"total24h": v, "total7d": v, "total30d": v, "change_1d": 40.32},
        {"total24h": v, "total7d": v, "total30d": v, "change_1d": 40.32},
    ])
    verdict = L.classify_probe(probe, ["total24h", "total7d", "total30d", "change_1d"])
    assert verdict["outcome"] == "reject"
    assert verdict["rule"] == "a"
    assert "total24h" in verdict["frozen_fields"]


def test_live_moving_fixture_passes() -> None:
    """Every field moves at least once → pass."""
    probe = _probe([
        {"total24h": 100, "peer_count": 42, "height": 1000},
        {"total24h": 105, "peer_count": 42, "height": 1001},
        {"total24h": 110, "peer_count": 43, "height": 1002},
        {"total24h": 108, "peer_count": 42, "height": 1003},
    ])
    v = L.classify_probe(probe, ["total24h", "peer_count", "height"])
    assert v["outcome"] == "pass"


def test_non_rolling_static_fires_warn_path_c() -> None:
    """peer_count (non-rolling) constant at NON-ZERO across the
    window → WARN, not REJECT — legitimate for static config."""
    probe = _probe([
        {"peer_count": 42, "height": 1000},
        {"peer_count": 42, "height": 1001},
        {"peer_count": 42, "height": 1002},
        {"peer_count": 42, "height": 1003},
    ])
    v = L.classify_probe(probe, ["peer_count", "height"])
    assert v["outcome"] == "warn"
    assert v["rule"] == "c"
    assert "peer_count" in v["static_fields"]


def test_precedence_dead_before_rolling_frozen() -> None:
    """Rule (b) takes precedence over (a) — a frozen zero on a
    rolling field is severe as a dead field."""
    probe = _probe([
        {"total24h": 0},
        {"total24h": 0},
        {"total24h": 0},
        {"total24h": 0},
    ])
    v = L.classify_probe(probe, ["total24h"])
    assert v["outcome"] == "reject"
    assert v["rule"] == "b"  # not "a"


def test_error_hit_disqualifies_frozen_verdict() -> None:
    """A field with an error-hit can't be called frozen — we don't
    know if it would have moved. Falls through to pass."""
    probe = {
        "url": "https://x/y",
        "hits": [
            {"i": 0, "ok": False, "vals": {"total24h": "error:HTTP 502"},
             "status": 502, "error": "non-json"},
            {"i": 1, "ok": True, "vals": {"total24h": 100}, "status": 200, "error": None},
            {"i": 2, "ok": True, "vals": {"total24h": 100}, "status": 200, "error": None},
            {"i": 3, "ok": True, "vals": {"total24h": 100}, "status": 200, "error": None},
        ],
        "n_hits": 4, "digest_fields": ["total24h"],
    }
    v = L.classify_probe(probe, ["total24h"])
    assert v["outcome"] == "pass"


# ---------- scheduler: 4 hits, gap, concurrency ------------------------

@pytest.mark.asyncio
async def test_scheduler_fires_four_hits_at_advancing_time() -> None:
    """Mocked clock — 4 hits at t=0/150/300/450 with the correct
    inter-hit spacing."""
    calls: list[float] = []
    now = [0.0]

    def _now() -> float:
        return now[0]

    async def _sleep(secs: float) -> None:
        calls.append(secs)
        now[0] += secs

    async def _one_hit(url: str, timeout: float = 15.0) -> dict[str, Any]:
        return {"ok": True, "status": 200, "body": {"x": now[0]}}

    with patch.object(L, "_one_hit", _one_hit):
        probe = await L.run_liveness_probe(
            "https://api.example.com/x", ["x"],
            hits=4, gap_secs=150, sleep_fn=_sleep, now_fn=_now,
        )
    # Three inter-hit sleeps of ~150s each.
    assert len(calls) == 3
    for s in calls:
        assert s == 150.0
    assert len(probe["hits"]) == 4


@pytest.mark.asyncio
async def test_scheduler_treats_http_error_as_value() -> None:
    """A 401 becomes the value string 'error:...' for that field on
    that hit — never crashes the scheduler."""
    async def _one_hit(url: str, timeout: float = 15.0) -> dict[str, Any]:
        return {"ok": False, "status": 401, "error": "non-2xx status 401"}

    async def _no_sleep(_secs: float) -> None:
        return None

    with patch.object(L, "_one_hit", _one_hit):
        probe = await L.run_liveness_probe(
            "https://api.example.com/x", ["market_price_usd"],
            hits=4, gap_secs=0, sleep_fn=_no_sleep,
        )
    for hit in probe["hits"]:
        v = hit["vals"]["market_price_usd"]
        assert isinstance(v, str) and v.startswith("error:")


@pytest.mark.asyncio
async def test_scheduler_runs_concurrently_with_gate_tests_window() -> None:
    """Concurrency shape: launching probe as a task and then awaiting
    a separate 'gate_tests' coroutine that dominates the window; both
    complete without extending wall time beyond the longer one."""
    async def _no_sleep(_secs: float) -> None:
        return None

    async def _one_hit(url: str, timeout: float = 15.0) -> dict[str, Any]:
        return {"ok": True, "status": 200, "body": {"x": 1}}

    async def _gate_tests_stub() -> str:
        await asyncio.sleep(0)  # yield to allow probe to progress
        return "pending_approval"

    with patch.object(L, "_one_hit", _one_hit):
        probe_task = asyncio.create_task(L.run_liveness_probe(
            "https://api.example.com/x", ["x"],
            hits=4, gap_secs=0, sleep_fn=_no_sleep,
        ))
        gate_result = await _gate_tests_stub()
        probe = await probe_task
    assert gate_result == "pending_approval"
    assert len(probe["hits"]) == 4


# ---------- lifecycle wiring ------------------------------------------

def test_rejected_static_status_registered() -> None:
    from self_modify import proposals as P
    assert P.STATUS_REJECTED_STATIC == "rejected_static"
    assert P.STATUS_REJECTED_STATIC in P.ALL_STATUSES
    assert P.STATUS_REJECTED_STATIC in P.NEGATIVE_LIST_STATUSES


def test_rejected_static_wired_into_retry_eligibility() -> None:
    from self_modify import reflect as R
    assert "rejected_static" in R._RETRY_ELIGIBLE_OUTCOMES
    assert R._OUTCOME_TO_STATUS.get("rejected_static") == "rejected_static"


@pytest.mark.asyncio
async def test_list_shaped_body_with_frozen_rolling_field_now_rejects() -> None:
    """RETRO FIXTURE — the 1182ee96 hole.

    Before Phase C, the probe read fields directly off ``body`` and
    a list-shaped body (fapi.binance.com returns a list-of-dicts)
    yielded ``body.get(field) → None`` on every hit. The probe
    reported all fields as ``error:HTTP 200`` and the classifier
    fail-open discipline concluded PASS.

    Post-fix: extraction goes through the shared template contract,
    so ``list[0]`` is unwrapped and the rolling field's frozen value
    surfaces as a rule-(a) reject.
    """
    async def _one_hit(url: str, timeout: float = 15.0) -> dict[str, Any]:
        return {"ok": True, "status": 200, "body": [
            {"symbol": "BTCUSDT", "longShortRatio": "1.42",
             "volume_24h": 6565792268, "timestamp": 1720000000},
        ]}

    async def _no_sleep(_secs: float) -> None:
        return None

    with patch.object(L, "_one_hit", _one_hit):
        probe = await L.run_liveness_probe(
            "https://api.example.com/list",
            ["symbol", "longShortRatio", "volume_24h", "timestamp"],
            hits=4, gap_secs=0, sleep_fn=_no_sleep,
        )
    # The extraction contract unwrapped list[0] on each hit — values
    # are now visible.
    for hit in probe["hits"]:
        assert hit["vals"]["volume_24h"] == 6565792268
        assert hit["vals"]["longShortRatio"] == "1.42"

    v = L.classify_probe(probe, ["symbol", "longShortRatio",
                                  "volume_24h", "timestamp"])
    assert v["outcome"] == "reject"
    assert v["rule"] == "a"
    assert "volume_24h" in v["frozen_fields"]


@pytest.mark.asyncio
async def test_list_shaped_body_with_live_moving_fields_passes() -> None:
    """Symmetric fixture — a list-shaped endpoint whose fields DO
    move across hits must still pass. Locks the fix's fail-open
    discipline: unwrap the shape, but only reject on real freeze."""
    hits_bodies = [
        [{"volume_24h": 100, "longShortRatio": "1.20"}],
        [{"volume_24h": 105, "longShortRatio": "1.22"}],
        [{"volume_24h": 108, "longShortRatio": "1.19"}],
        [{"volume_24h": 110, "longShortRatio": "1.25"}],
    ]
    call_i = [0]

    async def _one_hit(url: str, timeout: float = 15.0) -> dict[str, Any]:
        b = hits_bodies[call_i[0]]
        call_i[0] += 1
        return {"ok": True, "status": 200, "body": b}

    async def _no_sleep(_secs: float) -> None:
        return None

    with patch.object(L, "_one_hit", _one_hit):
        probe = await L.run_liveness_probe(
            "https://api.example.com/list",
            ["volume_24h", "longShortRatio"],
            hits=4, gap_secs=0, sleep_fn=_no_sleep,
        )
    v = L.classify_probe(probe, ["volume_24h", "longShortRatio"])
    assert v["outcome"] == "pass"


@pytest.mark.asyncio
async def test_nested_data_wrapper_extraction_reaches_probe() -> None:
    """``{"data": [{...}]}`` fallback in the extraction contract —
    the probe reaches ``data[0]`` fields."""
    async def _one_hit(url: str, timeout: float = 15.0) -> dict[str, Any]:
        return {"ok": True, "status": 200, "body": {
            "data": [{"total24h": 0, "n_tx": 42}],
        }}

    async def _no_sleep(_secs: float) -> None:
        return None

    with patch.object(L, "_one_hit", _one_hit):
        probe = await L.run_liveness_probe(
            "https://api.example.com/nested",
            ["total24h", "n_tx"],
            hits=4, gap_secs=0, sleep_fn=_no_sleep,
        )
    # total24h is 0 across all hits → rule (b) dead field
    v = L.classify_probe(probe, ["total24h", "n_tx"])
    assert v["outcome"] == "reject"
    assert v["rule"] == "b"
    assert "total24h" in v["dead_fields"]


@pytest.mark.asyncio
async def test_extraction_failure_marks_hit_as_error_unknown() -> None:
    """Body was JSON but neither dict-with-fields nor list-of-dicts —
    the fail-open discipline treats it as unknown (per-field error
    marker); classifier PASSes because no observation supports
    frozen/dead judgment."""
    async def _one_hit(url: str, timeout: float = 15.0) -> dict[str, Any]:
        # Body is a JSON scalar — neither dict nor list.
        return {"ok": True, "status": 200, "body": 42}

    async def _no_sleep(_secs: float) -> None:
        return None

    with patch.object(L, "_one_hit", _one_hit):
        probe = await L.run_liveness_probe(
            "https://api.example.com/scalar",
            ["x"], hits=4, gap_secs=0, sleep_fn=_no_sleep,
        )
    for hit in probe["hits"]:
        v = hit["vals"]["x"]
        assert isinstance(v, str) and v.startswith("error:")
    result = L.classify_probe(probe, ["x"])
    assert result["outcome"] == "pass"


def test_liveness_probe_summary_helper() -> None:
    """The retry corrective prompt renders the observed hit values —
    the model's lever is a different endpoint."""
    from self_modify.reflect import _liveness_probe_summary
    probe = _probe([
        {"peer_count": 0, "height": 25474160},
        {"peer_count": 0, "height": 25474175},
    ])
    s = _liveness_probe_summary(probe, ["peer_count", "height"])
    assert "peer_count=[0,0]" in s
    assert "25474160" in s
