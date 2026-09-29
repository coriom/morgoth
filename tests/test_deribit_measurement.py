"""Deribit coverage and source/unit correctness without HTTP, DB or inference."""
from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from analysis.measurement_coverage import measurement_blind_spots, FIELD_MAPS, TOOL_MAPS
from analysis.source_attribution import citations, measure_thesis, references_from_objective
from core.domain import current_domain
from core.field_confusion import phrase_to_field, phrase_to_fields
from core.numeric_fidelity import check_thesis

D = "get_deribit_btc_perpetual"
B = "get_bitcoin_futures_funding"


def measure(claim, deribit=None, binance=None, evidence=None, **kwargs):
    payloads = {}
    if deribit is not None:
        payloads[D] = deribit if isinstance(deribit, dict) else {"funding_current_rate": deribit}
    if binance is not None:
        payloads[B] = binance if isinstance(binance, dict) else {"lastFundingRate": binance}
    return measure_thesis({"claim": claim, "evidence": evidence or []}, payloads, **kwargs)


def errors(result, name):
    return [r for r in result["citations"] if r[name]]


def test_case_1_claim_source_beats_evidence_tool():
    r = measure("Deribit funding 0.0001", .00005, .0001,
                [{"source": B, "detail": "funding 0.0001"}])
    hit = errors(r, "source_misattribution")
    assert len(hit) == 1 and hit[0]["source"] == "Deribit"
    assert hit[0]["reference"]["field"] == "lastFundingRate"
    assert not r["cross_source"]


@pytest.mark.parametrize("boundary", ["while", "whereas", "vs", "versus", "than", "compared to", "compared with", "against", "and", "but", "or", ",", ";"])
def test_case_2_swapped_sources_across_clause_boundaries(boundary):
    r = measure(f"Deribit's funding is 0.0001 {boundary} Binance's is 0.00005", .00005, .0001)
    assert [x["source"] for x in errors(r, "source_misattribution")] == ["Deribit", "Binance"]


def test_case_3_own_source_written_precision_wins():
    r = measure("Deribit funding 0.0001", .000104, .0001)
    assert r["citations"][0]["verdict"] == "CLEAN"
    assert not errors(r, "source_misattribution")


@pytest.mark.parametrize("evidence,source", [([], None), ([{"source": B, "detail": "spread 0.00005"}], "Binance"), ([{"source": B, "detail": "0.00005"}, {"source": D, "detail": "0.00005"}], None)])
def test_case_4_two_aliases_never_guess(evidence, source):
    r = measure("Binance-Deribit funding spread 0.00005", .00005, .0001, evidence)
    claim = r["citations"][0]
    assert claim["source"] == source
    assert claim["attribution"] == ("unattributed" if source is None else "evidence")
    assert not r["cross_source"]


def test_case_5_named_unread_source():
    r = measure("Deribit funding 0.0001", None, .0001)
    assert len(errors(r, "source_misattribution")) == 1
    assert r["citations"][0]["reason"] == "source_not_read"


def test_case_6_zero_band_is_clean():
    r = measure("Deribit funding = 0 inside the ±0.025% zero band", 0, .0001)
    assert not errors(r, "source_misattribution")
    assert not errors(r, "unit_mismatch")
    assert r["citations"][0]["verdict"] == "CLEAN"


def test_case_7_generic_funding_accepts_eight_hour_field():
    r = measure("Deribit funding rate 0.0001", {"funding_current_rate": 0, "funding_8h_rate": .000104}, .00005)
    assert r["citations"][0]["verdict"] == "CLEAN"
    assert r["citations"][0]["reference"]["field"] == "funding_8h_rate"
    assert phrase_to_field(D, "funding rate") is None
    assert phrase_to_fields(D, "funding rate") == {"funding_current_rate", "funding_8h_rate"}


def test_case_8_btc_volume_claimed_in_dollars():
    r = measure("Deribit 24h volume $15,000", {"volume_24h_btc": 15000.0})
    assert len(r["citations"]) == 1  # no phantom 24-hour/window citation
    assert len(errors(r, "unit_mismatch")) == 1
    assert r["citations"][0]["reference"]["unit"] == "btc"


def test_reverse_unit_error_usd_claimed_as_btc():
    r = measure("Deribit open interest 15000 BTC", {"open_interest_usd": 15000})
    assert len(errors(r, "unit_mismatch")) == 1


