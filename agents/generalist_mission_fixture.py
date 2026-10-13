"""Opt-in, fixture-only capability acquisition; never wired to Brain or tools.

This module proves state transitions, not a production approval mechanism. The
only adapter is an in-memory synthetic task service. No generated code runs.
"""

from __future__ import annotations

from copy import deepcopy
from enum import Enum
from hashlib import sha256
import json
from pathlib import Path
import sys
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class MissionStage(str, Enum):
    """Bounded fixture mission states."""

    MISSION_RECEIVED = "MISSION_RECEIVED"
    CAPABILITY_ASSESSMENT = "CAPABILITY_ASSESSMENT"
    CAPABILITY_GAP_IDENTIFIED = "CAPABILITY_GAP_IDENTIFIED"
    DOCUMENTATION_DISCOVERY = "DOCUMENTATION_DISCOVERY"
    CAPABILITY_PROPOSAL = "CAPABILITY_PROPOSAL"
    ISOLATED_VALIDATION = "ISOLATED_VALIDATION"
    PENDING_HUMAN_APPROVAL = "PENDING_HUMAN_APPROVAL"
    APPROVED_CAPABILITY_SELECTED = "APPROVED_CAPABILITY_SELECTED"
    MISSION_COMPLETE = "MISSION_COMPLETE"


class CapabilityState(str, Enum):
    """A proposal is not execution proof or approval."""

    PROPOSED = "PROPOSED"
    TESTED = "TESTED"
    APPROVED = "APPROVED"


class MissionFixtureError(ValueError):
    """Safe, fixed-code fixture refusal."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _unique_document_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Reject ambiguous duplicate keys in untrusted local documentation."""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate documentation key")
        result[key] = value
    return result


