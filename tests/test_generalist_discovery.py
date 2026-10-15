"""Offline, no-model discovery and persistence boundaries."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agents.generalist_capability import CapabilityRefusal
from agents.generalist_discovery import (
    DiscoveryIntent, DiscoveryMissionPlanner, OfflineDiscovery, OllamaIntentSource,
)
from agents.generalist_mission_engine import MissionState
from agents.generalist_mission_planner import MissionRequirements, load_fixture_catalog


CATALOG = Path(__file__).parent / "fixtures" / "generalist_discovery_catalog_v1.json"
BOOK_MISSION = "Find a public novel record and report its title and available details"


class MemoryStore:
    """CAS-compatible test double; SQL behavior has separate integration tests."""

    def __init__(self, project_id="fixture_project"):
        self.project = SimpleNamespace(id=project_id)
        self.missions = {}
        self.capabilities = {}

    async def create_mission(self, mission_id, user_id, state, record):
        if mission_id in self.missions:
            return False
        self.missions[mission_id] = (record, 0)
        return True

    async def get_mission(self, mission_id):
        return self.missions.get(mission_id)

    async def save_mission(self, mission_id, expected_revision, state, record):
        old = self.missions.get(mission_id)
        if old is None or old[1] != expected_revision:
            return False
        self.missions[mission_id] = (record, expected_revision + 1)
        return True

    async def list_capability_identities(self):
        return list(self.capabilities)

    async def get_capability(self, capability_id, version, digest):
        return self.capabilities.get((capability_id, version, digest))

    async def save_capability(self, user_id, capability_id, version, digest, record):
        self.capabilities[(capability_id, version, digest)] = record


def intent(*, shape="detail", fields=("title",), terms=("book", "bibliographic"),
           abstain=False):
    return DiscoveryIntent(outcome="Retrieve verified public information",
                           shape=shape, required_fields=list(fields),
                           search_terms=list(terms),
                           missing_capabilities=["public_record_read"],
                           completion_criteria=["Source grounded title is present"],
                           uncertainty="No authorization to perform a real request",
                           abstain=abstain)


class FixtureIntent:
    def __init__(self, value):
        self.value, self.calls = value, 0

    async def interpret(self, mission, inventory):
        self.calls += 1
        return self.value


def planner(store=None, source=None, mission=BOOK_MISSION):
    store = store or MemoryStore()
    source = source or FixtureIntent(intent())
    return DiscoveryMissionPlanner(store, store.project.id,
                                   OfflineDiscovery(load_fixture_catalog(CATALOG)), source,
                                   MissionRequirements(mission, "detail", ("title",)))


@pytest.mark.asyncio
async def test_unknown_capability_search_restart_and_pending_approval():
    store, source = MemoryStore(), FixtureIntent(intent())
    engine = planner(store, source)
    mission = await engine.receive(BOOK_MISSION)
    first = await engine.advance(mission.mission_id, now_tick=1)
    assert first.state is MissionState.RECEIVED
    assert first.checkpoint["discovery_phase"] == "searched"
    assert first.checkpoint["lexical_top_id"] == "books"
    # New planner instance simulates process restart; no repeat model call.
    restarted = planner(store, source)
    second = await restarted.advance(mission.mission_id, now_tick=1)
    assert second.checkpoint["discovery_phase"] == "selected"
    result = await planner(store, source).advance(mission.mission_id, now_tick=1)
    assert result.state is MissionState.PENDING
    assert result.checkpoint["catalog_id"] == "books"
    assert result.required_capabilities == ["open_library_work_read"]
    assert source.calls == 1
    assert "CONTRACT_VALIDATED" in result.state_history
    assert "ISOLATED_VALIDATION" in result.state_history
    assert await planner(store, source).advance(mission.mission_id, now_tick=1) == result
    assert source.calls == 1
    assert all("APPROVED" not in raw for raw in store.capabilities.values())


@pytest.mark.asyncio
@pytest.mark.parametrize("proposal,code", [
    (intent(abstain=True), "INTENT_ABSTAINED"),
    (intent(terms=("unrelated",)), "DOCUMENTATION_NOT_FOUND"),
    (intent(shape="collection"), "INTENT_ORACLE_MISMATCH"),
    (intent(fields=("status",)), "INTENT_ORACLE_MISMATCH"),
])
async def test_abstention_or_incompatibility_blocks_without_acquisition(proposal, code):
    store = MemoryStore()
    engine = planner(store, FixtureIntent(proposal))
    mission = await engine.receive(BOOK_MISSION)
    result = await engine.advance(mission.mission_id, now_tick=1)
    assert result.state is MissionState.BLOCKED
    assert result.failure_reasons == [code]
    assert store.capabilities == {}


@pytest.mark.asyncio
async def test_concurrent_stage_claim_prevents_duplicate_model_calls():
    class SlowIntent(FixtureIntent):
        def __init__(self):
            super().__init__(intent())
            self.started, self.release = asyncio.Event(), asyncio.Event()

        async def interpret(self, mission, inventory):
            self.calls += 1
            self.started.set()
            await self.release.wait()
            return self.value

    store, source = MemoryStore(), SlowIntent()
    engine = planner(store, source)
    mission = await engine.receive(BOOK_MISSION)
    first = asyncio.create_task(engine.advance(mission.mission_id, now_tick=1))
    await source.started.wait()
    other = await engine.advance(mission.mission_id, now_tick=1)
    assert other.checkpoint["discovery_phase"] == "interpreting"
    source.release.set()
    results = await asyncio.gather(first)
    assert all(not isinstance(item, Exception) for item in results)
    assert source.calls == 1
    assert store.missions[mission.mission_id][1] == 2


@pytest.mark.asyncio
async def test_model_intent_receives_no_catalog_or_operation_id():
    class FakeClient:
        async def chat(self, messages, **kwargs):
            self.messages, self.kwargs = messages, kwargs
            return SimpleNamespace(done=True, done_reason="stop",
                                   message=SimpleNamespace(content=intent().model_dump_json(),
                                                           tool_calls=[]))
    client = FakeClient()
    result = await OllamaIntentSource(client, "fixture-model").interpret(BOOK_MISSION, ())
    assert result.shape == "detail"
    assert client.kwargs["format"] == DiscoveryIntent.model_json_schema()
    assert client.kwargs["tools"] is None and client.kwargs["stream"] is False
    assert "get_work" not in str(client.messages)
    assert "openlibrary" not in str(client.messages).lower()


@pytest.mark.asyncio
@pytest.mark.parametrize("content,code", [
    ("```json\n{}\n```", "INTENT_SCHEMA_INVALID"),
    ('{"outcome":"x","outcome":"y"}', "INTENT_SCHEMA_INVALID"),
    ("", "MODEL_RESPONSE_INCOMPLETE"),
])
async def test_model_intent_fails_closed_without_private_output(content, code):
    class FakeClient:
        async def chat(self, *args, **kwargs):
            return SimpleNamespace(done=True, done_reason="stop",
                                   message=SimpleNamespace(content=content, tool_calls=[]))
    with pytest.raises(CapabilityRefusal, match=code):
        await OllamaIntentSource(FakeClient(), "fixture-model").interpret(BOOK_MISSION, ())