@pytest.mark.parametrize("claim,expect_error", [("Deribit funding 0.0001%", True), ("Deribit funding 0.01%", False), ("Deribit funding 0.01 percent", False), ("Deribit funding 0.0001", False)])
def test_fraction_and_percent_are_explicit(claim, expect_error):
    r = measure(claim, .0001)
    assert bool(errors(r, "unit_mismatch")) == expect_error
    if not expect_error:
        assert r["citations"][0]["verdict"] == "CLEAN"


def test_gate_percentage_verdict_unchanged():
    thesis = {"subject": "funding", "evidence": [{"source": D, "detail": "funding rate 0.01%"}]}
    findings = [f'- {D}: {{"funding_current_rate": 0.0001}}']
    assert check_thesis(thesis, findings).action == "drop"


def test_comparable_correct_cross_source():
    r = measure("Deribit funding 0.00005 vs Binance funding 0.0001", .00005, .0001)
    assert r["cross_source"]
    assert not errors(r, "source_misattribution")
    assert not errors(r, "unit_mismatch")


def test_two_sources_different_metrics_not_cross_source():
    r = measure("Deribit 24h volume 15000 BTC and Binance funding 0.0001", {"volume_24h_btc": 15000}, .0001)
    assert not r["cross_source"]


def test_price_subtypes_are_not_corresponding_references():
    r = measure("Deribit 24h high price 65000", {"high_24h_usd": 70000}, {"markPrice": 65000})
    assert not errors(r, "source_misattribution")
    assert r["citations"][0]["own_references"][0]["field"] == "high_24h_usd"
    assert r["citations"][0]["verdict"] == "UNVERIFIED"
    r = measure("Deribit mark price 60000 vs Binance index price 61000", {"mark_price_usd": 60000}, {"indexPrice": 61000})
    assert not r["cross_source"]
    r = measure("Deribit price 60000 vs Binance price 61000", {"mark_price_usd": 60000}, {"markPrice": 61000})
    assert r["cross_source"]


def test_field_specificity_and_phrase_boundaries():
    assert phrase_to_fields(D, "8h funding") == {"funding_8h_rate"}
    assert phrase_to_fields(D, "current funding rate") == {"funding_current_rate"}
    assert phrase_to_fields(D, "price") == {"mark_price_usd", "index_price_usd", "last_price_usd"}
    assert phrase_to_fields(D, "mark price") == {"mark_price_usd"}
    assert not phrase_to_fields(D, "repriced")
    r = measure("Deribit 8h funding 0.0001", {"funding_current_rate": .0001, "funding_8h_rate": .00005}, .0001)
    assert errors(r, "source_misattribution")


def test_generic_price_own_index_match_wins():
    r = measure("Deribit price 65000", {"mark_price_usd": 70000, "index_price_usd": 65000}, {"markPrice": 65000})
    assert r["citations"][0]["verdict"] == "CLEAN"


def test_never_infer_identity_from_prose_or_partial_name():
    r = measure("Deribitish exchange funding 0.0001", .0001, .0001)
    assert r["citations"][0]["source"] is None


def test_no_matching_evidence_value_means_unattributed():
    r = measure("funding 0.0001", .0001, .0001, [{"source": B, "detail": "funding 0.00005"}])
    assert r["citations"][0]["source"] is None


def test_no_reference_is_not_proof_of_unread_source():
    r = measure("Deribit funding 0.0001")
    assert not errors(r, "source_misattribution")
    assert r["citations"][0]["verdict"] == "UNVERIFIED"


def test_failed_read_is_not_fabricated_reference():
    refs, read = references_from_objective([{"type": "cycle_payload", "tool_results": [{"tool": D, "success": False, "error": "synthetic failure", "result": {"funding_current_rate": .0001}}]}])
    assert refs == {} and read == {D}
    r = measure_thesis({"claim": "Deribit funding 0.0001"}, refs, read_tools=read)
    assert not errors(r, "source_misattribution")


def test_any_valid_own_objective_reading_wins():
    r = measure_thesis({"claim": "Deribit funding 0.0001"}, {D: [{"funding_current_rate": .00005}, {"funding_current_rate": .000104}], B: {"lastFundingRate": .0001}})
    assert r["citations"][0]["verdict"] == "CLEAN"


def test_gate_rewrites_identifiable_not_silently_discarded():
    thesis = {"subject": "funding", "claim": "Deribit funding 0.0001", "evidence": [{"source": D, "detail": "funding 0.00008"}]}
    events = [{"subject": "funding", "tool": D, "action": "rewrite", "reason": "transcription_drift", "cited_value": .0001, "true_value": .00008}]
    r = measure_thesis(thesis, {D: {"funding_current_rate": .00008}}, gate_events=events)
    assert all(x["gate_rewrites"] for x in r["citations"])


