"""Opt-in fixture mission lifecycle over Project-local durable storage.

No Brain, scheduler, tool-router or production approval endpoint imports this
module. The only executable adapter is a deterministic in-memory fixture.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from hashlib import sha256
import json
from typing import Protocol
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from agents.generalist_capability import (
    CapabilityRefusal, DeclarativeContract, FixtureAdapter, parse_contract, validate_isolated,
)


class MissionState(str, Enum):
    """Durable mission stages for the fixture-only generalist slice."""

    RECEIVED = "MISSION_RECEIVED"
    GAP = "CAPABILITY_GAP_IDENTIFIED"
    PENDING = "PENDING_HUMAN_APPROVAL"
    READY = "APPROVED_CAPABILITY_SELECTED"
    COMPLETE = "MISSION_COMPLETE"
    BLOCKED = "MISSION_BLOCKED"
    ATTEMPTED = "EXTERNAL_OPERATION_ATTEMPTED"


class CapabilityState(str, Enum):
    """Validation, fixture authorization and revocation are separate facts."""

    DISCOVERED = "DISCOVERED"
    PROPOSED = "PROPOSED"
    VALIDATED = "VALIDATED"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    TEST_ONLY_APPROVED = "TEST_ONLY_APPROVED"
    APPROVED = "APPROVED"  # Reserved: no production grant operation exists.
    STALE = "STALE"
    REVOKED = "REVOKED"


def _now() -> datetime:
    return datetime.now(timezone.utc)


class MissionRecord(BaseModel):
    """Project-bound checkpoint and minimal safe mission evidence."""

    model_config = ConfigDict(extra="forbid")
    mission_id: UUID = Field(default_factory=uuid4)
    project_id: str
    user_id: str = "fixture_user"
    objective: str = Field(min_length=1, max_length=240)
    state: MissionState = MissionState.RECEIVED
    state_history: list[str] = Field(default_factory=lambda: [MissionState.RECEIVED.value])
    required_capabilities: list[str] = Field(default_factory=list)
    capability_gaps: list[str] = Field(default_factory=list)
    completed_steps: list[str] = Field(default_factory=list)
    pending_steps: list[str] = Field(default_factory=list)
    checkpoint: dict[str, str | int] = Field(default_factory=dict)
    failure_reasons: list[str] = Field(default_factory=list)
    resume_eligible: bool = True
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class CapabilityRecord(BaseModel):
    """Exact contract identity and evidence; fixture grant is never a real grant."""

    model_config = ConfigDict(extra="forbid")
    project_id: str
    user_id: str = "fixture_user"
    capability_id: str
    version: str
    documentation_identity: str
    documentation_digest: str
    operations: list[str]
    required_scopes: list[str]
    invocation_contract: dict[str, object] = Field(default_factory=dict)
    validation_state: str = "UNTESTED"
    validation_evidence: str | None = None
    state: CapabilityState = CapabilityState.DISCOVERED
    lifecycle_history: list[CapabilityState] = Field(default_factory=lambda: [CapabilityState.DISCOVERED])
    approval_provenance: str | None = None
    compatible_schema_revision: int = 1
    valid_until_tick: int
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class MissionStore(Protocol):
    """Small storage boundary; PostgreSQL implementation is Project scoped."""

    project: object

    async def create_mission(self, mission_id: UUID, user_id: str, state: str, record: str) -> bool: ...
    async def get_mission(self, mission_id: UUID) -> tuple[str, int] | None: ...
    async def save_mission(self, mission_id: UUID, expected_revision: int, state: str, record: str) -> bool: ...
    async def save_capability(self, user_id: str, capability_id: str, version: str, digest: str, record: str) -> None: ...
    async def get_capability(self, capability_id: str, version: str, digest: str) -> str | None: ...
    async def get_receipt(self, mission_id: UUID, key: str) -> str | None: ...
    async def commit_execution(self, mission_id: UUID, user_id: str, key: str,
                               expected_revision: int, state: str, record: str,
                               result: str) -> tuple[bool, str]: ...


class GeneralistFixtureEngine:
    """Resume a mission through a strict contract and fixture-only approval."""

    def __init__(self, store: MissionStore, project_id: str) -> None:
        if getattr(store.project, "id", None) != project_id or project_id == "default":
            raise CapabilityRefusal("PROJECT_MISMATCH")
        self.store = store
        self.project_id = project_id

    async def receive(self, objective: str, capability_id: str) -> MissionRecord:
        """Persist an unknown mission before capability assessment."""
        mission = MissionRecord(project_id=self.project_id, objective=objective,
                                required_capabilities=[capability_id],
                                capability_gaps=[capability_id],
                                pending_steps=["assess_capability", "execute_fixture"])
        if not await self.store.create_mission(mission.mission_id, mission.user_id,
                                               mission.state.value, mission.model_dump_json()):
            raise CapabilityRefusal("MISSION_EXISTS")
        return mission

    async def _mission(self, mission_id: UUID) -> tuple[MissionRecord, int]:
        raw = await self.store.get_mission(mission_id)
        if raw is None:
            raise CapabilityRefusal("MISSION_NOT_FOUND")
        mission = MissionRecord.model_validate_json(raw[0])
        if mission.project_id != self.project_id:
            raise CapabilityRefusal("PROJECT_MISMATCH")
        return mission, raw[1]

    async def assess(self, mission_id: UUID, document: str, *, now_tick: int,
                     records: list[dict[str, str]]) -> MissionRecord:
        """Discover, validate on a copy, then stop at the approval boundary."""
        mission, revision = await self._mission(mission_id)
        if mission.state is not MissionState.RECEIVED:
            raise CapabilityRefusal("MISSION_STATE_INVALID")
        try:
            contract, digest = parse_contract(document, now_tick=now_tick)
        except CapabilityRefusal as exc:
            await self._block(mission, revision, exc.code)
            raise
        if mission.required_capabilities != [contract.service_id]:
            await self._block(mission, revision, "CAPABILITY_MISMATCH")
            raise CapabilityRefusal("CAPABILITY_MISMATCH")
        existing_raw = await self.store.get_capability(contract.service_id, contract.version, digest)
        existing = CapabilityRecord.model_validate_json(existing_raw) if existing_raw else None
        if existing and existing.state in {CapabilityState.REVOKED, CapabilityState.STALE}:
            await self._block(mission, revision, "CAPABILITY_REVOKED_OR_STALE")
            raise CapabilityRefusal("CAPABILITY_REVOKED_OR_STALE")
        if existing and existing.state is CapabilityState.APPROVED:
            await self._block(mission, revision, "PRODUCTION_APPROVAL_UNAVAILABLE")
            raise CapabilityRefusal("PRODUCTION_APPROVAL_UNAVAILABLE")
        if existing and existing.project_id == self.project_id and existing.state is CapabilityState.TEST_ONLY_APPROVED and existing.validation_state == "PASSED":
            capability = existing
            mission.state = MissionState.READY
            mission.state_history.extend(["DOCUMENTATION_DISCOVERY", MissionState.READY.value])
            mission.capability_gaps = []
            mission.completed_steps = ["assess_capability", "reuse_exact_capability"]
        else:
            # No model-generated claim can satisfy this deterministic fixture test.
            capability = CapabilityRecord(
                project_id=self.project_id, capability_id=contract.service_id,
                version=contract.version, documentation_identity=f"local_fixture:{contract.service_id}",
                documentation_digest=digest, operations=[op.operation_id for op in contract.operations],
                required_scopes=[op.scope for op in contract.operations],
                invocation_contract=contract.model_dump(mode="json"),
                state=CapabilityState.PROPOSED, valid_until_tick=contract.valid_until_tick,
                lifecycle_history=[CapabilityState.DISCOVERED, CapabilityState.PROPOSED],
            )
            await self.store.save_capability(capability.user_id, capability.capability_id,
                                             capability.version, digest, capability.model_dump_json())
            try:
                evidence = validate_isolated(contract, records)
            except CapabilityRefusal as exc:
                capability.validation_state = "FAILED"
                capability.updated_at = _now()
                await self.store.save_capability(capability.user_id, capability.capability_id,
                                                 capability.version, digest, capability.model_dump_json())
                await self._block(mission, revision, exc.code)
                raise
            capability.validation_state = "PASSED"
            capability.validation_evidence = evidence
            capability.state = CapabilityState.PENDING_APPROVAL
            capability.lifecycle_history.extend([CapabilityState.VALIDATED, CapabilityState.PENDING_APPROVAL])
            capability.updated_at = _now()
            await self.store.save_capability(capability.user_id, capability.capability_id,
                                             capability.version, digest, capability.model_dump_json())
            mission.state = MissionState.PENDING
            mission.state_history.extend([MissionState.GAP.value, "DOCUMENTATION_DISCOVERY",
                                          "CAPABILITY_PROPOSAL", "ISOLATED_VALIDATION",
                                          MissionState.PENDING.value])
            mission.completed_steps = ["assess_capability", "discover_documentation",
                                       "propose_capability", "validate_isolated"]
            mission.pending_steps = ["human_approval", "execute_fixture"]
        mission.checkpoint = {"capability_id": contract.service_id, "version": contract.version,
                              "digest": digest, "valid_until_tick": contract.valid_until_tick,
                              "report_tick_count": 0}
        mission.updated_at = _now()
        if not await self.store.save_mission(mission_id, revision, mission.state.value,
                                             mission.model_dump_json()):
            raise CapabilityRefusal("MISSION_CONCURRENT_CHANGE")
        return mission

    async def _block(self, mission: MissionRecord, revision: int, code: str) -> None:
        """Persist a fixed safe failure reason without raw documentation text."""
        mission.state = MissionState.BLOCKED
        mission.state_history.append(MissionState.BLOCKED.value)
        mission.failure_reasons.append(code)
        mission.resume_eligible = False
        mission.updated_at = _now()
        if not await self.store.save_mission(mission.mission_id, revision, mission.state.value,
                                             mission.model_dump_json()):
            raise CapabilityRefusal("MISSION_CONCURRENT_CHANGE")

    async def approve_test_fixture(self, mission_id: UUID, *, explicit_test_authorization: bool) -> None:
        """Persist synthetic test consent only; no production grant is possible."""
        if not explicit_test_authorization:
            raise CapabilityRefusal("HUMAN_APPROVAL_REQUIRED")
        mission, _ = await self._mission(mission_id)
        if mission.state is not MissionState.PENDING:
            raise CapabilityRefusal("MISSION_STATE_INVALID")
        cap = await self._capability(mission)
        if cap.state is not CapabilityState.PENDING_APPROVAL or cap.validation_state != "PASSED":
            raise CapabilityRefusal("VALIDATION_REQUIRED")
        cap.state = CapabilityState.TEST_ONLY_APPROVED
        cap.lifecycle_history.append(CapabilityState.TEST_ONLY_APPROVED)
        cap.approval_provenance = "synthetic_fixture_only"
        cap.updated_at = _now()
        await self.store.save_capability(cap.user_id, cap.capability_id, cap.version,
                                         cap.documentation_digest, cap.model_dump_json())

    async def revoke_test_fixture(self, mission_id: UUID) -> None:
        """Revoke a fixture capability before subsequent invocations."""
        mission, _ = await self._mission(mission_id)
        cap = await self._capability(mission)
        cap.state = CapabilityState.REVOKED
        cap.lifecycle_history.append(CapabilityState.REVOKED)
        cap.updated_at = _now()
        await self.store.save_capability(cap.user_id, cap.capability_id, cap.version,
                                         cap.documentation_digest, cap.model_dump_json())

    async def _capability(self, mission: MissionRecord) -> CapabilityRecord:
        cap_id = str(mission.checkpoint.get("capability_id", ""))
        version = str(mission.checkpoint.get("version", ""))
        digest = str(mission.checkpoint.get("digest", ""))
        raw = await self.store.get_capability(cap_id, version, digest)
        if raw is None:
            raise CapabilityRefusal("CAPABILITY_NOT_FOUND")
        cap = CapabilityRecord.model_validate_json(raw)
        if cap.project_id != self.project_id or cap.documentation_digest != digest:
            raise CapabilityRefusal("PROJECT_MISMATCH")
        return cap

    async def execute(self, mission_id: UUID, document: str,
                      records: list[dict[str, str]], *, scopes: frozenset[str],
                      now_tick: int, tick: int = 1) -> str:
        """Compute a local fixture result and atomically commit it once per tick."""
        mission, revision = await self._mission(mission_id)
        key = f"fixture_report:{tick}"
        if mission.state not in {MissionState.PENDING, MissionState.READY, MissionState.COMPLETE}:
            raise CapabilityRefusal("MISSION_STATE_INVALID")
        contract, digest = parse_contract(document, now_tick=now_tick)
        cap = await self._capability(mission)
        if (cap.state is not CapabilityState.TEST_ONLY_APPROVED or
            cap.approval_provenance != "synthetic_fixture_only" or
            cap.validation_state != "PASSED"):
            raise CapabilityRefusal("HUMAN_APPROVAL_REQUIRED")
        if (contract.service_id, contract.version, digest) != (
            cap.capability_id, cap.version, cap.documentation_digest):
            raise CapabilityRefusal("CAPABILITY_VERSION_CHANGED")
        if not set(cap.required_scopes).issubset(scopes):
            raise CapabilityRefusal("PERMISSION_REQUIRED")
        existing = await self.store.get_receipt(mission_id, key)
        if existing is not None:
            return json.loads(existing)["report"]
        if tick != int(mission.checkpoint.get("report_tick_count", 0)) + 1:
            raise CapabilityRefusal("TICK_SEQUENCE_INVALID")
        adapter = FixtureAdapter(contract, records)
        listed = adapter.invoke(contract.operation("list").operation_id)
        if not isinstance(listed, list):
            raise CapabilityRefusal("FIXTURE_SCHEMA_INVALID")
        ordered = sorted(row[contract.record_id_field] for row in listed)
        adapter.invoke(contract.operation("reorder").operation_id, ordered_ids=ordered)
        report = adapter.invoke(contract.operation("report").operation_id)
        if not isinstance(report, str) or not report.startswith("weekly: "):
            raise CapabilityRefusal("FIXTURE_REPORT_INVALID")
        mission.state = MissionState.COMPLETE
        if not mission.state_history or mission.state_history[-1] != MissionState.COMPLETE.value:
            mission.state_history.append(MissionState.COMPLETE.value)
        mission.resume_eligible = True
        mission.capability_gaps = []
        mission.pending_steps = []
        mission.completed_steps = list(dict.fromkeys([*mission.completed_steps, "execute_fixture"]))
        mission.checkpoint["report_tick_count"] = tick
        mission.checkpoint["last_report_digest"] = sha256(report.encode()).hexdigest()
        mission.updated_at = _now()
        result = json.dumps({"report": report}, sort_keys=True)
        _, committed = await self.store.commit_execution(mission_id, mission.user_id, key,
                                                         revision, mission.state.value,
                                                         mission.model_dump_json(), result)
        return json.loads(committed)["report"]
