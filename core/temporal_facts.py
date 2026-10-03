"""Generic, bounded temporal facts captured prospectively from declared tool fields.

No provider controls acquired_at in persistence: PostgreSQL assigns it at INSERT.
The in-memory acquisition time is for immediate diagnostics only.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Mapping


_NAME = re.compile(r"[a-z][a-z0-9_]*\Z")
_SENSITIVE = re.compile(r"(?:secret|token|password|credential|api_key|auth)", re.I)


def _utc(value: Any, label: str) -> datetime:
    """Require an explicitly timezone-aware instant and normalize to UTC."""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError(f"invalid {label}") from None
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"invalid {label}")
    return value.astimezone(timezone.utc)


def _dimensions(value: Mapping[str, Any]) -> dict[str, str | float | int | bool | None]:
    """Accept only a small flat, deterministic and non-sensitive context."""
    if not isinstance(value, Mapping) or not 1 <= len(value) <= 12:
        raise ValueError("invalid fact dimensions")
    result: dict[str, str | float | int | bool | None] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not _NAME.fullmatch(key) or _SENSITIVE.search(key):
            raise ValueError("invalid fact dimension name")
        if isinstance(item, str):
            if len(item) > 128 or _SENSITIVE.search(item):
                raise ValueError("invalid fact dimension value")
        elif isinstance(item, float):
            if not math.isfinite(item):
                raise ValueError("non-finite fact dimension")
        elif not (item is None or isinstance(item, (int, bool))):
            raise ValueError("nested fact dimensions are forbidden")
        result[key] = item
    if len(json.dumps(result, sort_keys=True, separators=(",", ":"))) > 1024:
        raise ValueError("fact dimensions exceed size limit")
    return result


@dataclass(frozen=True)
class TemporalFact:
    """One project-owned scalar prediction or observation at a valid time."""

    kind: str
    project_id: str
    domain_id: str
    source: str
    tool: str
    metric: str
    value: float
    unit: str
    entity: str
    dimensions: Mapping[str, Any]
    acquired_at: datetime
    valid_at: datetime
    source_updated_at: datetime | None = None
    source_record_id: str | None = None
    code_version: str | None = None
    semantic_key: str = field(init=False)

    def __post_init__(self) -> None:
        if self.kind not in {"prediction", "observation"}:
            raise ValueError("invalid temporal fact kind")
        for label in ("project_id", "domain_id", "tool", "metric", "entity"):
            item = getattr(self, label)
            if not isinstance(item, str) or not _NAME.fullmatch(item) or len(item) > 128:
                raise ValueError(f"invalid temporal fact {label}")
        if not self.source or len(self.source) > 128 or not self.unit or not _NAME.fullmatch(self.unit):
            raise ValueError("invalid temporal fact source or unit")
        if isinstance(self.value, bool) or not isinstance(self.value, (int, float)) or not math.isfinite(self.value):
            raise ValueError("invalid temporal fact value")
        if self.source_record_id is not None and (not self.source_record_id or len(self.source_record_id) > 256):
            raise ValueError("invalid source record identity")
        dims = _dimensions(self.dimensions)
        object.__setattr__(self, "dimensions", MappingProxyType(dims))
        object.__setattr__(self, "acquired_at", _utc(self.acquired_at, "acquired_at"))
        object.__setattr__(self, "valid_at", _utc(self.valid_at, "valid_at"))
        if self.source_updated_at is not None:
            object.__setattr__(self, "source_updated_at", _utc(self.source_updated_at, "source_updated_at"))
        identity = [self.kind, self.project_id, self.domain_id, self.source, self.tool,
                    self.metric, float(self.value), self.unit, self.entity, dims,
                    self.valid_at.isoformat(),
                    self.source_updated_at.isoformat() if self.source_updated_at else None,
                    self.source_record_id]
        encoded = json.dumps(identity, sort_keys=True, separators=(",", ":"), allow_nan=False)
        object.__setattr__(self, "semantic_key", hashlib.sha256(encoded.encode()).hexdigest())

    @property
    def prospective_eligible(self) -> bool:
        """Only predictions captured by Morgoth by their target time qualify."""
        return self.kind == "prediction" and self.acquired_at <= self.valid_at


def extract_temporal_facts(domain: Any, project: Any, tool: str, payload: Mapping[str, Any],
                           acquired_at: datetime, code_version: str | None = None) -> tuple[TemporalFact, ...]:
    """Extract only Domain-declared facts; malformed declared data fails explicitly."""
    rules = domain.fact_captures.get(tool, ())
    if not rules:
        return ()
    if tool not in domain.rail_tools or not isinstance(payload, Mapping):
        raise ValueError("inactive tool or malformed fact payload")
    facts: list[TemporalFact] = []
    for rule in rules:
        records = payload.get(rule.records) if rule.records else [payload]
        if not isinstance(records, list) or len(records) > 256:
            raise ValueError("invalid temporal fact records")
        for record in records:
            if not isinstance(record, Mapping):
                raise ValueError("invalid temporal fact record")
            value = record.get(rule.field)
            if value is None:  # Providers may explicitly report missing measurements.
                continue
            valid_at = record.get(rule.valid_at)
            unit = domain.field_units.get(tool, {}).get(rule.field)
            if not unit or valid_at is None:
                raise ValueError("temporal fact missing declared unit or valid_at")
            dimensions = {key: payload.get(path) for key, path in rule.dimensions.items()}
            if any(value is None for value in dimensions.values()):
                raise ValueError("temporal fact missing identity dimension")
            updated = payload.get(rule.source_updated_at) if rule.source_updated_at else None
            record_id = record.get(rule.source_record_id) if rule.source_record_id else None
            facts.append(TemporalFact(
                kind=rule.kind, project_id=project.id, domain_id=domain.name,
                source=domain.tool_sources[tool], tool=tool, metric=rule.metric,
                value=value, unit=unit, entity=rule.entity, dimensions=dimensions,
                acquired_at=acquired_at, valid_at=_utc(valid_at, "valid_at"),
                source_updated_at=_utc(updated, "source_updated_at") if updated is not None else None,
                source_record_id=str(record_id) if record_id is not None else None,
                code_version=code_version,
            ))
    return tuple(facts)
