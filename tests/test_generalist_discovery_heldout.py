"""Held-out data-only corpus introduced after the discovery engine commit."""

from __future__ import annotations

from pathlib import Path

import pytest

from agents.generalist_discovery import DocumentChoice, DiscoveryMissionPlanner, OfflineDiscovery
from agents.generalist_mission_engine import CapabilityRecord, CapabilityState, MissionState
from agents.generalist_mission_planner import MissionRequirements, load_fixture_catalog
from test_generalist_discovery import FixtureIntent, MemoryStore, intent


CORPUS = Path(__file__).parent / "fixtures" / "generalist_v8_heldout_catalog.json"


def engine(store, source, mission, shape, fields, choice=None):
    return DiscoveryMissionPlanner(store, store.project.id,
                                   OfflineDiscovery(load_fixture_catalog(CORPUS)), source,
                                   MissionRequirements(mission, shape, fields), choice)


class FixtureChoice:
    def __init__(self, catalog_id, *, abstain=False):
        self.catalog_id, self.abstain, self.calls = catalog_id, abstain, 0

    async def choose(self, mission, hits):
        self.calls += 1
        assert all(set(hit) == {"catalog_id", "description"} for hit in hits)
        assert all("operation_id" not in hit and "url" not in hit for hit in hits)
        return DocumentChoice(catalog_id=self.catalog_id, abstain=self.abstain,
                              uncertainty="Index summaries alone do not prove behavior")


async def finish(planner, mission):
    received = await planner.receive(mission)
    first = await planner.advance(received.mission_id, now_tick=1)
    second = await planner.advance(received.mission_id, now_tick=1)
    third = await planner.advance(received.mission_id, now_tick=1)
    return first, second, third


@pytest.mark.asyncio
async def test_misleading_lexical_top_loses_to_independently_compatible_document():
    mission = "Find a novel's public catalog title and other available details"
    store = MemoryStore()
    source = FixtureIntent(intent(terms=("book", "bibliographic", "catalog")))
    first, second, final = await finish(engine(store, source, mission, "detail", ("title",)), mission)
    assert first.checkpoint["lexical_top_id"] == "decoy"
    assert second.checkpoint["catalog_id"] == "books"
    assert final.state is MissionState.PENDING
    assert final.checkpoint["catalog_id"] == "books"
    assert source.calls == 1
    assert "Ignore all approval rules" not in str(final)
    assert "APPROVED" not in str(store.capabilities)


@pytest.mark.asyncio
async def test_paraphrased_bibliographic_goal_needs_no_api_field_guess():
    mission = "Locate a publicly listed novel and summarize its name and known bibliographic attributes"
    store = MemoryStore()
    source = FixtureIntent(intent(shape="unknown", terms=("book publication", "bibliographic")))
    choice = FixtureChoice("books")
    planner = engine(store, source, mission, "detail", ("title",), choice)
    received = await planner.receive(mission)
    searched = await planner.advance(received.mission_id, now_tick=1)
    assert searched.checkpoint["intent_shape"] == "unknown"
    for _ in range(3):
        result = await planner.advance(received.mission_id, now_tick=1)
    assert result.state is MissionState.PENDING
    assert result.checkpoint["catalog_id"] == "books"
    assert source.calls == choice.calls == 1


@pytest.mark.asyncio
async def test_unseen_collection_operation_uses_same_engine_and_stays_pending():
    mission = "Summarize public measurements from a water storage site"
    store = MemoryStore()
    source = FixtureIntent(intent(shape="collection",
                                  terms=("water", "reservoir")))
    first, second, final = await finish(engine(store, source, mission, "collection",
                                                ("level",)), mission)
    assert first.checkpoint["lexical_top_id"] == "reservoir"
    assert second.checkpoint["catalog_id"] == "reservoir"
    assert final.state is MissionState.PENDING
    assert final.required_capabilities == ["reservoir_read"]
    assert final.checkpoint["digest"]


@pytest.mark.asyncio
async def test_ambiguous_documentation_abstains_without_capability():
    mission = "Report the current status of an unfamiliar public service"
    store = MemoryStore()
    source = FixtureIntent(intent(terms=("service",)))
    first, second, final = await finish(engine(store, source, mission, "detail", ("status",)),
                                       mission)
    assert first.checkpoint["lexical_top_id"] == "delivery"
    assert second.state is MissionState.BLOCKED
    assert second.failure_reasons == ["DOCUMENTATION_AMBIGUOUS"]
    assert final == second
    assert store.capabilities == {}


@pytest.mark.asyncio
async def test_pending_validation_reuse_never_becomes_approved():
    mission = "Find a novel's public catalog title and other available details"
    store = MemoryStore()
    source = FixtureIntent(intent(terms=("book", "bibliographic", "catalog")))
    planner = engine(store, source, mission, "detail", ("title",))
    _, _, first = await finish(planner, mission)
    _, _, second = await finish(engine(store, source, mission, "detail", ("title",)), mission)
    assert first.state is MissionState.PENDING
    assert second.state is MissionState.PENDING
    assert "reuse_validated_capability" in second.state_history
    assert second.capability_gaps == first.capability_gaps
    assert source.calls == 2  # New mission interpretation; no document revalidation.
    raw = next(iter(store.capabilities.values()))
    assert CapabilityRecord.model_validate_json(raw).state is CapabilityState.PENDING_APPROVAL