@pytest.mark.parametrize("text,values", [("24h funding 1e-4", [.0001]), ("funding -0.00005", [-.00005]), ("volume $15,000; open interest 3.7B", [15000, 3.7]), ("8-hour funding 0", [0])])
def test_one_fidelity_number_grammar_and_original_offsets(text, values):
    result = citations(text)
    assert [c.value for c in result] == values
    for c in result:
        assert text[c.start:c.end].replace(",", "") == c.token


def test_explicit_magnitude_reuses_fidelity_scaling():
    r = measure("Deribit open interest $3.7B", {"open_interest_usd": 3.7e9})
    assert r["citations"][0]["verdict"] == "CLEAN"


@pytest.mark.parametrize("map_name", FIELD_MAPS + TOOL_MAPS)
def test_missing_deribit_metadata_is_a_warning(map_name):
    domain = current_domain()
    mutated = dict(getattr(domain, map_name)); del mutated[D]
    spots = measurement_blind_spots(replace(domain, **{map_name: mutated}), {D: tuple(domain.rail_tool_fields[D])})
    assert any(s["tool"] == D and s["map"] == map_name and s["severity"] == "warning" for s in spots)


def test_deribit_complete_and_exemption_subtraction():
    domain = current_domain()
    fields = {D: tuple(domain.rail_tool_fields[D])}
    assert measurement_blind_spots(domain, fields) == []
    mapping = {t: dict(v) for t, v in domain.field_units.items()}; mapping[D].pop("volume_24h_btc")
    modified = replace(domain, field_units=mapping)
    assert measurement_blind_spots(modified, fields)[0]["fields"] == ["volume_24h_btc"]
    exemptions = {k: dict(v) for k, v in domain.coverage_exemptions.items()}
    exemptions["field_units"][D + ".volume_24h_btc"] = "synthetic exemption for audit test"
    assert measurement_blind_spots(replace(modified, coverage_exemptions=exemptions), fields) == []


def test_unknown_unit_is_not_invented(monkeypatch):
    domain = current_domain()
    monkeypatch.setattr("analysis.source_attribution.current_domain", lambda: replace(domain, field_units={}))
    r = measure("Deribit 24h volume $15,000", {"volume_24h_btc": 15000})
    assert not errors(r, "unit_mismatch")


@pytest.mark.asyncio
async def test_recorded_response_shape_all_eleven_fields_without_http():
    """Frozen public-ticker envelope, synthetic values; no production sample needed."""
    from tools.data_feeds.get_deribit_btc_perpetual import GetDeribitBtcPerpetualTool
    fixture = json.loads((Path(__file__).parent / "fixtures/deribit_ticker_response.json").read_text())
    calls = []
    def respond(request):
        calls.append(request.url.path)
        return httpx.Response(200, json=fixture)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        config = MagicMock()
        config.permissions.permissions.can_access_internet = True
        tool = GetDeribitBtcPerpetualTool(config, client=client)
        result = await tool.execute()
    assert result["success"] is True
    assert len(result["result"]) == 11
    assert set(result["result"]) == set(current_domain().rail_tool_fields[D])
    assert all(isinstance(v, float) and not isinstance(v, bool) for v in result["result"].values())
    assert result["result"]["funding_current_rate"] == 0.0
    assert result["result"]["funding_8h_rate"] == .000104
    assert result["result"]["volume_24h_btc"] == 15000
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_deribit_cache_equal_cadence_is_sequential_not_synchronized(monkeypatch):
    from core import source_cache as cache
    assert cache.SOURCE_CACHE_CONFIG[D] == cache.SOURCE_CACHE_CONFIG[B] == (1800, 7200)
    monkeypatch.setattr(cache, "SOURCE_CACHE_CONFIG", {B: (1800, 7200), D: (1800, 7200)})
    monkeypatch.setenv("SOURCE_CACHE_ENABLED", "true")
    clock = [0.0]
    monkeypatch.setattr(cache.time, "monotonic", lambda: clock[0])
    calls = []
    async def collect(pm, router, source):
        calls.append((source, clock[0])); clock[0] += 2
        return True
    monkeypatch.setattr(cache, "collect_one", collect)
    state = cache.CollectorState()
    await cache.collect_due_sources(None, None, state)
    assert calls == [(B, 0), (D, 2)]
    assert state._last == {B: 2, D: 4}
    clock[0] = 1802
    assert state.due(B) and not state.due(D)


