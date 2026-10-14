"""Opt-in Project mission orchestration over trusted, narrow capability drivers.

This module is not imported by Brain, Scheduler or ToolRouter. Fixture grants
remain caller-supplied; the public GET instead requires a disposable,
Project-bound SQL approval consumed by the broker before I/O. No production
approval or model-controlled driver registry exists here.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from typing import TYPE_CHECKING, Any, Literal, Protocol
from uuid import UUID

import httpx

from agents.generalist_capability import (
    CapabilityRefusal, FixtureAdapter, parse_contract, validate_isolated,
)
from agents.generalist_http_read import (
    parse_public_issues_contract, read_public_issues, validate_isolated_public_issues,
)
from agents.generalist_generic_read import (
    extract_openapi_read, read_once, validated_request, validate_response,
)
from agents.generalist_approval_broker import ReviewedReadRequest
from agents.generalist_mission_engine import (
    CapabilityRecord, CapabilityState, MissionRecord, MissionState, MissionStore, _now,
)

if TYPE_CHECKING:
    from agents.generalist_approval_broker import ProjectApprovalBroker


@dataclass(frozen=True)
class CapabilityDescriptor:
    """Validated identity and authority requirements of a data-only document."""

    capability_id: str
    version: str
    digest: str
    documentation_identity: str
    operation: str
    scopes: frozenset[str]
    valid_until_tick: int
    invocation_contract: dict[str, object]


@dataclass(frozen=True)
class InvocationGrant:
    """Caller-supplied fixture permission; rejected for the real HTTP driver."""

    project_id: str
    capability_id: str
    version: str
    digest: str
    scopes: frozenset[str]
    expires_tick: int
    provenance: Literal["synthetic_fixture_only", "operator_task_public_read"]


class CapabilityDriver(Protocol):
    """Trusted adapter interface; untrusted documentation cannot instantiate it."""

    provenance: str

    def inspect(self, document: str, *, now_tick: int) -> CapabilityDescriptor: ...
    def validate(self, document: str, sample: object, *, now_tick: int) -> str: ...
    async def execute(self, document: str, sample: object | None, *, now_tick: int) -> dict[str, Any]: ...


class FixtureDriver:
    """Bridge the existing task and inventory contracts into the same engine."""

    provenance = "synthetic_fixture_only"

    def inspect(self, document: str, *, now_tick: int) -> CapabilityDescriptor:
        """Inspect either fixture service without a service-specific branch."""
        contract, digest = parse_contract(document, now_tick=now_tick)
        return CapabilityDescriptor(contract.service_id, contract.version, digest,
                                    f"local_fixture:{contract.service_id}",
                                    contract.operation("report").operation_id,
                                    frozenset(op.scope for op in contract.operations),
                                    contract.valid_until_tick, contract.model_dump(mode="json"))

    def validate(self, document: str, sample: object, *, now_tick: int) -> str:
        """Run the reviewed fixture interpreter on a copy."""
        contract, _ = parse_contract(document, now_tick=now_tick)
        if not isinstance(sample, list):
            raise CapabilityRefusal("FIXTURE_SCHEMA_INVALID")
        return validate_isolated(contract, sample)

    async def execute(self, document: str, sample: object | None, *, now_tick: int) -> dict[str, Any]:
        """Produce a deterministic report with no external side effect."""
        contract, _ = parse_contract(document, now_tick=now_tick)
        if not isinstance(sample, list):
            raise CapabilityRefusal("FIXTURE_SCHEMA_INVALID")
        adapter = FixtureAdapter(contract, sample)
        rows = adapter.invoke(contract.operation("list").operation_id)
        if not isinstance(rows, list):
            raise CapabilityRefusal("FIXTURE_SCHEMA_INVALID")
        ordered = sorted(row[contract.record_id_field] for row in rows)
        adapter.invoke(contract.operation("reorder").operation_id, ordered_ids=ordered)
        report = adapter.invoke(contract.operation("report").operation_id)
        if not isinstance(report, str):
            raise CapabilityRefusal("FIXTURE_REPORT_INVALID")
        return {"source_identity": f"local_fixture:{contract.service_id}",
                "operation": contract.operation("report").operation_id,
                "retrieved_at": _now().isoformat(), "report": report,
                "response_sha256": sha256(report.encode()).hexdigest()}


class PublicIssuesDriver:
    """Only the fixed public GitHub Issues read contract is executable."""

    provenance = "operator_task_public_read"

    def __init__(self, *, transport: Any = None) -> None:
        self._transport = transport

    def inspect(self, document: str, *, now_tick: int) -> CapabilityDescriptor:
        """Bind the exact GitHub contract and digest."""
        contract, digest = parse_public_issues_contract(document, now_tick=now_tick)
        return CapabilityDescriptor(contract.service_id, contract.version, digest,
                                    contract.documentation_identity, contract.operation_id,
                                    frozenset({contract.scope}), contract.valid_until_tick,
                                    contract.model_dump(mode="json"))

    def validate(self, document: str, sample: object, *, now_tick: int) -> str:
        """Validate a mocked response, never a model claim."""
        contract, _ = parse_public_issues_contract(document, now_tick=now_tick)
        return validate_isolated_public_issues(contract, sample)

    async def execute(self, document: str, sample: object | None, *, now_tick: int) -> dict[str, Any]:
        """Perform one real or injected-transport GET with no credential header."""
        contract, _ = parse_public_issues_contract(document, now_tick=now_tick)
        return await read_public_issues(contract, transport=self._transport)


class DeclarativeHttpReadDriver:
    """One test-only GET interpreter for independently reviewed destinations."""

    provenance = "operator_task_public_read"

    def __init__(self, reviewed_request: ReviewedReadRequest, inputs: dict[str, object],
                 *, transport: Any = None) -> None:
        if type(reviewed_request) is not ReviewedReadRequest or type(inputs) is not dict:
            raise CapabilityRefusal("UNREVIEWED_DESTINATION")
        # This milestone only permits synthetic transports for previously
        # unknown services. No production destination registry exists yet.
        if type(transport) is not httpx.MockTransport:
            raise CapabilityRefusal("GENERIC_LIVE_READ_NOT_AUTHORIZED")
        self.reviewed_request = reviewed_request
        self.inputs = dict(inputs)
        self._transport = transport

    def inspect(self, document: str, *, now_tick: int) -> CapabilityDescriptor:
        """Parse data and demand an independent exact request binding."""
        contract = extract_openapi_read(document)
        if now_tick > contract.valid_until_tick:
            raise CapabilityRefusal("DOCUMENT_STALE")
        path, query = validated_request(contract, self.inputs)
        review = self.reviewed_request
        if ((contract.host, path, contract.operation_id, contract.scope, query) !=
                (review.host, review.endpoint, review.operation, review.scope, review.query)):
            raise CapabilityRefusal("UNREVIEWED_DESTINATION")
        invocation = {"method": "GET", "host": contract.host,
                      "path_template": contract.path_template, "endpoint": path,
                      "operation_id": contract.operation_id, "scope": contract.scope,
                      "query": query, "service_id": contract.service_id,
                      "capability_id": contract.capability_id,
                      "documentation_identity": contract.documentation_identity}
        return CapabilityDescriptor(contract.capability_id, contract.version,
                                    contract.documentation_digest, contract.documentation_identity,
                                    contract.operation_id, frozenset({contract.scope}),
                                    contract.valid_until_tick, invocation)

    def validate(self, document: str, sample: object, *, now_tick: int) -> str:
        """Validate an isolated mock response using the same schema as execution."""
        self.inspect(document, now_tick=now_tick)
        contract = extract_openapi_read(document)
        result = validate_response(contract, sample)
        return sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()

    async def execute(self, document: str, sample: object | None, *, now_tick: int) -> dict[str, Any]:
        """Perform the one reviewed mock GET after the SQL broker has claimed."""
        self.inspect(document, now_tick=now_tick)
        contract = extract_openapi_read(document)
        return await read_once(contract, self.reviewed_request, self.inputs,
                               transport=self._transport)


class GeneralistMissionOrchestrator:
    """Durable gap/proposal/validation/claim/receipt transitions for any driver."""

    def __init__(self, store: MissionStore, project_id: str, driver: CapabilityDriver,
                 *, approval_broker: ProjectApprovalBroker | None = None) -> None:
        if getattr(store.project, "id", None) != project_id or project_id == "default":
            raise CapabilityRefusal("PROJECT_MISMATCH")
        if type(driver) not in {FixtureDriver, PublicIssuesDriver, DeclarativeHttpReadDriver}:
            raise CapabilityRefusal("UNREVIEWED_DRIVER")
        if type(driver) is DeclarativeHttpReadDriver and (
            approval_broker is None or approval_broker.reviewed_request != driver.reviewed_request
        ):
            raise CapabilityRefusal("UNREVIEWED_DESTINATION")
        self.store, self.project_id, self.driver = store, project_id, driver
        self.approval_broker = approval_broker

    async def receive(self, objective: str, capability_id: str) -> MissionRecord:
        """Commit the mission before looking up a capability."""
        mission = MissionRecord(project_id=self.project_id, objective=objective,
                                required_capabilities=[capability_id],
                                capability_gaps=[capability_id],
                                pending_steps=["assess_capability", "execute_capability"])
        if not await self.store.create_mission(mission.mission_id, mission.user_id,
                                               mission.state.value, mission.model_dump_json()):
            raise CapabilityRefusal("MISSION_EXISTS")
        return mission

    async def _load(self, mission_id: UUID) -> tuple[MissionRecord, int]:
        """Load only this Project's mission checkpoint."""
        raw = await self.store.get_mission(mission_id)
        if raw is None:
            raise CapabilityRefusal("MISSION_NOT_FOUND")
        mission = MissionRecord.model_validate_json(raw[0])
        if mission.project_id != self.project_id:
            raise CapabilityRefusal("PROJECT_MISMATCH")
        return mission, raw[1]

    async def assess(self, mission_id: UUID, document: str, sample: object,
                     *, now_tick: int) -> MissionRecord:
        """Acquire an exact contract, prove isolated behavior, await permission."""
        mission, revision = await self._load(mission_id)
        if mission.state is not MissionState.RECEIVED:
            raise CapabilityRefusal("MISSION_STATE_INVALID")
        desc = self.driver.inspect(document, now_tick=now_tick)
        if mission.required_capabilities != [desc.capability_id]:
            raise CapabilityRefusal("CAPABILITY_MISMATCH")
        raw = await self.store.get_capability(desc.capability_id, desc.version, desc.digest)
        existing = CapabilityRecord.model_validate_json(raw) if raw else None
        if existing and existing.state in {CapabilityState.REVOKED, CapabilityState.STALE}:
            raise CapabilityRefusal("CAPABILITY_REVOKED_OR_STALE")
        if existing and existing.state is CapabilityState.APPROVED:
            raise CapabilityRefusal("PRODUCTION_APPROVAL_UNAVAILABLE")
        if existing and existing.project_id != self.project_id:
            raise CapabilityRefusal("PROJECT_MISMATCH")
        if existing and existing.validation_state == "PASSED":
            cap = existing
            mission.state = MissionState.READY
            mission.state_history.append(MissionState.READY.value)
            mission.completed_steps.append("reuse_exact_capability")
        else:
            cap = CapabilityRecord(project_id=self.project_id, capability_id=desc.capability_id,
                                   version=desc.version, documentation_identity=desc.documentation_identity,
                                   documentation_digest=desc.digest, operations=[desc.operation],
                                   required_scopes=sorted(desc.scopes),
                                   invocation_contract=desc.invocation_contract,
                                   state=CapabilityState.PROPOSED,
                                   lifecycle_history=[CapabilityState.DISCOVERED, CapabilityState.PROPOSED],
                                   valid_until_tick=desc.valid_until_tick)
            await self.store.save_capability(cap.user_id, cap.capability_id, cap.version,
                                             cap.documentation_digest, cap.model_dump_json())
            try:
                evidence = self.driver.validate(document, sample, now_tick=now_tick)
            except CapabilityRefusal:
                cap.validation_state = "FAILED"
                await self.store.save_capability(cap.user_id, cap.capability_id, cap.version,
                                                 cap.documentation_digest, cap.model_dump_json())
                raise
            cap.validation_state = "PASSED"
            cap.validation_evidence = evidence
            cap.state = CapabilityState.PENDING_APPROVAL
            cap.lifecycle_history.extend([CapabilityState.VALIDATED, CapabilityState.PENDING_APPROVAL])
            await self.store.save_capability(cap.user_id, cap.capability_id, cap.version,
                                             cap.documentation_digest, cap.model_dump_json())
            mission.state = MissionState.PENDING
            mission.state_history.extend([MissionState.GAP.value, "DOCUMENTATION_DISCOVERY",
                                          "CAPABILITY_PROPOSAL", "ISOLATED_VALIDATION",
                                          MissionState.PENDING.value])
            mission.completed_steps.extend(["assess_capability", "validate_isolated"])
        mission.checkpoint = {"capability_id": desc.capability_id, "version": desc.version,
                              "digest": desc.digest, "valid_until_tick": desc.valid_until_tick}
        mission.updated_at = _now()
        if not await self.store.save_mission(mission_id, revision, mission.state.value,
                                             mission.model_dump_json()):
            raise CapabilityRefusal("MISSION_CONCURRENT_CHANGE")
        return mission

    async def execute(self, mission_id: UUID, document: str, *, grant: InvocationGrant | None,
                      now_tick: int, sample: object | None = None,
                      grant_id: UUID | None = None) -> dict[str, Any]:
        """Claim before I/O; uncertain external outcomes never auto-retry."""
        mission, revision = await self._load(mission_id)
        if mission.state not in {MissionState.PENDING, MissionState.READY, MissionState.COMPLETE}:
            raise CapabilityRefusal("MISSION_STATE_INVALID")
        desc = self.driver.inspect(document, now_tick=now_tick)
        if mission.checkpoint.get("digest") != desc.digest or mission.checkpoint.get("version") != desc.version:
            raise CapabilityRefusal("CAPABILITY_VERSION_CHANGED")
        raw = await self.store.get_capability(desc.capability_id, desc.version, desc.digest)
        if raw is None:
            raise CapabilityRefusal("CAPABILITY_NOT_FOUND")
        cap = CapabilityRecord.model_validate_json(raw)
        if cap.project_id != self.project_id or cap.state in {CapabilityState.REVOKED, CapabilityState.STALE}:
            raise CapabilityRefusal("CAPABILITY_REVOKED_OR_STALE")
        if cap.state not in {CapabilityState.PENDING_APPROVAL, CapabilityState.TEST_ONLY_APPROVED}:
            raise CapabilityRefusal("PRODUCTION_APPROVAL_UNAVAILABLE")
        if cap.state is CapabilityState.TEST_ONLY_APPROVED and (
            cap.approval_provenance != "synthetic_fixture_only" or
            self.driver.provenance != "synthetic_fixture_only"
        ):
            raise CapabilityRefusal("CAPABILITY_RECORD_INVALID")
        if (cap.capability_id != desc.capability_id or cap.version != desc.version or
            cap.documentation_digest != desc.digest or
            cap.documentation_identity != desc.documentation_identity or
            cap.operations != [desc.operation] or
            set(cap.required_scopes) != desc.scopes or
            cap.invocation_contract != desc.invocation_contract):
            raise CapabilityRefusal("CAPABILITY_RECORD_INVALID")
        if cap.validation_state != "PASSED" or cap.validation_evidence is None:
            raise CapabilityRefusal("VALIDATION_REQUIRED")
        real_read = type(self.driver) in {PublicIssuesDriver, DeclarativeHttpReadDriver}
        if real_read:
            if grant is not None or grant_id is None or self.approval_broker is None:
                raise CapabilityRefusal("PERMISSION_REQUIRED")
        else:
            if grant_id is not None or grant is None or (
                grant.project_id, grant.capability_id, grant.version, grant.digest,
                grant.provenance) != (self.project_id, desc.capability_id,
                                     desc.version, desc.digest, self.driver.provenance):
                raise CapabilityRefusal("PERMISSION_REQUIRED")
            if now_tick > grant.expires_tick or not desc.scopes.issubset(grant.scopes):
                raise CapabilityRefusal("PERMISSION_REQUIRED")
        key = f"{desc.operation}:{desc.digest[:16]}"
        old = await self.store.get_receipt(mission_id, key)
        if old is not None:
            if real_read and mission.checkpoint.get("grant_id") != str(grant_id):
                raise CapabilityRefusal("PERMISSION_REQUIRED")
            return json.loads(old)
        if mission.state is MissionState.COMPLETE:
            raise CapabilityRefusal("RECEIPT_MISSING")
        mission.state = MissionState.ATTEMPTED
        mission.state_history.append(MissionState.ATTEMPTED.value)
        mission.checkpoint["attempt_key"] = key
        if real_read:
            mission.checkpoint["grant_id"] = str(grant_id)
        mission.resume_eligible = False
        mission.updated_at = _now()
        if real_read:
            assert self.approval_broker is not None and grant_id is not None
            await self.approval_broker.claim(grant_id, mission, revision, desc,
                                             mission.model_dump_json())
        elif not await self.store.save_mission(mission_id, revision, mission.state.value,
                                               mission.model_dump_json()):
            raise CapabilityRefusal("MISSION_CONCURRENT_CHANGE")
        # A crash, transport error or commit failure leaves ATTEMPTED durably.
        # Even a GET is not automatically replayed; mutating adapters need a
        # separately reviewed remote idempotency/reconciliation contract.
        observed = await self.driver.execute(document, sample, now_tick=now_tick)
        mission.state = MissionState.COMPLETE
        mission.state_history.append(MissionState.COMPLETE.value)
        mission.resume_eligible = True
        mission.capability_gaps = []
        mission.pending_steps = []
        mission.completed_steps.append("execute_capability")
        mission.checkpoint["result_digest"] = sha256(json.dumps(observed, sort_keys=True).encode()).hexdigest()
        mission.updated_at = _now()
        result = {"mission_id": str(mission_id), "capability_id": desc.capability_id,
                  "capability_version": desc.version, "documentation_digest": desc.digest,
                  "status": MissionState.COMPLETE.value, "execution_receipt": key,
                  "evidence": observed}
        _, saved = await self.store.commit_execution(mission_id, mission.user_id, key,
                                                     revision + 1, mission.state.value,
                                                     mission.model_dump_json(),
                                                     json.dumps(result, sort_keys=True))
        return json.loads(saved)