class ServiceDocumentation(BaseModel):
    """Data-only local service contract; executable instructions are forbidden."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    service: Literal["synthetic_task_service"]
    version: str = Field(pattern=r"^[1-9][0-9]*\.[0-9]+$")
    schema_revision: Literal[1]
    operations: list[Literal["list_projects", "set_order", "status_report"]]
    permissions: list[Literal["projects.read", "projects.write", "reports.read"]]
    valid_until_tick: int = Field(ge=1)


class CapabilityRecord(BaseModel):
    """Exact-version capability record with explicit evidence and authority state."""

    model_config = ConfigDict(frozen=True)
    service: str
    version: str
    document_sha256: str
    state: CapabilityState
    validation_passed: bool = False


class MissionCheckpoint(BaseModel):
    """Serializable fixture checkpoint, with no production credential or grant."""

    stage: MissionStage = MissionStage.MISSION_RECEIVED
    history: list[MissionStage] = Field(default_factory=lambda: [MissionStage.MISSION_RECEIVED])
    service: str = "synthetic_task_service"
    version: str = "1.0"
    document_sha256: str | None = None
    valid_until_tick: int = 0
    capability_state: CapabilityState | None = None
    report: str | None = None
    report_tick_count: int = 0


class SyntheticTaskService:
    """In-memory fixture API: no network, filesystem, package or account access."""

    def __init__(self, projects: list[dict[str, str]], *, broken_report: bool = False) -> None:
        self.projects = deepcopy(projects)
        self.broken_report = broken_report
        self.orders: list[str] = []

    def list_projects(self) -> list[dict[str, str]]:
        """Return synthetic project rows."""
        return deepcopy(self.projects)

    def set_order(self, names: list[str]) -> None:
        """Apply an order only to this in-memory fixture."""
        if sorted(names) != sorted(row["name"] for row in self.projects):
            raise MissionFixtureError("FIXTURE_PROJECT_MISMATCH")
        self.orders = list(names)

    def status_report(self) -> str:
        """Return a deterministic weekly report over the fixture order."""
        if self.broken_report:
            return "invalid fixture report"
        statuses = {row["name"]: row["status"] for row in self.projects}
        return "weekly: " + "; ".join(f"{name}={statuses[name]}" for name in self.orders)


class FixtureCapabilityRegistry:
    """In-memory test registry; cannot authorize a production capability."""

    def __init__(self) -> None:
        self._records: dict[tuple[str, str, str], CapabilityRecord] = {}

    def find_approved(self, service: str, version: str, digest: str) -> CapabilityRecord | None:
        """Select only a tested, approved record for the exact contract."""
        record = self._records.get((service, version, digest))
        return record if record and record.state is CapabilityState.APPROVED and record.validation_passed else None

    def record_tested(self, record: CapabilityRecord) -> None:
        """Store execution proof without granting authority."""
        if record.state is not CapabilityState.TESTED or not record.validation_passed:
            raise MissionFixtureError("VALIDATION_REQUIRED")
        self._records[(record.service, record.version, record.document_sha256)] = record

    def approve_fixture(self, service: str, version: str, digest: str, *, human_fixture_grant: bool) -> None:
        """Simulate a human grant in tests only; never called by production code."""
        key = (service, version, digest)
        record = self._records.get(key)
        if not human_fixture_grant or record is None or record.state is not CapabilityState.TESTED:
            raise MissionFixtureError("HUMAN_APPROVAL_REQUIRED")
        self._records[key] = record.model_copy(update={"state": CapabilityState.APPROVED})


class FixtureMission:
    """Deterministic acquisition and resume flow for one synthetic mission."""

    REQUIRED_OPERATIONS = ("list_projects", "set_order", "status_report")
    REQUIRED_PERMISSIONS = ("projects.read", "projects.write", "reports.read")

    def __init__(self, registry: FixtureCapabilityRegistry, checkpoint: MissionCheckpoint | None = None) -> None:
        self.registry = registry
        self.checkpoint = checkpoint or MissionCheckpoint()
        self.documentation: ServiceDocumentation | None = None

    def _advance(self, stage: MissionStage) -> None:
        self.checkpoint.stage = stage
        self.checkpoint.history.append(stage)

    def assess(self, *, document_text: str, now_tick: int) -> MissionStage:
        """Inspect a bounded local contract and select or propose a capability."""
        if self.checkpoint.stage is not MissionStage.MISSION_RECEIVED:
            raise MissionFixtureError("INVALID_STAGE")
        self._advance(MissionStage.CAPABILITY_ASSESSMENT)
        if len(document_text) > 4096:
            raise MissionFixtureError("DOCUMENT_INVALID")
        try:
            raw = json.loads(document_text, object_pairs_hook=_unique_document_object)
            doc = ServiceDocumentation.model_validate(raw)
        except (ValueError, ValidationError, TypeError):
            raise MissionFixtureError("DOCUMENT_INVALID") from None
        if now_tick > doc.valid_until_tick:
            raise MissionFixtureError("DOCUMENT_STALE")
        if tuple(doc.operations) != self.REQUIRED_OPERATIONS:
            raise MissionFixtureError("SCHEMA_INCOMPATIBLE")
        if tuple(doc.permissions) != self.REQUIRED_PERMISSIONS:
            raise MissionFixtureError("PERMISSION_SCOPE_INVALID")
        digest = sha256(document_text.encode()).hexdigest()
        self.documentation = doc
        self.checkpoint.version = doc.version
        self.checkpoint.document_sha256 = digest
        self.checkpoint.valid_until_tick = doc.valid_until_tick
        approved = self.registry.find_approved(doc.service, doc.version, digest)
        if approved:
            self._advance(MissionStage.DOCUMENTATION_DISCOVERY)
            self.checkpoint.capability_state = CapabilityState.APPROVED
            self._advance(MissionStage.APPROVED_CAPABILITY_SELECTED)
        else:
            self._advance(MissionStage.CAPABILITY_GAP_IDENTIFIED)
            self._advance(MissionStage.DOCUMENTATION_DISCOVERY)
            self.checkpoint.capability_state = CapabilityState.PROPOSED
            self._advance(MissionStage.CAPABILITY_PROPOSAL)
        return self.checkpoint.stage

    def validate_isolated(self, adapter: SyntheticTaskService) -> None:
        """Run a fixed assertion against a copy, never generated integration code."""
        if self.checkpoint.stage is not MissionStage.CAPABILITY_PROPOSAL or self.documentation is None:
            raise MissionFixtureError("INVALID_STAGE")
        self._advance(MissionStage.ISOLATED_VALIDATION)
        sandbox = deepcopy(adapter)
        original_orders = list(adapter.orders)
        rows = sandbox.list_projects()
        names = sorted(row["name"] for row in rows)
        sandbox.set_order(names)
        expected = "weekly: " + "; ".join(
            f"{name}={next(row['status'] for row in rows if row['name'] == name)}" for name in names
        )
        if sandbox.status_report() != expected or adapter.orders != original_orders:
            raise MissionFixtureError("ISOLATED_VALIDATION_FAILED")
        record = CapabilityRecord(
            service=self.documentation.service,
            version=self.documentation.version,
            document_sha256=self.checkpoint.document_sha256 or "",
            state=CapabilityState.TESTED,
            validation_passed=True,
        )
        self.registry.record_tested(record)
        self.checkpoint.capability_state = CapabilityState.TESTED
        self._advance(MissionStage.PENDING_HUMAN_APPROVAL)

    def _require_execution_grant(self, permissions: frozenset[str], now_tick: int) -> None:
        """Recheck exact fixture approval, document freshness and permissions."""
        if now_tick > self.checkpoint.valid_until_tick:
            raise MissionFixtureError("DOCUMENT_STALE")
        digest = self.checkpoint.document_sha256
        record = self.registry.find_approved(self.checkpoint.service, self.checkpoint.version, digest or "")
        if record is None:
            raise MissionFixtureError("HUMAN_APPROVAL_REQUIRED")
        if not set(self.REQUIRED_PERMISSIONS).issubset(permissions):
            raise MissionFixtureError("PERMISSION_REQUIRED")

    def execute_fixture(self, adapter: SyntheticTaskService, *, permissions: frozenset[str], now_tick: int = 1) -> str:
        """Resume only with an exact approved fixture record and explicit scope."""
        if self.checkpoint.stage not in {MissionStage.PENDING_HUMAN_APPROVAL, MissionStage.APPROVED_CAPABILITY_SELECTED}:
            raise MissionFixtureError("INVALID_STAGE")
        self._require_execution_grant(permissions, now_tick)
        names = sorted(row["name"] for row in adapter.list_projects())
        adapter.set_order(names)
        report = adapter.status_report()
        if not report.startswith("weekly: "):
            raise MissionFixtureError("FIXTURE_REPORT_INVALID")
        self.checkpoint.capability_state = CapabilityState.APPROVED
        self.checkpoint.report = report
        self.checkpoint.report_tick_count = 1
        self._advance(MissionStage.MISSION_COMPLETE)
        return report

    def report_tick(self, adapter: SyntheticTaskService, *, permissions: frozenset[str], now_tick: int) -> str:
        """Simulate a later weekly tick; a real scheduler is not connected."""
        if self.checkpoint.stage is not MissionStage.MISSION_COMPLETE:
            raise MissionFixtureError("INVALID_STAGE")
        self._require_execution_grant(permissions, now_tick)
        report = adapter.status_report()
        if not report.startswith("weekly: "):
            raise MissionFixtureError("FIXTURE_REPORT_INVALID")
        self.checkpoint.report = report
        self.checkpoint.report_tick_count += 1
        return report


def run_fixture_demo() -> dict[str, object]:
    """Exercise only the repository's synthetic contract and in-memory adapter."""
    fixture = Path(__file__).resolve().parents[1] / "data/fixtures/generalist_task_service.json"
    document = fixture.read_text(encoding="utf-8")
    registry = FixtureCapabilityRegistry()
    mission = FixtureMission(registry)
    service = SyntheticTaskService(
        [{"name": "Beta", "status": "blocked"}, {"name": "Alpha", "status": "open"}]
    )
    mission.assess(document_text=document, now_tick=1)
    mission.validate_isolated(service)
    pending = mission.checkpoint.model_dump_json()
    registry.approve_fixture(
        "synthetic_task_service", "1.0", mission.checkpoint.document_sha256 or "",
        human_fixture_grant=True,
    )
    resumed = FixtureMission(registry, MissionCheckpoint.model_validate_json(pending))
    report = resumed.execute_fixture(
        service, permissions=frozenset(FixtureMission.REQUIRED_PERMISSIONS)
    )
    repeated_report = resumed.report_tick(
        service, permissions=frozenset(FixtureMission.REQUIRED_PERMISSIONS), now_tick=2
    )
    return {
        "fixture_only": True,
        "production_approval": False,
        "stages": [stage.value for stage in resumed.checkpoint.history],
        "report": report,
        "repeated_report": repeated_report,
        "report_tick_count": resumed.checkpoint.report_tick_count,
        "service_order": service.orders,
    }


if __name__ == "__main__":
    sys.stdout.write(json.dumps(run_fixture_demo(), sort_keys=True) + "\n")