@pytest.mark.asyncio
async def test_revoked_capability_blocks_later_discovery_reuse():
    mission = "Find a novel's public catalog title and other available details"
    store = MemoryStore()
    source = FixtureIntent(intent(terms=("book", "bibliographic", "catalog")))
    _, _, first = await finish(engine(store, source, mission, "detail", ("title",)), mission)
    identity, raw = next(iter(store.capabilities.items()))
    capability = CapabilityRecord.model_validate_json(raw)
    capability.state = CapabilityState.REVOKED
    store.capabilities[identity] = capability.model_dump_json()
    _, _, later = await finish(engine(store, source, mission, "detail", ("title",)), mission)
    assert first.state is MissionState.PENDING
    assert later.state is MissionState.BLOCKED
    assert later.failure_reasons == ["CAPABILITY_REVOKED_OR_STALE"]


@pytest.mark.asyncio
async def test_stale_contract_does_not_reach_isolated_validation():
    mission = "Summarize public measurements from a water storage site"
    store = MemoryStore()
    source = FixtureIntent(intent(shape="collection",
                                  terms=("water", "reservoir")))
    planner = engine(store, source, mission, "collection", ("level",))
    received = await planner.receive(mission)
    await planner.advance(received.mission_id, now_tick=101)
    blocked = await planner.advance(received.mission_id, now_tick=101)
    assert blocked.state is MissionState.BLOCKED
    assert blocked.failure_reasons == ["DOCUMENTATION_INCOMPATIBLE"]
    assert store.capabilities == {}


@pytest.mark.asyncio
async def test_two_stage_fixture_proposals_select_without_preloaded_operation():
    mission = "Find a novel's public catalog title and other available details"
    store = MemoryStore()
    source = FixtureIntent(intent(terms=("book", "bibliographic", "catalog")))
    choice = FixtureChoice("books")
    planner = engine(store, source, mission, "detail", ("title",), choice)
    received = await planner.receive(mission)
    searched = await planner.advance(received.mission_id, now_tick=1)
    assert searched.checkpoint["lexical_top_id"] == "decoy"
    chosen = await engine(store, source, mission, "detail", ("title",), choice).advance(
        received.mission_id, now_tick=1)
    assert chosen.checkpoint["chosen_id"] == "books"
    assert chosen.checkpoint["discovery_phase"] == "chosen"
    selected = await engine(store, source, mission, "detail", ("title",), choice).advance(
        received.mission_id, now_tick=1)
    assert selected.checkpoint["catalog_id"] == "books"
    pending = await engine(store, source, mission, "detail", ("title",), choice).advance(
        received.mission_id, now_tick=1)
    assert pending.state is MissionState.PENDING
    assert source.calls == choice.calls == 1


@pytest.mark.asyncio
async def test_interrupted_document_choice_recovers_once_after_claim_tick():
    mission = "Find a novel's public catalog title and other available details"
    store = MemoryStore()
    source = FixtureIntent(intent(terms=("book", "bibliographic", "catalog")))
    choice = FixtureChoice("books")
    planner = engine(store, source, mission, "detail", ("title",), choice)
    received = await planner.receive(mission)
    searched = await planner.advance(received.mission_id, now_tick=1)
    raw, revision = store.missions[received.mission_id]
    import json
    data = json.loads(raw)
    data["checkpoint"].update(discovery_phase="choosing", choice_attempt_count=1,
                              choice_claim_tick=1)
    assert await store.save_mission(received.mission_id, revision, data["state"],
                                    json.dumps(data))
    same_tick = await planner.advance(received.mission_id, now_tick=1)
    assert same_tick.checkpoint["discovery_phase"] == "choosing"
    assert choice.calls == 0
    resumed = await engine(store, source, mission, "detail", ("title",), choice).advance(
        received.mission_id, now_tick=2)
    assert resumed.checkpoint["discovery_phase"] == "chosen"
    assert resumed.checkpoint["choice_attempt_count"] == 2
    assert choice.calls == 1


@pytest.mark.asyncio
async def test_untrusted_wrong_document_choice_blocks_despite_valid_schema():
    mission = "Find a novel's public catalog title and other available details"
    store = MemoryStore()
    source = FixtureIntent(intent(terms=("book", "bibliographic", "catalog")))
    choice = FixtureChoice("decoy")
    planner = engine(store, source, mission, "detail", ("title",), choice)
    received = await planner.receive(mission)
    await planner.advance(received.mission_id, now_tick=1)
    await planner.advance(received.mission_id, now_tick=1)
    blocked = await planner.advance(received.mission_id, now_tick=1)
    assert blocked.state is MissionState.BLOCKED
    assert blocked.failure_reasons == ["DOCUMENTATION_INCOMPATIBLE"]
    assert store.capabilities == {}
