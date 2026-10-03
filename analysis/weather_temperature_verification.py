"""V1 MET-to-NWS temperature verification over persisted, immutable facts.

Pure computation only: callers supply a coherent database corpus and an explicit
evaluation clock. These errors describe provider forecasts, not Morgoth's LLM.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from typing import Any, Mapping

from core.temporal_facts import _dimensions, _utc


SCORER_ID = "weather_temperature_verification"
POLICY_VERSION = "temperature_met_nws_v1"
FORECAST_SOURCE = "MET Norway"
OBSERVATION_SOURCE = "NWS"
METRIC = "temperature"
UNIT = "celsius"
EARTH_RADIUS_KM = 6371.0088
STATUSES = (
    "MATCHED", "RETROSPECTIVE", "NOT_DUE", "INVALID_VALUE",
    "UNIT_INCOMPATIBLE", "MISSING_COORDINATES", "NO_OBSERVATION",
    "NO_POST_ACQUISITION_OBSERVATION", "TOO_FAR_IN_TIME", "TOO_FAR",
)


@dataclass(frozen=True, slots=True)
class VerificationOptions:
    """Explicit immutable V1 policy and evaluation clock."""

    from_at: datetime
    to_at: datetime
    as_of: datetime
    max_station_distance_km: float = 10.0
    max_observation_offset_seconds: int = 1800

    def __post_init__(self) -> None:
        for name in ("from_at", "to_at", "as_of"):
            object.__setattr__(self, name, _utc(getattr(self, name), name))
        if self.from_at >= self.to_at:
            raise ValueError("verification interval must satisfy from < to")
        distance = self.max_station_distance_km
        if (isinstance(distance, bool) or not isinstance(distance, (int, float))
                or not math.isfinite(distance) or not 0 < distance <= 1000):
            raise ValueError("invalid maximum station distance")
        offset = self.max_observation_offset_seconds
        if isinstance(offset, bool) or not isinstance(offset, int) or not 0 < offset <= 86400:
            raise ValueError("invalid maximum observation offset")


@dataclass(frozen=True, slots=True)
class PersistedFact:
    """Read-only projection of one database row; acquired_at is the DB value."""

    semantic_key: str
    project_id: str
    domain_id: str
    kind: str
    source: str
    tool: str
    metric: str
    value: Any
    unit: str
    entity: str
    dimensions: tuple[tuple[str, Any], ...]
    acquired_at: datetime
    valid_at: datetime
    source_updated_at: datetime | None
    source_record_id: str | None
    code_version: str | None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "PersistedFact":
        """Freeze an actual query row without trusting its eligibility alias."""
        if not isinstance(row, Mapping):
            raise ValueError("invalid persisted fact record")
        try:
            raw_dimensions = row["dimensions"]
            if isinstance(raw_dimensions, str):  # asyncpg's default JSONB codec
                raw_dimensions = json.loads(raw_dimensions)
            dimensions = _dimensions(raw_dimensions)
            required = ("semantic_key", "project_id", "domain_id", "kind", "source",
                        "tool", "metric", "unit", "entity")
            if any(not isinstance(row[key], str) or not row[key] for key in required):
                raise ValueError("invalid persisted fact identity")
            if row["kind"] not in ("prediction", "observation"):
                raise ValueError("invalid persisted fact kind")
            updated = row.get("source_updated_at")
            return cls(
                semantic_key=row["semantic_key"], project_id=row["project_id"],
                domain_id=row["domain_id"], kind=row["kind"], source=row["source"],
                tool=row["tool"], metric=row["metric"], value=row.get("value"),
                unit=row["unit"], entity=row["entity"],
                dimensions=tuple(sorted(dimensions.items())),
                acquired_at=_utc(row["acquired_at"], "acquired_at"),
                valid_at=_utc(row["valid_at"], "valid_at"),
                source_updated_at=_utc(updated, "source_updated_at") if updated is not None else None,
                source_record_id=row.get("source_record_id"),
                code_version=row.get("code_version"),
            )
        except (KeyError, TypeError, json.JSONDecodeError):
            raise ValueError("invalid persisted fact record") from None


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _numeric(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def _coordinates(fact: PersistedFact) -> tuple[float, float] | None:
    values = dict(fact.dimensions)
    latitude, longitude = _numeric(values.get("latitude")), _numeric(values.get("longitude"))
    if latitude is None or longitude is None or not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
        return None
    return latitude, longitude


def _distance_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat_a, lon_a, lat_b, lon_b = map(math.radians, (*a, *b))
    latitude = lat_b - lat_a
    longitude = lon_b - lon_a
    haversine = math.sin(latitude / 2) ** 2 + math.cos(lat_a) * math.cos(lat_b) * math.sin(longitude / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(min(1.0, haversine)))


def _canonical(fact: PersistedFact) -> dict[str, Any]:
    value = _numeric(fact.value)
    return {
        "semantic_key": fact.semantic_key, "project_id": fact.project_id,
        "domain_id": fact.domain_id, "kind": fact.kind, "source": fact.source,
        "tool": fact.tool, "metric": fact.metric, "value": value,
        "value_valid": value is not None, "unit": fact.unit, "entity": fact.entity,
        "dimensions": dict(fact.dimensions), "acquired_at": _iso(fact.acquired_at),
        "valid_at": _iso(fact.valid_at),
        "source_updated_at": _iso(fact.source_updated_at) if fact.source_updated_at else None,
        "source_record_id": fact.source_record_id, "code_version": fact.code_version,
    }


def _latest_observation_revisions(observations: tuple[PersistedFact, ...]) -> tuple[PersistedFact, ...]:
    """Latest acquisition by station/time, with a key-only deterministic tie."""
    revisions: dict[tuple[str, str, datetime], PersistedFact] = {}
    for fact in observations:
        station = dict(fact.dimensions).get("station_id")
        if not isinstance(station, str) or not station:
            continue
        identity = station, fact.metric, fact.valid_at
        previous = revisions.get(identity)
        if previous is None or (fact.acquired_at, fact.semantic_key) > (previous.acquired_at, previous.semantic_key):
            revisions[identity] = fact
    return tuple(sorted(revisions.values(), key=lambda fact: (fact.valid_at, fact.semantic_key)))


def _match(prediction: PersistedFact, observations: tuple[PersistedFact, ...],
           options: VerificationOptions) -> dict[str, Any]:
    """Return one status; never use forecast error to select an observation."""
    value = _numeric(prediction.value)
    detail: dict[str, Any] = {
        "prediction_key": prediction.semantic_key, "status": None,
        "forecast_source": prediction.source, "observation_source": None,
        "station_id": None, "observation_key": None,
        "forecast_value": value, "observed_value": None, "unit": prediction.unit,
        "acquired_at": _iso(prediction.acquired_at), "valid_at": _iso(prediction.valid_at),
        "observed_at": None,
        "lead_time_seconds": (prediction.valid_at - prediction.acquired_at).total_seconds(),
        "temporal_delta_seconds": None, "station_distance_km": None,
        "signed_error": None, "absolute_error": None,
    }

    def finish(status: str) -> dict[str, Any]:
        detail["status"] = status
        return detail

    # Precedence: provenance -> maturity -> prediction validity -> acquisition
    # -> time -> coordinates -> distance -> unit -> observation value -> match.
    if prediction.acquired_at > prediction.valid_at:
        return finish("RETROSPECTIVE")
    if (options.as_of - prediction.valid_at).total_seconds() < options.max_observation_offset_seconds:
        return finish("NOT_DUE")
    if value is None:
        return finish("INVALID_VALUE")
    if prediction.unit != UNIT:
        return finish("UNIT_INCOMPATIBLE")
    forecast_coords = _coordinates(prediction)
    if forecast_coords is None:
        return finish("MISSING_COORDINATES")
    if not observations:
        return finish("NO_OBSERVATION")
    after_acquisition = tuple(obs for obs in observations if obs.valid_at > prediction.acquired_at)
    if not after_acquisition:
        return finish("NO_POST_ACQUISITION_OBSERVATION")
    in_time = tuple(obs for obs in after_acquisition
                    if abs((obs.valid_at - prediction.valid_at).total_seconds())
                    <= options.max_observation_offset_seconds)
    if not in_time:
        return finish("TOO_FAR_IN_TIME")
    with_coordinates = tuple((obs, _coordinates(obs)) for obs in in_time)
    with_coordinates = tuple((obs, coords) for obs, coords in with_coordinates if coords is not None)
    if not with_coordinates:
        return finish("MISSING_COORDINATES")
    nearby = tuple((obs, _distance_km(forecast_coords, coords)) for obs, coords in with_coordinates)
    nearby = tuple((obs, distance) for obs, distance in nearby
                   if distance <= options.max_station_distance_km)
    if not nearby:
        return finish("TOO_FAR")
    compatible = tuple((obs, distance) for obs, distance in nearby if obs.unit == UNIT)
    if not compatible:
        return finish("UNIT_INCOMPATIBLE")
    valid = tuple((obs, distance) for obs, distance in compatible if _numeric(obs.value) is not None)
    if not valid:
        return finish("INVALID_VALUE")
    observation, distance = min(
        valid,
        key=lambda item: (
            item[1], abs((item[0].valid_at - prediction.valid_at).total_seconds()),
            item[0].valid_at, str(dict(item[0].dimensions)["station_id"]), item[0].semantic_key,
        ),
    )
    observed = _numeric(observation.value)
    assert observed is not None
    error = value - observed
    detail.update({
        "observation_source": observation.source,
        "station_id": dict(observation.dimensions)["station_id"],
        "observation_key": observation.semantic_key,
        "observed_value": observed, "observed_at": _iso(observation.valid_at),
        "temporal_delta_seconds": (observation.valid_at - prediction.valid_at).total_seconds(),
        "station_distance_km": distance, "signed_error": error,
        "absolute_error": abs(error),
    })
    return finish("MATCHED")


def verify_temperature(
    fact_rows: tuple[Mapping[str, Any], ...], *, from_at: datetime, to_at: datetime,
    as_of: datetime, max_station_distance_km: float = 10.0,
    max_observation_offset_seconds: int = 1800,
) -> dict[str, Any]:
    """Score a fixed persisted corpus without clocks, I/O or mutable inputs."""
    if not isinstance(fact_rows, tuple) or any(not isinstance(row, Mapping) for row in fact_rows):
        raise ValueError("verification requires an immutable tuple of fact records")
    options = VerificationOptions(from_at, to_at, as_of, max_station_distance_km,
                                  max_observation_offset_seconds)
    margin = timedelta(seconds=options.max_observation_offset_seconds)
    parsed = tuple(PersistedFact.from_row(row) for row in fact_rows)
    scopes = {(fact.project_id, fact.domain_id) for fact in parsed}
    if len(scopes) > 1:
        raise ValueError("verification corpus contains multiple Projects or Domains")
    selected = tuple(
        fact for fact in parsed if fact.acquired_at <= options.as_of and fact.metric == METRIC
        and ((fact.kind == "prediction" and fact.source == FORECAST_SOURCE
              and options.from_at <= fact.valid_at < options.to_at)
             or (fact.kind == "observation" and fact.source == OBSERVATION_SOURCE
                 and options.from_at - margin <= fact.valid_at < options.to_at + margin))
    )
    by_key: dict[str, PersistedFact] = {}
    for fact in selected:
        previous = by_key.get(fact.semantic_key)
        if previous is not None and _canonical(previous) != _canonical(fact):
            raise ValueError("conflicting rows share a temporal fact key")
        by_key[fact.semantic_key] = fact
    selected = tuple(sorted(by_key.values(), key=lambda fact: (fact.valid_at, fact.acquired_at, fact.semantic_key)))
    canonical = [_canonical(fact) for fact in selected]
    digest = hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":"),
                                       allow_nan=False).encode("utf-8")).hexdigest()
    predictions = tuple(fact for fact in selected if fact.kind == "prediction"
                        and options.from_at <= fact.valid_at < options.to_at)
    observations = _latest_observation_revisions(
        tuple(fact for fact in selected if fact.kind == "observation")
    )
    details = [_match(prediction, observations, options) for prediction in predictions]
    statuses = {status: sum(detail["status"] == status for detail in details) for status in STATUSES}
    due = len(predictions) - statuses["RETROSPECTIVE"] - statuses["NOT_DUE"]
    matched = statuses["MATCHED"]
    errors = [detail["signed_error"] for detail in details if detail["status"] == "MATCHED"]
    targets = {(fact.entity, fact.metric, fact.valid_at,
                tuple((key, value) for key, value in fact.dimensions if key in ("latitude", "longitude")))
               for fact, detail in zip(predictions, details) if detail["status"] not in ("RETROSPECTIVE", "NOT_DUE")}
    return {
        "report_schema_version": 1, "scorer": SCORER_ID, "policy_version": POLICY_VERSION,
        "source_pair": {"forecast": FORECAST_SOURCE, "observation": OBSERVATION_SOURCE},
        "metric": METRIC, "unit": UNIT,
        "interval": {"from": _iso(options.from_at), "to_exclusive": _iso(options.to_at)},
        "as_of": _iso(options.as_of),
        "matching_options": {"max_station_distance_km": float(options.max_station_distance_km),
                             "max_observation_offset_seconds": options.max_observation_offset_seconds},
        "input_corpus_digest_sha256": digest,
        "corpus_counts": {"selected_facts": len(selected), "prediction_facts": sum(fact.kind == "prediction" for fact in selected),
                          "observation_facts": sum(fact.kind == "observation" for fact in selected),
                          "observation_revisions_available": len(observations)},
        "counts": {"candidate_predictions": len(predictions), "retrospective": statuses["RETROSPECTIVE"],
                   "not_due": statuses["NOT_DUE"], "due_prospective": due,
                   "matched_pairs": matched, "unmatched_due": due - matched,
                   "unique_due_forecast_targets": len(targets),
                   "unique_observations_used": len({detail["observation_key"] for detail in details
                                                    if detail["observation_key"] is not None})},
        "status_counts": statuses,
        "unmatched_by_reason": {key: value for key, value in statuses.items()
                                if key not in ("MATCHED", "RETROSPECTIVE", "NOT_DUE")},
        "match_rate_denominator": "due_prospective",
        "match_rate": matched / due if due else None,
        "metrics": {"mae_celsius": sum(abs(error) for error in errors) / matched if matched else None,
                    "bias_celsius": sum(errors) / matched if matched else None,
                    "rmse_celsius": math.sqrt(sum(error * error for error in errors) / matched) if matched else None},
        "revision_weighted_descriptive": True,
        "details": details,
    }
