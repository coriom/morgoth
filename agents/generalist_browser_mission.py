"""Project-scoped checkpoint adapter for the opt-in synthetic browser lab."""

from __future__ import annotations

from hashlib import sha256
import json
from typing import Awaitable, Callable
from uuid import UUID

from agents.generalist_browser_lab import BrowserProcedure, BrowserRefusal
from agents.generalist_mission_engine import (
    CapabilityRecord, CapabilityState, MissionRecord, MissionState, _now,
)
from agents.generalist_mission_store import ProjectMissionStore


BrowserRun = Callable[[], Awaitable[dict]]


class BrowserMissionCoordinator:
    """Persist a claim before fixture I/O and a receipt only after UI proof."""

    def __init__(self, store: ProjectMissionStore, project_id: str) -> None:
        if store.project.id != project_id or project_id == "default":
            raise BrowserRefusal("PROJECT_MISMATCH")
        self.store = store
        self.project_id = project_id

    async def receive(self, objective: str, capability_id: str) -> MissionRecord:
        """Create a mission with a browser capability gap in one Project."""
        mission = MissionRecord(project_id=self.project_id, objective=objective,
                                required_capabilities=[capability_id],
                                capability_gaps=[capability_id],
                                pending_steps=["observe_fixture", "validate_procedure"])
        if not await self.store.create_mission(mission.mission_id, mission.user_id,
                                               mission.state.value, mission.model_dump_json()):
            raise BrowserRefusal("MISSION_EXISTS")
        return mission

    async def validate_fixture(self, mission_id: UUID, *, capability_id: str,
                               run: BrowserRun) -> tuple[MissionRecord, BrowserProcedure]:
        """Claim once, run the isolated fixture, then persist bounded proof.

        If the worker or persistence fails after the claim, the mission remains
        ATTEMPTED. Recovery never replays an uncertain action automatically.
        """
        raw = await self.store.get_mission(mission_id)
        if raw is None:
            raise BrowserRefusal("MISSION_NOT_FOUND")
        mission = MissionRecord.model_validate_json(raw[0])
        if mission.project_id != self.project_id or mission.required_capabilities != [capability_id]:
            raise BrowserRefusal("MISSION_CAPABILITY_MISMATCH")
        if mission.state is not MissionState.RECEIVED:
            raise BrowserRefusal("MISSION_ALREADY_CLAIMED")
        mission.state = MissionState.ATTEMPTED
        mission.state_history.append(MissionState.ATTEMPTED.value)
        mission.resume_eligible = False
        mission.checkpoint = {"browser_phase": "isolated_attempt_claimed"}
        mission.updated_at = _now()
        if not await self.store.save_mission(mission_id, raw[1], mission.state.value,
                                             mission.model_dump_json()):
            raise BrowserRefusal("MISSION_CONCURRENT_CHANGE")
        result = await run()
        if result.get("VERDICT") != "ISOLATED_PROCEDURE_VALIDATED" or result.get("BROWSER_ISOLATED") is not True:
            raise BrowserRefusal("ISOLATED_VALIDATION_FAILED")
        procedure = BrowserProcedure.model_validate(result["PROCEDURE"])
        if result.get("ACTION_COUNT") != len(procedure.steps):
            raise BrowserRefusal("ISOLATED_EVIDENCE_INVALID")
        capability = CapabilityRecord(
            project_id=self.project_id, user_id=mission.user_id,
            capability_id=capability_id, version=str(procedure.version),
            documentation_identity="synthetic_browser_fixture",
            documentation_digest=procedure.source_digest,
            operations=["browser_synthetic_procedure"],
            required_scopes=["browser.synthetic.local"],
            invocation_contract=procedure.model_dump(mode="json"),
            validation_state="PASSED", validation_evidence="ISOLATED_UI_STATE_OBSERVED",
            state=CapabilityState.PENDING_APPROVAL, valid_until_tick=1,
            lifecycle_history=[CapabilityState.DISCOVERED, CapabilityState.PROPOSED,
                               CapabilityState.VALIDATED, CapabilityState.PENDING_APPROVAL],
        )
        await self.store.save_capability(mission.user_id, capability_id, str(procedure.version),
                                         procedure.source_digest, capability.model_dump_json())
        mission.state = MissionState.PENDING
        mission.state_history.append(MissionState.PENDING.value)
        mission.resume_eligible = True
        mission.capability_gaps = []
        mission.completed_steps = ["observe_fixture", "validate_procedure"]
        mission.pending_steps = ["authenticated_browser_approval"]
        mission.checkpoint = {"browser_phase": "procedure_validated",
                              "source_digest": procedure.source_digest,
                              "action_count": len(procedure.steps)}
        mission.updated_at = _now()
        receipt = json.dumps({"ui_state_observed": True, "action_count": len(procedure.steps),
                              "procedure_digest": sha256(procedure.model_dump_json().encode()).hexdigest()},
                             sort_keys=True)
        inserted, _ = await self.store.commit_execution(
            mission_id, mission.user_id, "browser_fixture_validation", raw[1] + 1,
            mission.state.value, mission.model_dump_json(), receipt)
        if not inserted:
            raise BrowserRefusal("MISSION_DUPLICATE_RECEIPT")
        return mission, procedure

    async def recover(self, mission_id: UUID) -> tuple[MissionRecord, BrowserProcedure | None]:
        """Recover only a committed pending proposal; never replay ATTEMPTED."""
        raw = await self.store.get_mission(mission_id)
        if raw is None:
            raise BrowserRefusal("MISSION_NOT_FOUND")
        mission = MissionRecord.model_validate_json(raw[0])
        if mission.project_id != self.project_id:
            raise BrowserRefusal("PROJECT_MISMATCH")
        if mission.state is not MissionState.PENDING:
            return mission, None
        digest = mission.checkpoint.get("source_digest")
        if not isinstance(digest, str) or len(mission.required_capabilities) != 1:
            raise BrowserRefusal("PROCEDURE_CHECKPOINT_INVALID")
        stored = await self.store.get_capability(mission.required_capabilities[0], "1", digest)
        if stored is None:
            raise BrowserRefusal("PROCEDURE_CAPABILITY_MISSING")
        capability = CapabilityRecord.model_validate_json(stored)
        if (capability.project_id != self.project_id or
            capability.state not in {CapabilityState.PENDING_APPROVAL,
                                     CapabilityState.TEST_ONLY_APPROVED} or
            capability.validation_state != "PASSED"):
            raise BrowserRefusal("PROCEDURE_NOT_VALIDATED")
        procedure = BrowserProcedure.model_validate(capability.invocation_contract)
        if procedure.source_digest != digest:
            raise BrowserRefusal("PROCEDURE_VERSION_DRIFT")
        receipt = await self.store.get_receipt(mission_id, "browser_fixture_validation")
        if receipt is None:
            raise BrowserRefusal("PROCEDURE_RECEIPT_MISSING")
        try:
            evidence = json.loads(receipt)
        except ValueError:
            raise BrowserRefusal("PROCEDURE_RECEIPT_INVALID") from None
        if (evidence.get("ui_state_observed") is not True or
            evidence.get("action_count") != len(procedure.steps) or
            evidence.get("procedure_digest") !=
                sha256(procedure.model_dump_json().encode()).hexdigest()):
            raise BrowserRefusal("PROCEDURE_RECEIPT_INVALID")
        return mission, procedure
