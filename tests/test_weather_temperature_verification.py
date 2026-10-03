"""Independent synthetic arithmetic and status proofs for the pure verifier."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import math
from types import MappingProxyType

import pytest

from analysis.weather_temperature_verification import _distance_km, verify_temperature


TARGET = datetime(2026, 1, 2, 12, tzinfo=timezone.utc)
ACQUIRED = TARGET - timedelta(hours=2)
AS_OF = TARGET + timedelta(hours=1)
START = TARGET - timedelta(hours=1)
END = TARGET + timedelta(hours=1)


def _fact(label: str, *, kind: str = "prediction", value: float | None = 20.0,
          valid_at: datetime = TARGET, acquired_at: datetime = ACQUIRED,
          latitude: float | None = 0.0, longitude: float | None = 0.0,
          station: str = "KAAA", unit: str = "celsius",
          source: str | None = None) -> dict:
    """Clearly synthetic database-shaped fact; no provider history is claimed."""
    return {
        "semantic_key": hashlib.sha256(label.encode()).hexdigest(),
        "project_id": "synthetic_weather", "domain_id": "weather",
        "kind": kind, "source": source or ("MET Norway" if kind == "prediction" else "NWS"),
        "tool": "synthetic_forecast" if kind == "prediction" else "synthetic_observation",
        "metric": "temperature", "value": value, "unit": unit, "entity": "location",
        "dimensions": {"latitude": latitude, "longitude": longitude, **({"station_id": station} if kind == "observation" else {})},
        "acquired_at": acquired_at, "valid_at": valid_at,
        "source_updated_at": None, "source_record_id": f"SYNTHETIC_{label}",
        "code_version": "synthetic",
    }


def _score(*facts: dict, as_of: datetime = AS_OF, **options) -> dict:
    frozen = tuple(MappingProxyType(fact) for fact in facts)
    return verify_temperature(frozen, from_at=START, to_at=END, as_of=as_of, **options)


def _prediction(label: str = "p", **changes) -> dict:
    return _fact(label, **changes)


def _observation(label: str = "o", **changes) -> dict:
    options = {"kind": "observation", "value": 22.0,
               "acquired_at": TARGET + timedelta(minutes=5)}
    options.update(changes)
    return _fact(label, **options)


def test_signed_error_and_independent_known_aggregate():
    p1, p2 = _prediction("p1", value=20.0), _prediction("p2", value=25.0)
    report = _score(p1, p2, _observation())
    assert {row["prediction_key"]: row["signed_error"] for row in report["details"]} == {
        p1["semantic_key"]: -2.0, p2["semantic_key"]: 3.0}
    assert sorted(row["absolute_error"] for row in report["details"]) == [2.0, 3.0]
    assert report["metrics"]["mae_celsius"] == 2.5
    assert report["metrics"]["bias_celsius"] == 0.5
    assert report["metrics"]["rmse_celsius"] == pytest.approx(math.sqrt(6.5))
    assert report["counts"]["candidate_predictions"] == 2
    assert report["counts"]["unique_due_forecast_targets"] == 1
    assert report["counts"]["unique_observations_used"] == 1
    assert report["counts"]["matched_pairs"] == 2


def test_no_match_has_null_error_metrics():
    report = _score(_prediction())
    assert report["status_counts"]["NO_OBSERVATION"] == 1
    assert report["metrics"] == {"mae_celsius": None, "bias_celsius": None, "rmse_celsius": None}
    assert report["match_rate"] == 0.0
    assert _score()["match_rate"] is None


def test_retrospective_and_not_due_do_not_become_missing_observations():
    retrospective = _prediction("retro", acquired_at=TARGET + timedelta(minutes=1))
    report = _score(retrospective, _prediction("pending"),
                    as_of=TARGET + timedelta(minutes=10))
    assert report["status_counts"]["RETROSPECTIVE"] == 1
    assert report["status_counts"]["NOT_DUE"] == 1
    assert report["counts"]["due_prospective"] == 0
    assert report["counts"]["unmatched_due"] == 0


def test_time_and_distance_bounds_are_both_required():
    late = _observation("late", valid_at=TARGET + timedelta(minutes=31))
    assert _score(_prediction(), late)["details"][0]["status"] == "TOO_FAR_IN_TIME"
    far = _observation("far", latitude=0.10)
    assert _score(_prediction(), far)["details"][0]["status"] == "TOO_FAR"
    assert _score(_prediction(), late, far)["details"][0]["status"] == "TOO_FAR"
    assert _score(_prediction(), far, max_station_distance_km=12)["details"][0]["status"] == "MATCHED"
    assert _score(_prediction(), late, max_observation_offset_seconds=31 * 60)["details"][0]["status"] == "MATCHED"


def test_missing_coordinates_and_units_are_explicit():
    assert _score(_prediction(latitude=None), _observation())["details"][0]["status"] == "MISSING_COORDINATES"
    assert _score(_prediction(), _observation(latitude=None))["details"][0]["status"] == "MISSING_COORDINATES"
    assert _score(_prediction(unit="fahrenheit"), _observation())["details"][0]["status"] == "UNIT_INCOMPATIBLE"
    assert _score(_prediction(), _observation(unit="fahrenheit"))["details"][0]["status"] == "UNIT_INCOMPATIBLE"


def test_invalid_values_never_create_numeric_scores():
    assert _score(_prediction(value=math.nan), _observation())["details"][0]["status"] == "INVALID_VALUE"
    assert _score(_prediction(), _observation(value=None))["details"][0]["status"] == "INVALID_VALUE"


def test_nearer_station_wins_even_when_its_error_is_worse():
    near_bad = _observation("near", value=30.0, latitude=0.01, station="KNEAR")
    far_perfect = _observation("far", value=20.0, latitude=0.04, station="KFAR")
    report = _score(_prediction(), far_perfect, near_bad)
    assert report["details"][0]["station_id"] == "KNEAR"
    assert report["details"][0]["signed_error"] == -10


def test_nearest_time_wins_after_equal_station_distance():
    early = _observation("early_near_time", station="KONE", value=24.0,
                         valid_at=TARGET - timedelta(minutes=3))
    late = _observation("late_far_time", station="KTWO", value=20.0,
                        valid_at=TARGET + timedelta(minutes=8))
    report = _score(_prediction(), late, early)
    assert report["details"][0]["observation_key"] == early["semantic_key"]
    assert report["details"][0]["temporal_delta_seconds"] == -180


def test_stable_tie_break_and_input_order_independence():
    early = _observation("early", station="KBBB", valid_at=TARGET - timedelta(minutes=2), value=24.0)
    later = _observation("later", station="KAAA", valid_at=TARGET + timedelta(minutes=2), value=30.0)
    first = _score(_prediction(), later, early)
    second = _score(early, _prediction(), later)
    assert first == second
    assert first["details"][0]["observation_key"] == early["semantic_key"]
    assert first["details"][0]["temporal_delta_seconds"] == -120


def test_pre_acquisition_observation_cannot_verify_forecast():
    prediction = _prediction(acquired_at=TARGET - timedelta(minutes=5))
    observation = _observation(valid_at=TARGET - timedelta(minutes=10))
    report = _score(prediction, observation)
    assert report["details"][0]["status"] == "NO_POST_ACQUISITION_OBSERVATION"
    assert report["counts"]["matched_pairs"] == 0


def test_observation_revisions_use_latest_acquired_by_as_of_not_best_error():
    old = _observation("old", value=20.0, acquired_at=TARGET + timedelta(minutes=2))
    new = _observation("new", value=30.0, acquired_at=TARGET + timedelta(minutes=20))
    future = _observation("future", value=22.0, acquired_at=TARGET + timedelta(hours=2))
    report = _score(_prediction(), old, new, future)
    assert report["details"][0]["observation_key"] == new["semantic_key"]
    assert report["details"][0]["signed_error"] == -10
    assert report["corpus_counts"]["observation_revisions_available"] == 1


def test_repeated_input_deduplicates_but_real_forecast_revisions_remain_distinct():
    first = _prediction("rev1")
    second = _prediction("rev2", acquired_at=ACQUIRED + timedelta(hours=1))
    observation = _observation()
    report = _score(first, first.copy(), second, observation, observation.copy())
    assert report["counts"]["candidate_predictions"] == 2
    assert report["counts"]["matched_pairs"] == 2
    assert report["counts"]["unique_due_forecast_targets"] == 1
    assert report["corpus_counts"]["selected_facts"] == 3


def test_all_prediction_statuses_reconcile():
    report = _score(_prediction("match"), _prediction("retro", acquired_at=TARGET + timedelta(minutes=1)),
                    _prediction("invalid", value=math.nan), _observation())
    assert sum(report["status_counts"].values()) == report["counts"]["candidate_predictions"]
    assert report["counts"]["due_prospective"] + report["counts"]["retrospective"] + report["counts"]["not_due"] == report["counts"]["candidate_predictions"]


def test_haversine_and_option_validation():
    assert _distance_km((0.0, 0.0), (1.0, 0.0)) == pytest.approx(111.195, rel=0.0001)
    with pytest.raises(ValueError, match="distance"):
        _score(_prediction(), max_station_distance_km=float("inf"))
    with pytest.raises(ValueError, match="offset"):
        _score(_prediction(), max_observation_offset_seconds=0)
    with pytest.raises(ValueError, match="interval"):
        verify_temperature((), from_at=END, to_at=START, as_of=AS_OF)


def test_as_of_excludes_later_acquisitions():
    late_prediction = _prediction(acquired_at=AS_OF + timedelta(seconds=1))
    report = _score(late_prediction)
    assert report["counts"]["candidate_predictions"] == 0
    assert report["corpus_counts"]["selected_facts"] == 0
