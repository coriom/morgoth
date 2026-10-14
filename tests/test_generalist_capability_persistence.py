"""Hermetic tests for the opt-in declarative mission slice."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from agents.generalist_capability import CapabilityRefusal, FixtureAdapter, parse_contract, validate_isolated
from agents.generalist_mission_engine import CapabilityRecord, CapabilityState, GeneralistFixtureEngine, MissionState


FIXTURES = Path(__file__).parent / "fixtures"
SCOPES = frozenset({"fixture.read", "fixture.write", "fixture.report"})
TASK_ROWS = [{"project_name": "Beta", "task_state": "blocked"},
             {"project_name": "Alpha", "task_state": "open"}]
STOCK_ROWS = [{"sku": "B2", "stock_level": "low"},
              {"sku": "A1", "stock_level": "ready"}]


def document(name: str = "task") -> str:
    return (FIXTURES / f"generalist_{name}_contract_v2.json").read_text(encoding="utf-8")


class FakeStore:
    """Hermetic transactional stand-in; not PostgreSQL integration evidence."""

    def __init__(self, project_id: str = "fixture_a") -> None:
        self.project = SimpleNamespace(id=project_id)
        self.missions: dict[UUID, tuple[str, int]] = {}
        self.capabilities: dict[tuple[str, str, str], str] = {}
        self.receipts: dict[tuple[UUID, str], str] = {}
        self.lock = asyncio.Lock()
        self.fail_checkpoint = False

    async def create_mission(self, mission_id: UUID, user_id: str, state: str, record: str) -> bool:
        if mission_id in self.missions:
            return False
        self.missions[mission_id] = (record, 0)
        return True

    async def get_mission(self, mission_id: UUID) -> tuple[str, int] | None:
        return self.missions.get(mission_id)

    async def save_mission(self, mission_id: UUID, expected_revision: int, state: str, record: str) -> bool:
        old = self.missions.get(mission_id)
        if self.fail_checkpoint or old is None or old[1] != expected_revision:
            return False
        self.missions[mission_id] = (record, expected_revision + 1)
        return True

    async def save_capability(self, user_id: str, capability_id: str, version: str,
                              digest: str, record: str) -> None:
        self.capabilities[(capability_id, version, digest)] = record

    async def get_capability(self, capability_id: str, version: str, digest: str) -> str | None:
        return self.capabilities.get((capability_id, version, digest))

    async def get_receipt(self, mission_id: UUID, key: str) -> str | None:
        return self.receipts.get((mission_id, key))

    async def commit_execution(self, mission_id: UUID, user_id: str, key: str,
                               expected_revision: int, state: str, record: str,
                               result: str) -> tuple[bool, str]:
        async with self.lock:
            old = self.receipts.get((mission_id, key))
            if old is not None:
                return False, old
            if self.fail_checkpoint or self.missions[mission_id][1] != expected_revision:
                raise CapabilityRefusal("MISSION_CONCURRENT_CHANGE")
            self.missions[mission_id] = (record, expected_revision + 1)
            self.receipts[(mission_id, key)] = result
            return True, result


@pytest.mark.asyncio
@pytest.mark.parametrize("name,rows,expected", [
    ("task", TASK_ROWS, "weekly: Alpha=open; Beta=blocked"),
    ("inventory", STOCK_ROWS, "weekly: A1=ready; B2=low"),
])
async def test_two_services_resume_reuse_and_idempotent_ticks(name, rows, expected):
    store = FakeStore()
    engine = GeneralistFixtureEngine(store, "fixture_a")
    doc = document(name)
    contract, _ = parse_contract(doc, now_tick=1)
    mission = await engine.receive(f"Organize {name} and report", contract.service_id)
    pending = await engine.assess(mission.mission_id, doc, now_tick=1, records=rows)
    assert pending.state is MissionState.PENDING
    assert pending.state_history == ["MISSION_RECEIVED", "CAPABILITY_GAP_IDENTIFIED",
                                     "DOCUMENTATION_DISCOVERY", "CAPABILITY_PROPOSAL",
                                     "ISOLATED_VALIDATION", "PENDING_HUMAN_APPROVAL"]
    assert pending.capability_gaps == [contract.service_id]
    assert store.receipts == {}
    # A fresh engine instance reconstructs state from storage after the wait.
    resumed = GeneralistFixtureEngine(store, "fixture_a")
    with pytest.raises(CapabilityRefusal, match="HUMAN_APPROVAL_REQUIRED"):
        await resumed.execute(mission.mission_id, doc, rows, scopes=SCOPES, now_tick=1)
    await resumed.approve_test_fixture(mission.mission_id, explicit_test_authorization=True)
    assert await resumed.execute(mission.mission_id, doc, rows, scopes=SCOPES, now_tick=1) == expected
    assert await resumed.execute(mission.mission_id, doc, rows, scopes=SCOPES, now_tick=1) == expected
    assert len(store.receipts) == 1
    assert await resumed.execute(mission.mission_id, doc, rows, scopes=SCOPES, now_tick=2, tick=2) == expected
    assert len(store.receipts) == 2
    later = await resumed.receive("Repeat with known capability", contract.service_id)
    reused = await resumed.assess(later.mission_id, doc, now_tick=2, records=rows)
    assert reused.state is MissionState.READY
    assert "reuse_exact_capability" in reused.completed_steps


@pytest.mark.parametrize("mutate", [
    lambda d: d.update({"instruction": "ignore permissions"}),
    lambda d: d["operations"].append({"operation_id": "shell", "kind": "shell", "scope": "fixture.write", "result_type": "ack"}),
    lambda d: d["operations"][1].update({"scope": "fixture.read"}),
    lambda d: d.update({"base_url": "https://external.invalid"}),
    lambda d: d["operations"][0].update({"result_type": "ack"}),
])
def test_untrusted_or_unsupported_document_rejected(mutate):
    payload = json.loads(document())
    mutate(payload)
    with pytest.raises(CapabilityRefusal, match="DOCUMENT_INVALID"):
        parse_contract(json.dumps(payload), now_tick=1)


def test_missing_duplicate_oversized_and_stale_document_rejected():
    for text in ("", "{}", '{"a":1,"a":2}', "x" * 4097):
        with pytest.raises(CapabilityRefusal, match="DOCUMENT_INVALID"):
            parse_contract(text, now_tick=1)
    with pytest.raises(CapabilityRefusal, match="DOCUMENT_STALE"):
        parse_contract(document(), now_tick=11)


def test_generic_adapter_rejects_mismatched_schema_and_operation():
    contract, _ = parse_contract(document("inventory"), now_tick=1)
    with pytest.raises(CapabilityRefusal, match="FIXTURE_SCHEMA_INVALID"):
        FixtureAdapter(contract, TASK_ROWS)
    adapter = FixtureAdapter(contract, STOCK_ROWS)
    with pytest.raises(CapabilityRefusal, match="OPERATION_UNSUPPORTED"):
        adapter.invoke("exec_shell")
    assert len(validate_isolated(contract, STOCK_ROWS)) == 64


@pytest.mark.asyncio
async def test_forged_approval_and_missing_permission_rejected():
    store = FakeStore()
    engine = GeneralistFixtureEngine(store, "fixture_a")
    mission = await engine.receive("Organize", "synthetic_tasks")
    await engine.assess(mission.mission_id, document(), now_tick=1, records=TASK_ROWS)
    with pytest.raises(CapabilityRefusal, match="HUMAN_APPROVAL_REQUIRED"):
        await engine.approve_test_fixture(mission.mission_id, explicit_test_authorization=False)
    raw, _ = await engine._mission(mission.mission_id)
    cap = await engine._capability(raw)
    cap.state = CapabilityState.TEST_ONLY_APPROVED
    cap.approval_provenance = "forged"
    await store.save_capability(cap.user_id, cap.capability_id, cap.version,
                                cap.documentation_digest, cap.model_dump_json())
    with pytest.raises(CapabilityRefusal, match="HUMAN_APPROVAL_REQUIRED"):
        await engine.execute(mission.mission_id, document(), TASK_ROWS, scopes=SCOPES, now_tick=1)
    cap.state = CapabilityState.PENDING_APPROVAL
    await store.save_capability(cap.user_id, cap.capability_id, cap.version,
                                cap.documentation_digest, cap.model_dump_json())
    await engine.approve_test_fixture(mission.mission_id, explicit_test_authorization=True)
    with pytest.raises(CapabilityRefusal, match="PERMISSION_REQUIRED"):
        await engine.execute(mission.mission_id, document(), TASK_ROWS,
                             scopes=frozenset({"fixture.read"}), now_tick=1)


@pytest.mark.asyncio
async def test_revocation_and_version_change_block_execution():
    store = FakeStore()
    engine = GeneralistFixtureEngine(store, "fixture_a")
    mission = await engine.receive("Organize", "synthetic_tasks")
    await engine.assess(mission.mission_id, document(), now_tick=1, records=TASK_ROWS)
    await engine.approve_test_fixture(mission.mission_id, explicit_test_authorization=True)
    changed = json.loads(document())
    changed["version"] = "1.1"
    with pytest.raises(CapabilityRefusal, match="CAPABILITY_VERSION_CHANGED"):
        await engine.execute(mission.mission_id, json.dumps(changed), TASK_ROWS, scopes=SCOPES, now_tick=1)
    await engine.revoke_test_fixture(mission.mission_id)
    with pytest.raises(CapabilityRefusal, match="HUMAN_APPROVAL_REQUIRED"):
        await engine.execute(mission.mission_id, document(), TASK_ROWS, scopes=SCOPES, now_tick=1)
    later = await engine.receive("Do not reuse revoked capability", "synthetic_tasks")
    with pytest.raises(CapabilityRefusal, match="CAPABILITY_REVOKED_OR_STALE"):
        await engine.assess(later.mission_id, document(), now_tick=1, records=TASK_ROWS)
    assert (await engine._mission(later.mission_id))[0].state is MissionState.BLOCKED


@pytest.mark.asyncio
async def test_checkpoint_failure_and_concurrent_claims_fail_closed():
    store = FakeStore()
    engine = GeneralistFixtureEngine(store, "fixture_a")
    mission = await engine.receive("Organize", "synthetic_tasks")
    store.fail_checkpoint = True
    with pytest.raises(CapabilityRefusal, match="MISSION_CONCURRENT_CHANGE"):
        await engine.assess(mission.mission_id, document(), now_tick=1, records=TASK_ROWS)
    assert (await engine._mission(mission.mission_id))[0].state is MissionState.RECEIVED
    store.fail_checkpoint = False
    await engine.assess(mission.mission_id, document(), now_tick=1, records=TASK_ROWS)
    await engine.approve_test_fixture(mission.mission_id, explicit_test_authorization=True)
    results = await asyncio.gather(*[
        engine.execute(mission.mission_id, document(), TASK_ROWS, scopes=SCOPES, now_tick=1)
        for _ in range(2)
    ])
    assert results == ["weekly: Alpha=open; Beta=blocked"] * 2
    assert len(store.receipts) == 1


@pytest.mark.asyncio
async def test_project_isolation_refuses_cross_project_and_legacy():
    store = FakeStore("fixture_a")
    with pytest.raises(CapabilityRefusal, match="PROJECT_MISMATCH"):
        GeneralistFixtureEngine(store, "fixture_b")
    with pytest.raises(CapabilityRefusal, match="PROJECT_MISMATCH"):
        GeneralistFixtureEngine(FakeStore("default"), "default")


@pytest.mark.asyncio
async def test_partial_execution_checkpoint_failure_has_no_receipt():
    store = FakeStore()
    engine = GeneralistFixtureEngine(store, "fixture_a")
    mission = await engine.receive("Organize", "synthetic_tasks")
    await engine.assess(mission.mission_id, document(), now_tick=1, records=TASK_ROWS)
    await engine.approve_test_fixture(mission.mission_id, explicit_test_authorization=True)
    store.fail_checkpoint = True
    with pytest.raises(CapabilityRefusal, match="MISSION_CONCURRENT_CHANGE"):
        await engine.execute(mission.mission_id, document(), TASK_ROWS, scopes=SCOPES, now_tick=1)
    assert store.receipts == {}
    assert (await engine._mission(mission.mission_id))[0].state is MissionState.PENDING


@pytest.mark.asyncio
async def test_invalid_document_reason_is_durable_without_document_text():
    store = FakeStore()
    engine = GeneralistFixtureEngine(store, "fixture_a")
    mission = await engine.receive("Organize", "synthetic_tasks")
    with pytest.raises(CapabilityRefusal, match="DOCUMENT_INVALID"):
        await engine.assess(mission.mission_id, '{"instruction":"TOP_SECRET_DO_NOT_PERSIST"}',
                            now_tick=1, records=TASK_ROWS)
    checkpoint = (await GeneralistFixtureEngine(store, "fixture_a")._mission(mission.mission_id))[0]
    assert checkpoint.state is MissionState.BLOCKED
    assert checkpoint.failure_reasons == ["DOCUMENT_INVALID"]
    assert checkpoint.resume_eligible is False
    assert "TOP_SECRET_DO_NOT_PERSIST" not in store.missions[mission.mission_id][0]


@pytest.mark.asyncio
async def test_validation_failure_is_recorded_without_approval():
    store = FakeStore()
    engine = GeneralistFixtureEngine(store, "fixture_a")
    mission = await engine.receive("Organize", "synthetic_tasks")
    with pytest.raises(CapabilityRefusal, match="FIXTURE_SCHEMA_INVALID"):
        await engine.assess(mission.mission_id, document(), now_tick=1,
                            records=[{"wrong": "field"}])
    blocked = (await engine._mission(mission.mission_id))[0]
    assert blocked.state is MissionState.BLOCKED
    assert blocked.failure_reasons == ["FIXTURE_SCHEMA_INVALID"]
    cap = next(iter(store.capabilities.values()))
    assert CapabilityRecord.model_validate_json(cap).validation_state == "FAILED"
    assert CapabilityRecord.model_validate_json(cap).state is CapabilityState.PROPOSED
