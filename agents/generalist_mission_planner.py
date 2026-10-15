"""Opt-in mission-to-document planning; no model or document grants authority.

The catalog is trusted configuration, while every snapshot and candidate
operation is untrusted data. This module never performs external I/O.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Protocol
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from agents.generalist_approval_broker import ProjectApprovalBroker, ReviewedReadRequest
from agents.generalist_capability import CapabilityRefusal, _unique_pairs
from agents.generalist_documentation import DocumentationEvidence, reviewed_offline_snapshot
from agents.generalist_generic_read import extract_openapi_read, validated_request
from agents.generalist_mission_engine import MissionRecord, MissionState, _now
from agents.generalist_orchestrator import DeclarativeHttpReadDriver, GeneralistMissionOrchestrator


_WORD = re.compile(r"[a-z][a-z0-9]{2,}")
_IDENTIFIER = re.compile(r"[a-z][a-z0-9_]{0,31}\Z")


class PlanningProposal(BaseModel):
    """Untrusted model or fixture suggestion, never an executable grant."""

    model_config = ConfigDict(extra="forbid", strict=True)
    catalog_id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")
    operation_id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")
    outcome: str = Field(min_length=8, max_length=160)
    completion_criteria: list[str] = Field(min_length=1, max_length=4)
    uncertainty: str = Field(min_length=1, max_length=160)

    @field_validator("outcome", "uncertainty")
    @classmethod
    def safe_text(cls, value: str) -> str:
        """Keep only short, printable planning claims in durable checkpoints."""
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("control character")
        return value

    @field_validator("completion_criteria")
    @classmethod
    def safe_criteria(cls, values: list[str]) -> list[str]:
        """Bound each item, not only the number of items."""
        if any(type(v) is not str or not 8 <= len(v) <= 120 or
               any(ord(c) < 32 or ord(c) == 127 for c in v) for v in values):
            raise ValueError("invalid criteria")
        return values


@dataclass(frozen=True)
class CatalogEntry:
    """Reviewed source and exact candidate files; descriptions confer no authority."""

    catalog_id: str
    description: str
    terms: tuple[str, ...]
    source_identity: str
    snapshot_file: Path
    snapshot_sha256: str
    contract_file: Path
    contract_sha256: str
    inputs: dict[str, object]
    isolated_sample: object
    reviewed_request: ReviewedReadRequest


@dataclass(frozen=True)
class SelectedDocument:
    """Digest-checked offline evidence and independently validated candidate."""

    entry: CatalogEntry
    evidence: DocumentationEvidence
    document: str


class DocumentationCatalog:
    """Select only reviewed, digest-pinned offline sources by generic relevance."""

    def __init__(self, entries: tuple[CatalogEntry, ...]) -> None:
        if not 1 <= len(entries) <= 12 or len({e.catalog_id for e in entries}) != len(entries):
            raise CapabilityRefusal("CATALOG_INVALID")
        for entry in entries:
            if (_IDENTIFIER.fullmatch(entry.catalog_id) is None or
                not 2 <= len(entry.terms) <= 12 or
                any(_WORD.fullmatch(term) is None for term in entry.terms) or
                len(set(entry.terms)) != len(entry.terms) or
                not 8 <= len(entry.description) <= 160):
                raise CapabilityRefusal("CATALOG_INVALID")
        self.entries = entries

    def select(self, mission: str) -> CatalogEntry:
        """Return one relevant source; ambiguous or weak evidence is a gap."""
        words = set(_WORD.findall(mission.lower()))
        scored = [(len(words.intersection(entry.terms)), entry) for entry in self.entries]
        best = max(score for score, _ in scored)
        choices = [entry for score, entry in scored if score == best]
        if best < 2:
            raise CapabilityRefusal("DOCUMENTATION_NOT_FOUND")
        if len(choices) != 1:
            raise CapabilityRefusal("DOCUMENTATION_AMBIGUOUS")
        return choices[0]

    def get(self, catalog_id: str) -> CatalogEntry:
        """Resolve only a reviewed catalog identifier."""
        for entry in self.entries:
            if entry.catalog_id == catalog_id:
                return entry
        raise CapabilityRefusal("DOCUMENTATION_NOT_FOUND")

    def load(self, entry: CatalogEntry) -> SelectedDocument:
        """Read a pinned snapshot and operation file; never follow source links."""
        if self.get(entry.catalog_id) is not entry:
            raise CapabilityRefusal("CATALOG_INVALID")
        evidence = reviewed_offline_snapshot(entry.snapshot_file, entry.source_identity,
                                             entry.snapshot_sha256)
        path = entry.contract_file
        try:
            if (path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(
                    Path(__file__).resolve().parents[1])):
                raise ValueError("unreviewed file")
            raw = path.read_bytes()
            if not 1 <= len(raw) <= 16384 or sha256(raw).hexdigest() != entry.contract_sha256:
                raise ValueError("digest")
            document = raw.decode("utf-8")
        except (OSError, UnicodeError, ValueError):
            raise CapabilityRefusal("DOCUMENT_SOURCE_UNVERIFIED") from None
        contract = extract_openapi_read(document)
        if contract.documentation_identity != evidence.source_identity:
            raise CapabilityRefusal("DOCUMENT_SOURCE_UNVERIFIED")
        path_value, query = validated_request(contract, entry.inputs)
        review = entry.reviewed_request
        if ((contract.host, path_value, contract.operation_id, contract.scope, query) !=
                (review.host, review.endpoint, review.operation, review.scope, review.query)):
            raise CapabilityRefusal("UNREVIEWED_DESTINATION")
        return SelectedDocument(entry, evidence, document)


def load_fixture_catalog(path: Path) -> DocumentationCatalog:
    """Load reviewed local catalog data; this command never fetches a URL."""
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 16384:
            raise ValueError("catalog path")
        root = path.parent.resolve()
        raw = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_pairs)
        if type(raw) is not list:
            raise ValueError("catalog shape")
        entries = []
        required = {"catalog_id", "description", "terms", "source_identity", "snapshot_file",
                    "snapshot_sha256", "contract_file", "contract_sha256", "inputs",
                    "isolated_sample", "reviewed_request"}
        for item in raw:
            if type(item) is not dict or set(item) != required:
                raise ValueError("entry shape")
            def local_file(name: object) -> Path:
                if type(name) is not str or re.fullmatch(r"generalist_[a-z0-9_]+\.(?:json|txt)", name) is None:
                    raise ValueError("filename")
                return root / name
            entries.append(CatalogEntry(
                item["catalog_id"], item["description"], tuple(item["terms"]),
                item["source_identity"], local_file(item["snapshot_file"]),
                item["snapshot_sha256"], local_file(item["contract_file"]),
                item["contract_sha256"], item["inputs"], item["isolated_sample"],
                ReviewedReadRequest(**item["reviewed_request"])))
        return DocumentationCatalog(tuple(entries))
    except (OSError, UnicodeError, TypeError, ValueError, KeyError):
        raise CapabilityRefusal("CATALOG_INVALID") from None


class ProposalSource(Protocol):
    """A bounded proposal source; implementation cannot issue grants."""

    async def propose(self, mission: str, catalog: DocumentationCatalog) -> PlanningProposal: ...


class FixtureProposalSource:
    """Deterministic lexical fixture, not a semantic model or web search."""

    async def propose(self, mission: str, catalog: DocumentationCatalog) -> PlanningProposal:
        """Select the unique catalog match and expose its documented operation."""
        entry = catalog.select(mission)
        selected = catalog.load(entry)
        contract = extract_openapi_read(selected.document)
        return PlanningProposal(catalog_id=entry.catalog_id, operation_id=contract.operation_id,
                                outcome=f"Retrieve bounded {contract.shape} metadata for the mission",
                                completion_criteria=["Validated response under the pinned schema",
                                                     "Durable receipt after independently authorized execution"],
                                uncertainty="Intent selection is lexical fixture evidence only")


class OllamaProposalSource:
    """Opt-in local proposal adapter; output is candidate data, not authority."""

    def __init__(self, client: object, model: str) -> None:
        self.client, self.model = client, model

    async def propose(self, mission: str, catalog: DocumentationCatalog) -> PlanningProposal:
        """Bound one existing Ollama chat call and strictly parse its JSON text."""
        from core.llm_client import ChatMessage
        if not 1 <= len(mission) <= 240 or len(self.model) > 80:
            raise CapabilityRefusal("MISSION_INVALID")
        candidates = [{"catalog_id": e.catalog_id, "description": e.description,
                       "terms": list(e.terms)} for e in catalog.entries]
        instruction = ("Choose one relevant catalog_id for the user's mission and describe the "
                       "observable outcome. Return ONLY JSON with catalog_id, operation_id, outcome, "
                       "completion_criteria (1-4 short strings), uncertainty. You have no tool or "
                       "permission authority. If uncertain, return an unsupported catalog_id.")
        try:
            response = await asyncio.wait_for(self.client.chat(
                [ChatMessage(role="system", content=instruction),
                 ChatMessage(role="user", content=json.dumps({"mission": mission,
                                                               "catalog": candidates}))],
                model=self.model, tools=None, stream=False,
                options={"temperature": 0, "num_predict": 320}), timeout=20)
            content = response.message.content
            if (not response.done or response.message.tool_calls or type(content) is not str
                    or len(content.encode()) > 2048):
                raise ValueError("unusable response")
            raw = json.loads(content, object_pairs_hook=_unique_pairs)
            return PlanningProposal.model_validate(raw)
        except (TimeoutError, ValueError, ValidationError, httpx.HTTPError, OSError):
            raise CapabilityRefusal("MODEL_PROPOSAL_INVALID") from None


class MissionCapabilityPlanner:
    """Persist planning evidence, then delegate validation to existing engine."""

    def __init__(self, store: object, project_id: str, catalog: DocumentationCatalog,
                 source: ProposalSource) -> None:
        if project_id == "default" or getattr(store.project, "id", None) != project_id:
            raise CapabilityRefusal("PROJECT_MISMATCH")
        self.store, self.project_id, self.catalog, self.source = store, project_id, catalog, source

    async def receive(self, objective: str) -> MissionRecord:
        """Persist the mission before any proposal or documentation inspection."""
        if type(objective) is not str or not 8 <= len(objective) <= 240:
            raise CapabilityRefusal("MISSION_INVALID")
        mission = MissionRecord(project_id=self.project_id, objective=objective,
                                pending_steps=["plan_capability", "await_authorization"])
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
        """Bounded one-step plan; an interrupted checkpoint resumes without replay."""
        mission, revision = await self._load(mission_id)
        if mission.state in {MissionState.PENDING, MissionState.READY, MissionState.COMPLETE,
                             MissionState.ATTEMPTED, MissionState.BLOCKED}:
            return mission
        if mission.state is not MissionState.RECEIVED:
            raise CapabilityRefusal("MISSION_STATE_INVALID")
        try:
            if "catalog_id" in mission.checkpoint:
                entry = self.catalog.get(str(mission.checkpoint["catalog_id"]))
            else:
                proposal = await self.source.propose(mission.objective, self.catalog)
                if type(proposal) is not PlanningProposal:
                    raise CapabilityRefusal("MODEL_PROPOSAL_INVALID")
                entry = self.catalog.get(proposal.catalog_id)
                # A model may nominate a reviewed source, but cannot override
                # the independent relevance gate or select an unrelated host.
                if self.catalog.select(mission.objective).catalog_id != entry.catalog_id:
                    raise CapabilityRefusal("PROPOSED_SOURCE_UNSUPPORTED")
                selected = self.catalog.load(entry)
                contract = extract_openapi_read(selected.document)
                if proposal.operation_id != contract.operation_id:
                    raise CapabilityRefusal("PROPOSED_OPERATION_UNSUPPORTED")
                if now_tick > contract.valid_until_tick:
                    raise CapabilityRefusal("DOCUMENT_STALE")
                mission.required_capabilities = [contract.capability_id]
                mission.capability_gaps = [contract.capability_id]
                mission.state_history.extend(["PLAN_CREATED", "CAPABILITY_INVENTORY_CHECKED",
                                              MissionState.GAP.value, "DOCUMENTATION_SELECTED",
                                              "DOCUMENTATION_INSPECTED", "OPERATION_PROPOSED"])
                mission.checkpoint.update({"catalog_id": entry.catalog_id,
                                           "source_identity": selected.evidence.source_identity,
                                           "source_digest": selected.evidence.content_digest,
                                           "source_replayed_at": _now().isoformat(),
                                           "contract_digest": contract.documentation_digest,
                                           "planning_outcome": proposal.outcome,
                                           "planning_uncertainty": proposal.uncertainty,
                                           "completion_criteria": " | ".join(proposal.completion_criteria)})
                mission.updated_at = _now()
                if not await self.store.save_mission(mission_id, revision, mission.state.value,
                                                     mission.model_dump_json()):
                    raise CapabilityRefusal("MISSION_CONCURRENT_CHANGE")
            selected = self.catalog.load(entry)
            contract = extract_openapi_read(selected.document)
            if (mission.checkpoint.get("source_digest") != selected.evidence.content_digest or
                    mission.checkpoint.get("contract_digest") != contract.documentation_digest or
                    mission.required_capabilities != [contract.capability_id]):
                raise CapabilityRefusal("DOCUMENT_CHANGED")
            driver = DeclarativeHttpReadDriver(entry.reviewed_request, entry.inputs,
                                               transport=httpx.MockTransport(
                                                   lambda _: httpx.Response(200, json=entry.isolated_sample)),
                                               documentation=selected.evidence)
            broker = ProjectApprovalBroker(self.store, reviewed_request=entry.reviewed_request)
            engine = GeneralistMissionOrchestrator(self.store, self.project_id, driver,
                                                   approval_broker=broker)
            result = await engine.assess(mission_id, selected.document, entry.isolated_sample,
                                         now_tick=now_tick)
            return result
        except CapabilityRefusal as exc:
            if exc.code == "MISSION_CONCURRENT_CHANGE":
                raise
            current, current_revision = await self._load(mission_id)
            if current.state is MissionState.RECEIVED:
                current.state = MissionState.BLOCKED
                current.state_history.append(MissionState.BLOCKED.value)
                current.failure_reasons.append(exc.code)
                current.checkpoint["next_action"] = "review_source_or_proposal"
                current.updated_at = _now()
                if not await self.store.save_mission(mission_id, current_revision,
                                                     current.state.value,
                                                     current.model_dump_json()):
                    raise CapabilityRefusal("MISSION_CONCURRENT_CHANGE") from None
                return current
            raise
