"""Opt-in bounded offline documentation discovery for unknown missions.

Catalog metadata is reviewed search authority, not executable authority. A
model can propose an intent and query but cannot select an executable URL,
mint a grant, or satisfy the independently supplied mission requirements.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import re
from typing import Annotated, Literal, Protocol
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agents.generalist_approval_broker import ProjectApprovalBroker
from agents.generalist_capability import CapabilityRefusal
from agents.generalist_generic_read import extract_openapi_read
from agents.generalist_mission_engine import MissionRecord, MissionState, _now
from agents.generalist_mission_planner import (
    DocumentationCatalog, MissionRequirements, _proposal_pairs,
)
from agents.generalist_mission_store import ProjectMissionStore
from agents.generalist_orchestrator import DeclarativeHttpReadDriver, GeneralistMissionOrchestrator


_TERM = re.compile(r"[a-z][a-z0-9_]{2,31}\Z")
_WORDS = re.compile(r"[a-z][a-z0-9_]{2,31}")
MAX_RESULTS = 3
MAX_DOCUMENT_BYTES = 49152
Term = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{2,31}$")]
Criterion = Annotated[str, Field(min_length=8, max_length=160)]


class DiscoveryIntent(BaseModel):
    """Untrusted bounded mission interpretation; never an approval."""

    model_config = ConfigDict(extra="forbid", strict=True)
    outcome: str = Field(min_length=8, max_length=160)
    shape: Literal["detail", "collection"]
    required_fields: list[Term] = Field(min_length=1, max_length=6)
    search_terms: list[Term] = Field(min_length=1, max_length=4)
    missing_capabilities: list[Term] = Field(min_length=1, max_length=4)
    completion_criteria: list[Criterion] = Field(min_length=1, max_length=4)
    uncertainty: str = Field(min_length=1, max_length=160)
    abstain: bool

    @classmethod
    def validate_terms(cls, intent: DiscoveryIntent) -> DiscoveryIntent:
        """Bound identifiers and reject control/instruction payloads in claims."""
        if type(intent) is not DiscoveryIntent:
            raise CapabilityRefusal("INTENT_SCHEMA_INVALID")
        for term in intent.required_fields + intent.search_terms + intent.missing_capabilities:
            if type(term) is not str or _TERM.fullmatch(term) is None:
                raise CapabilityRefusal("INTENT_SCHEMA_INVALID")
        for claim in [intent.outcome, intent.uncertainty, *intent.completion_criteria]:
            if (type(claim) is not str or not 8 <= len(claim) <= 160
                    or any(ord(c) < 32 or ord(c) == 127 for c in claim)):
                raise CapabilityRefusal("INTENT_SCHEMA_INVALID")
        return intent


class IntentSource(Protocol):
    """One bounded proposal source; it receives no document or operation ID."""

    async def interpret(self, mission: str, inventory: tuple[str, ...]) -> DiscoveryIntent: ...


class DocumentChoice(BaseModel):
    """Untrusted choice among reviewed search-hit metadata; not authority."""

    model_config = ConfigDict(extra="forbid", strict=True)
    catalog_id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")
    abstain: bool
    uncertainty: str = Field(min_length=8, max_length=160)


class ChoiceSource(Protocol):
    """Candidate document choice receives no full document or operation ID."""

    async def choose(self, mission: str, hits: list[dict[str, str]]) -> DocumentChoice: ...


class OllamaDocumentChoiceSource:
    """One additional bounded local-model choice over short index metadata."""

    def __init__(self, client: object, model: str) -> None:
        self.client, self.model = client, model

    async def choose(self, mission: str, hits: list[dict[str, str]]) -> DocumentChoice:
        """Choose or abstain without seeing documents, contracts, or endpoints."""
        from core.llm_client import ChatMessage
        if (not 1 <= len(hits) <= MAX_RESULTS or
                any(type(hit) is not dict or set(hit) != {"catalog_id", "description"}
                    or len(hit["description"]) > 160 for hit in hits)):
            raise CapabilityRefusal("SEARCH_RESULT_INVALID")
        try:
            response = await asyncio.wait_for(self.client.chat(
                [ChatMessage(role="system", content=(
                    "Choose the most relevant document metadata for the mission. "
                    "Use only one supplied catalog_id; set abstain true and catalog_id none "
                    "when evidence is insufficient. A choice is not an execution grant.")),
                 ChatMessage(role="user", content=json.dumps({"mission": mission,
                                                               "search_hits": hits}))],
                model=self.model, tools=None, stream=False,
                options={"temperature": 0, "num_predict": 160},
                format=DocumentChoice.model_json_schema()), timeout=30)
        except (TimeoutError, httpx.TimeoutException):
            raise CapabilityRefusal("MODEL_TIMEOUT") from None
        except (httpx.HTTPError, OSError, ValueError, TypeError, KeyError):
            raise CapabilityRefusal("MODEL_UNAVAILABLE") from None
        try:
            content = response.message.content
            if response.message.tool_calls:
                raise CapabilityRefusal("FORBIDDEN_TOOL_ACTIVITY")
            if (response.done is not True or getattr(response, "done_reason", None) not in {None, "stop"}
                    or type(content) is not str or not 1 <= len(content.encode()) <= 512):
                raise CapabilityRefusal("MODEL_RESPONSE_INCOMPLETE")
            result = DocumentChoice.model_validate(json.loads(content, object_pairs_hook=_proposal_pairs))
            if result.abstain:
                if result.catalog_id != "none":
                    raise CapabilityRefusal("DOCUMENT_CHOICE_INVALID")
            elif result.catalog_id not in {hit["catalog_id"] for hit in hits}:
                raise CapabilityRefusal("DOCUMENT_CHOICE_INVALID")
            return result
        except CapabilityRefusal:
            raise
        except (AttributeError, UnicodeError, ValueError, TypeError, ValidationError):
            raise CapabilityRefusal("DOCUMENT_CHOICE_INVALID") from None


class OllamaIntentSource:
    """One schema-constrained local Ollama call with no tools or fallback."""

    def __init__(self, client: object, model: str) -> None:
        self.client, self.model = client, model

    async def interpret(self, mission: str, inventory: tuple[str, ...]) -> DiscoveryIntent:
        """Interpret a mission without disclosing catalog answers to the model."""
        from core.llm_client import ChatMessage
        if not 8 <= len(mission) <= 240 or len(inventory) > 64:
            raise CapabilityRefusal("MISSION_INVALID")
        prompt = ("Propose an information need for the mission as one JSON object under "
                  "the supplied schema. Use short lowercase search_terms and required_fields. "
                  "Set abstain true if the mission is ambiguous or unsupported. "
                  "Inventory names are not grants. Do not claim completion or authority.")
        try:
            response = await asyncio.wait_for(self.client.chat(
                [ChatMessage(role="system", content=prompt),
                 ChatMessage(role="user", content=json.dumps({"mission": mission,
                                                               "capability_inventory": inventory}))],
                model=self.model, tools=None, stream=False,
                options={"temperature": 0, "num_predict": 640},
                format=DiscoveryIntent.model_json_schema()), timeout=90)
        except (TimeoutError, httpx.TimeoutException):
            raise CapabilityRefusal("MODEL_TIMEOUT") from None
        except (httpx.HTTPError, OSError, ValueError, TypeError, KeyError):
            raise CapabilityRefusal("MODEL_UNAVAILABLE") from None
        try:
            content = response.message.content
            if response.message.tool_calls:
                raise CapabilityRefusal("FORBIDDEN_TOOL_ACTIVITY")
            if (response.done is not True or getattr(response, "done_reason", None) not in {None, "stop"}
                    or type(content) is not str or not 1 <= len(content.encode()) <= 2048):
                raise CapabilityRefusal("MODEL_RESPONSE_INCOMPLETE")
            value = json.loads(content, object_pairs_hook=_proposal_pairs)
            return DiscoveryIntent.validate_terms(DiscoveryIntent.model_validate(value))
        except CapabilityRefusal:
            raise
        except (AttributeError, UnicodeError, ValueError, TypeError, ValidationError):
            raise CapabilityRefusal("INTENT_SCHEMA_INVALID") from None


@dataclass(frozen=True)
class SearchHit:
    """Only reviewed index metadata; full documents stay behind retrieval."""

    catalog_id: str
    score: int


class OfflineDiscovery:
    """Rank a trusted offline index and bound subsequent document retrieval."""

    def __init__(self, catalog: DocumentationCatalog) -> None:
        self.catalog = catalog

    def search(self, terms: list[str]) -> tuple[SearchHit, ...]:
        """Return at most three metadata hits; no full document is read."""
        if (not 1 <= len(terms) <= 4 or any(type(t) is not str or _TERM.fullmatch(t) is None
                                            for t in terms)):
            raise CapabilityRefusal("SEARCH_QUERY_INVALID")
        query = set(terms)
        hits = []
        for entry in self.catalog.entries:
            indexed = set(_WORDS.findall(entry.description.lower())) | set(entry.terms)
            score = len(query & indexed)
            if score:
                hits.append(SearchHit(entry.catalog_id, score))
        return tuple(sorted(hits, key=lambda hit: (-hit.score, hit.catalog_id))[:MAX_RESULTS])

    def retrieve(self, hits: tuple[SearchHit, ...]):
        """Digest-check selected documents only, within a total byte budget."""
        if not 1 <= len(hits) <= MAX_RESULTS or len({h.catalog_id for h in hits}) != len(hits):
            raise CapabilityRefusal("SEARCH_RESULT_INVALID")
        documents = []
        total = 0
        for hit in hits:
            selected = self.catalog.load(self.catalog.get(hit.catalog_id))
            total += len(selected.document.encode())
            if total > MAX_DOCUMENT_BYTES:
                raise CapabilityRefusal("DOCUMENT_BUDGET_EXCEEDED")
            documents.append(selected)
        return tuple(documents)

    def metadata(self, hits: tuple[SearchHit, ...]) -> list[dict[str, str]]:
        """Expose reviewed summaries only, never an operation or destination."""
        if not 1 <= len(hits) <= MAX_RESULTS:
            raise CapabilityRefusal("SEARCH_RESULT_INVALID")
        return [{"catalog_id": hit.catalog_id,
                 "description": self.catalog.get(hit.catalog_id).description}
                for hit in hits]


class DiscoveryMissionPlanner:
    """Restart-safe, Project-scoped discovery up to pending approval."""

    def __init__(self, store: ProjectMissionStore, project_id: str,
                 discovery: OfflineDiscovery, intent_source: IntentSource,
                 requirements: MissionRequirements,
                 choice_source: ChoiceSource | None = None) -> None:
        if project_id == "default" or getattr(store.project, "id", None) != project_id:
            raise CapabilityRefusal("PROJECT_MISMATCH")
        self.store, self.project_id = store, project_id
        self.discovery, self.intent_source, self.requirements = discovery, intent_source, requirements
        self.choice_source = choice_source

    async def receive(self, objective: str) -> MissionRecord:
        """Persist the mission before planning or inspecting documentation."""
        if type(objective) is not str or not 8 <= len(objective) <= 240:
            raise CapabilityRefusal("MISSION_INVALID")
        mission = MissionRecord(project_id=self.project_id, objective=objective,
                                pending_steps=["interpret", "search", "validate", "authorize"])
        if not await self.store.create_mission(mission.mission_id, mission.user_id,
                                               mission.state.value, mission.model_dump_json()):
            raise CapabilityRefusal("MISSION_EXISTS")
        return mission

    async def _load(self, mission_id: UUID) -> tuple[MissionRecord, int]:
        raw = await self.store.get_mission(mission_id)
        if raw is None:
            raise CapabilityRefusal("MISSION_NOT_FOUND")
        mission = MissionRecord.model_validate_json(raw[0])
        if mission.project_id != self.project_id:
            raise CapabilityRefusal("PROJECT_MISMATCH")
        return mission, raw[1]

    async def advance(self, mission_id: UUID, *, now_tick: int) -> MissionRecord:
        """Commit one discovery stage; resume without repeating model calls."""
        mission, revision = await self._load(mission_id)
        if mission.state is not MissionState.RECEIVED:
            return mission
        if self.requirements.objective != mission.objective:
            raise CapabilityRefusal("MISSION_REQUIREMENTS_INVALID")
        phase = mission.checkpoint.get("discovery_phase", "new")
        try:
            if phase in {"new", "interpreting"}:
                if phase == "interpreting":
                    attempt = int(mission.checkpoint.get("intent_attempt_count", 0))
                    claim_tick = int(mission.checkpoint.get("intent_claim_tick", 0))
                    if now_tick <= claim_tick:
                        return mission
                    if attempt >= 2:
                        raise CapabilityRefusal("PLANNING_INTERRUPTED")
                else:
                    attempt = 0
                # Claim before the model call. A concurrent worker at this
                # tick sees the claim and cannot issue a duplicate request.
                mission.checkpoint.update({"discovery_phase": "interpreting",
                                           "intent_attempt_count": attempt + 1,
                                           "intent_claim_tick": now_tick})
                if not await self.store.save_mission(mission_id, revision, mission.state.value,
                                                     mission.model_dump_json()):
                    raise CapabilityRefusal("MISSION_CONCURRENT_CHANGE")
                mission, revision = await self._load(mission_id)
                inventory = await self.store.list_capability_identities()
                names = tuple(sorted({capability_id for capability_id, _, _ in inventory}))
                intent = DiscoveryIntent.validate_terms(
                    await self.intent_source.interpret(mission.objective, names))
                if intent.abstain:
                    raise CapabilityRefusal("INTENT_ABSTAINED")
                if (intent.shape != self.requirements.shape or
                    not set(self.requirements.required_result_fields).issubset(intent.required_fields)):
                    raise CapabilityRefusal("INTENT_ORACLE_MISMATCH")
                hits = self.discovery.search(intent.search_terms)
                if not hits:
                    raise CapabilityRefusal("DOCUMENTATION_NOT_FOUND")
                mission.state_history.extend(["PLAN_CREATED", "CAPABILITY_INVENTORY_CHECKED",
                                              MissionState.GAP.value, "DOCUMENTATION_SEARCHED"])
                mission.checkpoint.update({"discovery_phase": "searched",
                                           "candidate_ids": json.dumps([h.catalog_id for h in hits]),
                                           "lexical_top_id": hits[0].catalog_id,
                                           "intent_shape": intent.shape,
                                           "intent_fields": json.dumps(intent.required_fields),
                                           "planning_outcome": intent.outcome,
                                           "planning_uncertainty": intent.uncertainty,
                                           "completion_criteria": " | ".join(intent.completion_criteria),
                                           "inventory_count": len(inventory)})
            elif phase in {"searched", "choosing", "chosen"}:
                ids = json.loads(str(mission.checkpoint["candidate_ids"]))
                if type(ids) is not list or not 1 <= len(ids) <= MAX_RESULTS:
                    raise CapabilityRefusal("SEARCH_RESULT_INVALID")
                hits = tuple(SearchHit(item, 1) for item in ids)
                if self.choice_source is not None and phase in {"searched", "choosing"}:
                    if phase == "choosing":
                        count = int(mission.checkpoint.get("choice_attempt_count", 0))
                        claim_tick = int(mission.checkpoint.get("choice_claim_tick", 0))
                        if now_tick <= claim_tick:
                            return mission
                        if count >= 2:
                            raise CapabilityRefusal("PLANNING_INTERRUPTED")
                    else:
                        count = 0
                    mission.checkpoint.update({"discovery_phase": "choosing",
                                               "choice_attempt_count": count + 1,
                                               "choice_claim_tick": now_tick})
                    if not await self.store.save_mission(mission_id, revision, mission.state.value,
                                                         mission.model_dump_json()):
                        raise CapabilityRefusal("MISSION_CONCURRENT_CHANGE")
                    mission, revision = await self._load(mission_id)
                    choice = await self.choice_source.choose(
                        mission.objective, self.discovery.metadata(hits))
                    if type(choice) is not DocumentChoice:
                        raise CapabilityRefusal("DOCUMENT_CHOICE_INVALID")
                    if choice.abstain:
                        raise CapabilityRefusal("DOCUMENT_CHOICE_ABSTAINED")
                    if choice.catalog_id not in ids:
                        raise CapabilityRefusal("DOCUMENT_CHOICE_INVALID")
                    mission.checkpoint.update({"discovery_phase": "chosen",
                                               "chosen_id": choice.catalog_id})
                    mission.state_history.append("DOCUMENT_CANDIDATE_PROPOSED")
                    mission.updated_at = _now()
                    if not await self.store.save_mission(mission_id, revision, mission.state.value,
                                                         mission.model_dump_json()):
                        raise CapabilityRefusal("MISSION_CONCURRENT_CHANGE")
                    return mission
                if self.choice_source is not None:
                    chosen = mission.checkpoint.get("chosen_id")
                    if chosen not in ids:
                        raise CapabilityRefusal("DOCUMENT_CHOICE_INVALID")
                    hits = (SearchHit(str(chosen), 1),)
                selected = self.discovery.retrieve(hits)
                compatible = []
                for document in selected:
                    contract = extract_openapi_read(document.document)
                    try:
                        self.requirements.check(mission.objective, contract)
                    except CapabilityRefusal:
                        continue
                    if now_tick <= contract.valid_until_tick:
                        compatible.append((document, contract))
                if len(compatible) != 1:
                    raise CapabilityRefusal("DOCUMENTATION_AMBIGUOUS" if compatible else
                                            "DOCUMENTATION_INCOMPATIBLE")
                document, contract = compatible[0]
                mission.required_capabilities = [contract.capability_id]
                mission.capability_gaps = [contract.capability_id]
                mission.checkpoint.update({"discovery_phase": "selected",
                                           "catalog_id": document.entry.catalog_id,
                                           "source_identity": document.evidence.source_identity,
                                           "source_digest": document.evidence.content_digest,
                                           "contract_digest": contract.documentation_digest,
                                           "source_replayed_at": _now().isoformat()})
                mission.state_history.extend(["DOCUMENTATION_SELECTED", "DOCUMENTATION_INSPECTED",
                                              "OPERATION_PROPOSED", "CONTRACT_VALIDATED"])
            elif phase == "selected":
                entry = self.discovery.catalog.get(str(mission.checkpoint["catalog_id"]))
                selected = self.discovery.catalog.load(entry)
                contract = extract_openapi_read(selected.document)
                if (selected.evidence.content_digest != mission.checkpoint.get("source_digest")
                    or contract.documentation_digest != mission.checkpoint.get("contract_digest")
                    or mission.required_capabilities != [contract.capability_id]):
                    raise CapabilityRefusal("DOCUMENT_CHANGED")
                self.requirements.check(mission.objective, contract)
                driver = DeclarativeHttpReadDriver(
                    entry.reviewed_request, entry.inputs,
                    transport=httpx.MockTransport(
                        lambda _: httpx.Response(200, json=entry.isolated_sample)),
                    documentation=selected.evidence)
                broker = ProjectApprovalBroker(self.store, reviewed_request=entry.reviewed_request)
                engine = GeneralistMissionOrchestrator(self.store, self.project_id, driver,
                                                       approval_broker=broker)
                return await engine.assess(mission_id, selected.document, entry.isolated_sample,
                                           now_tick=now_tick)
            else:
                raise CapabilityRefusal("MISSION_STATE_INVALID")
            mission.updated_at = _now()
            if not await self.store.save_mission(mission_id, revision, mission.state.value,
                                                 mission.model_dump_json()):
                raise CapabilityRefusal("MISSION_CONCURRENT_CHANGE")
            return mission
        except CapabilityRefusal as exc:
            if exc.code == "MISSION_CONCURRENT_CHANGE":
                raise
            current, current_revision = await self._load(mission_id)
            if current.state is MissionState.RECEIVED:
                current.state = MissionState.BLOCKED
                current.state_history.append(MissionState.BLOCKED.value)
                current.failure_reasons.append(exc.code)
                current.resume_eligible = False
                current.updated_at = _now()
                if not await self.store.save_mission(mission_id, current_revision,
                                                     current.state.value, current.model_dump_json()):
                    raise CapabilityRefusal("MISSION_CONCURRENT_CHANGE") from None
            return current
