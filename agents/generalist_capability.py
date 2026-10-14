"""Opt-in declarative fixture capabilities; no autonomous runtime registration.

This module interprets a deliberately small data-only API contract. It never
imports code, opens a network connection, or turns documentation into authority.
"""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


class CapabilityRefusal(ValueError):
    """Fixed, non-sensitive refusal code for a fixture capability."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


class DeclarativeOperation(BaseModel):
    """One bounded fixture operation; its kind selects only prebuilt behavior."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    operation_id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,47}$")
    kind: Literal["list", "reorder", "report"]
    scope: Literal["fixture.read", "fixture.write", "fixture.report"]
    result_type: Literal["records", "ack", "report"]

    @model_validator(mode="after")
    def compatible_shape(self) -> "DeclarativeOperation":
        """Reject contracts that relabel a privileged action as a read."""
        if re.fullmatch(r"[a-z][a-z0-9_]{0,47}", self.operation_id) is None:
            raise ValueError("invalid operation identifier")
        expected = {
            "list": ("fixture.read", "records"),
            "reorder": ("fixture.write", "ack"),
            "report": ("fixture.report", "report"),
        }[self.kind]
        if (self.scope, self.result_type) != expected:
            raise ValueError("operation shape incompatible")
        return self


class DeclarativeContract(BaseModel):
    """Constrained local documentation subset, intentionally not full OpenAPI."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    service_id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,47}$")
    version: str = Field(pattern=r"^[1-9][0-9]*\.[0-9]+$")
    schema_revision: Literal[1]
    valid_until_tick: int = Field(ge=1, le=100000)
    record_id_field: str = Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")
    record_state_field: str = Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")
    operations: list[DeclarativeOperation] = Field(min_length=3, max_length=3)

    @model_validator(mode="after")
    def unique_kinds(self) -> "DeclarativeContract":
        """Require exactly one operation of each safe fixture kind."""
        for value, pattern in ((self.service_id, r"[a-z][a-z0-9_]{0,47}"),
                               (self.version, r"[1-9][0-9]*\.[0-9]+"),
                               (self.record_id_field, r"[a-z][a-z0-9_]{0,31}"),
                               (self.record_state_field, r"[a-z][a-z0-9_]{0,31}")):
            if re.fullmatch(pattern, value) is None:
                raise ValueError("invalid contract identifier")
        if {op.kind for op in self.operations} != {"list", "reorder", "report"}:
            raise ValueError("unsupported operation set")
        if len({op.operation_id for op in self.operations}) != 3:
            raise ValueError("duplicate operation")
        if self.record_id_field == self.record_state_field:
            raise ValueError("record fields overlap")
        return self

    def operation(self, kind: str) -> DeclarativeOperation:
        """Find a reviewed fixture operation by fixed kind."""
        return next(op for op in self.operations if op.kind == kind)


def parse_contract(text: str, *, now_tick: int) -> tuple[DeclarativeContract, str]:
    """Parse untrusted local documentation with exact keys and a SHA-256 identity."""
    if len(text.encode("utf-8")) > 4096:
        raise CapabilityRefusal("DOCUMENT_INVALID")
    try:
        value = json.loads(text, object_pairs_hook=_unique_pairs)
        contract = DeclarativeContract.model_validate(value)
    except (ValueError, ValidationError, TypeError):
        raise CapabilityRefusal("DOCUMENT_INVALID") from None
    if now_tick > contract.valid_until_tick:
        raise CapabilityRefusal("DOCUMENT_STALE")
    return contract, sha256(text.encode("utf-8")).hexdigest()


class FixtureAdapter:
    """Generic isolated interpreter over synthetic records, with no I/O."""

    def __init__(self, contract: DeclarativeContract, records: list[dict[str, str]]) -> None:
        self.contract = contract
        self.records = deepcopy(records)
        self.order: list[str] = []
        ids: list[str] = []
        for record in self.records:
            if set(record) != {contract.record_id_field, contract.record_state_field}:
                raise CapabilityRefusal("FIXTURE_SCHEMA_INVALID")
            if any(not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_ -]{1,64}", value) for value in record.values()):
                raise CapabilityRefusal("FIXTURE_SCHEMA_INVALID")
            ids.append(record[contract.record_id_field])
        if not ids or len(ids) != len(set(ids)):
            raise CapabilityRefusal("FIXTURE_SCHEMA_INVALID")

    def invoke(self, operation_id: str, *, ordered_ids: list[str] | None = None) -> object:
        """Execute only the three fixed operations named in the contract."""
        operation = next((op for op in self.contract.operations if op.operation_id == operation_id), None)
        if operation is None:
            raise CapabilityRefusal("OPERATION_UNSUPPORTED")
        if operation.kind == "list":
            return deepcopy(self.records)
        if operation.kind == "reorder":
            ids = [record[self.contract.record_id_field] for record in self.records]
            if ordered_ids is None or sorted(ordered_ids) != sorted(ids):
                raise CapabilityRefusal("FIXTURE_ORDER_INVALID")
            self.order = list(ordered_ids)
            return True
        if not self.order:
            raise CapabilityRefusal("FIXTURE_ORDER_MISSING")
        states = {record[self.contract.record_id_field]: record[self.contract.record_state_field] for record in self.records}
        return "weekly: " + "; ".join(f"{name}={states[name]}" for name in self.order)


def validate_isolated(contract: DeclarativeContract, records: list[dict[str, str]]) -> str:
    """Prove behavior on a copy and return a bounded deterministic evidence digest."""
    adapter = FixtureAdapter(contract, records)
    original = deepcopy(adapter.records)
    listed = adapter.invoke(contract.operation("list").operation_id)
    if not isinstance(listed, list):
        raise CapabilityRefusal("ISOLATED_VALIDATION_FAILED")
    ids = sorted(record[contract.record_id_field] for record in listed)
    adapter.invoke(contract.operation("reorder").operation_id, ordered_ids=ids)
    report = adapter.invoke(contract.operation("report").operation_id)
    if not isinstance(report, str) or not report.startswith("weekly: ") or adapter.records != original:
        raise CapabilityRefusal("ISOLATED_VALIDATION_FAILED")
    return sha256(report.encode("utf-8")).hexdigest()