def test_comparison_comma_without_space_keeps_both_numbers():
    r = measure("Deribit funding 0.00005,Binance funding 0.0001", .00005, .0001)
    assert [c["value"] for c in r["citations"]] == [.00005, .0001]
    assert r["cross_source"]


def test_multiple_fields_in_one_clause_are_number_local():
    r = measure("Deribit mark price 60000 index price 61000", {"mark_price_usd": 60000, "index_price_usd": 61000}, {"markPrice": 60000})
    assert [c["verdict"] for c in r["citations"]] == ["CLEAN", "CLEAN"]
    assert [c["reference"]["field"] for c in r["citations"]] == ["mark_price_usd", "index_price_usd"]


def test_rail_coverage_warning_does_not_change_health_verdict(monkeypatch):
    from analysis import rail_health
    monkeypatch.setattr("analysis.measurement_coverage.measurement_blind_spots", lambda: [{"tool": D, "map": "field_units", "fields": ["volume_24h_btc"], "severity": "warning"}])
    result = rail_health.classify(D, {"success": True, "result": {"volume_24h_btc": 15000}}, ("volume_24h_btc",), None)
    assert result.status == "OK"
    assert "WARN measurement coverage" in rail_health.render_table([result])


def test_unknown_source_alias_map_is_reported():
    domain = current_domain()
    aliases = dict(domain.source_aliases); aliases.pop("Deribit")
    assert any(r["map"] == "source_aliases" for r in measurement_blind_spots(replace(domain, source_aliases=aliases), {D: tuple(domain.rail_tool_fields[D])}))


def test_real_historical_funding_control_stays_31_genuine_16_confused():
    """Numeric-only references captured before edits; no production DB in tests."""
    from collections import Counter
    from analysis.campaign_quality import _close
    rows = json.loads((Path(__file__).parent / "fixtures/historical_funding_control.json").read_text())["rows"]
    verdicts = Counter()
    for row in rows:
        verdict = "genuine" if _close(row["cited"], row["reference_rate"], .01) else "confused"
        assert verdict == row["verdict"]
        verdicts[verdict] += 1
    assert verdicts == {"genuine": 31, "confused": 16}


@pytest.mark.parametrize("thesis_id,detail,value", [
    ("8a458d80-7b76-41c3-a014-777fe0a9aebe", "funding rate of 0.00009654%", .00009654),
    ("d0e1e76a-e817-4bbe-98e4-77fedc9acb56", "0.00009654% in both retrieved instances", .00009654),
    ("24baa3d1-0e7c-46b6-acef-e8bafb4d3637", "Last BTCUSDT funding rate 0.00010000%", .0001),
])
def test_manually_confirmed_real_unit_examples(thesis_id, detail, value):
    r = measure_thesis({"thesis_id": thesis_id, "subject": "BTC funding rate", "claim": "low", "evidence": [{"source": B, "detail": detail}]}, {B: {"lastFundingRate": value}})
    assert len(errors(r, "unit_mismatch")) == 1
    assert not errors(r, "source_misattribution")


@pytest.mark.asyncio
async def test_campaign_source_measurement_uses_objective_not_latest_snapshot():
    from analysis.campaign_quality import score_campaign, render_report
    class Reader:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False
        async def fetch(self, sql, *args):
            if "DISTINCT ON (source)" in sql:
                return [{"source": D, "payload": {"funding_current_rate": .0001}}, {"source": B, "payload": {"lastFundingRate": .00005}}]
            return []
        async def fetchrow(self, sql, *args):
            return {"evidence": [{"type": "cycle_payload", "tool_results": [{"tool": D, "success": True, "result": {"funding_current_rate": .00005}}, {"tool": B, "success": True, "result": {"lastFundingRate": .0001}}]}]}
    class Store:
        def _require_pool(self): return MagicMock(acquire=Reader)
        async def list_campaigns(self, **kwargs): return [{"campaign_id": "historical-test", "subject": "BTC"}]
        async def list_campaign_objectives(self, *args): return [{"objective_id": "objective-test", "title": "BTC funding", "created_at": None}]
        async def get_theses_by_objective(self, *args): return [{"thesis_id": "thesis-test", "subject": "BTC funding", "claim": "Deribit funding 0.0001", "evidence": []}]
    report = await score_campaign(Store(), "historical-test")
    assert report.measurement_counts["source_misattribution"] == 1
    assert report.measurement_examples["source_misattribution"][0]["reference"]["value"] == .0001
    assert "SOURCE / UNIT MEASUREMENT" in render_report(report)
