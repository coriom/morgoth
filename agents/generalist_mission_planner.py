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
from typing import Annotated, Protocol
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from agents.generalist_approval_broker import ProjectApprovalBroker, ReviewedReadRequest
from agents.generalist_capability import CapabilityRefusal, _unique_pairs
from agents.generalist_documentation import DocumentationEvidence, reviewed_offline_snapshot
from agents.generalist_generic_read import LearnedReadContract, extract_openapi_read, validated_request
from agents.generalist_mission_engine import (
    CapabilityRecord, CapabilityState, MissionRecord, MissionState, _now,
)
from agents.generalist_mission_store import ProjectMissionStore
from agents.generalist_orchestrator import DeclarativeHttpReadDriver, GeneralistMissionOrchestrator


_WORD = re.compile(r"[a-z][a-z0-9]{2,}")
_IDENTIFIER = re.compile(r"[a-z][a-z0-9_]{0,31}\Z")


class DuplicateProposalKeyError(ValueError):
    """Private JSON parser signal; never contains the duplicated key."""


def _proposal_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Reject duplicate keys at every JSON object depth without echoing data."""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateProposalKeyError()
        result[key] = value
    return result


class PlanningProposal(BaseModel):
    """Untrusted model or fixture suggestion, never an executable grant."""

    model_config = ConfigDict(extra="forbid", strict=True)
    catalog_id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")
    operation_id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")
    outcome: str = Field(min_length=8, max_length=160)
    completion_criteria: list[Annotated[str, Field(min_length=8, max_length=120)]] = Field(
        min_length=1, max_length=4)
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


@dataclass(frozen=True)
class MissionRequirements:
    """Trusted outcome constraints supplied separately from a model proposal."""

    objective: str
    shape: str
    required_result_fields: tuple[str, ...]

    def check(self, objective: str, contract: LearnedReadContract) -> None:
        """Reject a proposed operation unable to produce required fields."""
        if (objective != self.objective or type(self.objective) is not str
                or self.shape not in {"detail", "collection"}
                or type(self.required_result_fields) is not tuple
                or not 1 <= len(self.required_result_fields) <= 6
                or any(type(field) is not str or _IDENTIFIER.fullmatch(field) is None
                       for field in self.required_result_fields)):
            raise CapabilityRefusal("MISSION_REQUIREMENTS_INVALID")
        schema = contract.response_schema
        result_schema = schema["items"] if contract.shape == "collection" else schema
        if (contract.shape != self.shape
                or not set(self.required_result_fields).issubset(result_schema["required"])):
            raise CapabilityRefusal("MISSION_CAPABILITY_INCOMPATIBLE")


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

    def operation_choices(self) -> list[dict[str, object]]:
        """Expose only digest-verified operation metadata, never document prose."""
        choices = []
        for entry in self.entries:
            selected = self.load(entry)
            contract = extract_openapi_read(selected.document)
            schema = contract.response_schema
            fields = schema["items"]["properties"] if contract.shape == "collection" else schema["properties"]
            choices.append({"catalog_id": entry.catalog_id, "description": entry.description,
                            "operation_id": contract.operation_id, "shape": contract.shape,
                            "result_fields": sorted(fields)})
        return choices


def validate_planning_candidate(catalog: DocumentationCatalog, proposal: PlanningProposal,
                                requirements: MissionRequirements,
                                objective: str,
                                evidence: ProposalEvidence | None = None) -> SelectedDocument:
    """Check an untrusted choice against reviewed identity and outcome schema."""
    try:
        entry = catalog.get(proposal.catalog_id)
    except CapabilityRefusal:
        raise CapabilityRefusal("UNKNOWN_CATALOG_OR_OPERATION") from None
    try:
        selected = catalog.load(entry)
        contract = extract_openapi_read(selected.document)
    except CapabilityRefusal:
        raise CapabilityRefusal("DOCUMENTATION_VALIDATION_FAILED") from None
    if proposal.operation_id != contract.operation_id:
        raise CapabilityRefusal("UNKNOWN_CATALOG_OR_OPERATION")
    if evidence is not None:
        evidence.catalog_operation_validated = True
    requirements.check(objective, contract)
    if evidence is not None:
        evidence.mission_compatible = True
    return selected


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


@dataclass
class ProposalEvidence:
    """Non-sensitive, single-request diagnostic facts; never stores response text."""

    inference_attempted: bool = False
    response_received: bool = False
    proposal_validated: bool = False
    json_valid: bool = False
    catalog_operation_validated: bool = False
    mission_compatible: bool = False


class OllamaProposalSource:
    """Opt-in local proposal adapter; output is candidate data, not authority."""

    def __init__(self, client: object, model: str,
                 evidence: ProposalEvidence | None = None) -> None:
        self.client, self.model, self.evidence = client, model, evidence

    async def propose(self, mission: str, catalog: DocumentationCatalog) -> PlanningProposal:
        """Request native schema output and independently validate one proposal."""
        from core.llm_client import ChatMessage
        if not 1 <= len(mission) <= 240 or len(self.model) > 80:
            raise CapabilityRefusal("MISSION_INVALID")
        candidates = catalog.operation_choices()
        instruction = ("Choose one relevant catalog_id for the user's mission and describe the "
                       "observable outcome. Return ONLY JSON with catalog_id, operation_id, outcome, "
                       "completion_criteria (1-4 short strings), uncertainty. You have no tool or "
                       "permission authority. If uncertain, return an unsupported catalog_id.")
        try:
            if self.evidence is not None:
                self.evidence.inference_attempted = True
            response = await asyncio.wait_for(self.client.chat(
                [ChatMessage(role="system", content=instruction),
                 ChatMessage(role="user", content=json.dumps({"mission": mission,
                                                               "catalog": candidates}))],
                model=self.model, tools=None, stream=False,
                options={"temperature": 0, "num_predict": 640},
                format=PlanningProposal.model_json_schema()), timeout=90)
        except (TimeoutError, httpx.TimeoutException):
            raise CapabilityRefusal("INFERENCE_TIMEOUT") from None
        except (httpx.ConnectError, httpx.NetworkError):
            raise CapabilityRefusal("OLLAMA_UNREACHABLE") from None
        except httpx.HTTPStatusError as exc:
            # Only the known Ollama format-contract rejection is attributable.
            # Read the bounded body privately; never include it in an exception.
            body = exc.response.content
            try:
                detail = json.loads(body) if len(body) <= 256 else None
            except (ValueError, UnicodeError):
                detail = None
            if (exc.response.status_code == 400 and type(detail) is dict
                    and detail == {"error": 'invalid format: expected "json" or a JSON schema'}):
                raise CapabilityRefusal("MODEL_FORMAT_UNSUPPORTED") from None
            raise CapabilityRefusal("MODEL_SERVICE_ERROR") from None
        except (httpx.HTTPError, OSError, ValueError, ValidationError, TypeError, KeyError):
            raise CapabilityRefusal("MODEL_SERVICE_ERROR") from None
        if self.evidence is not None:
            self.evidence.response_received = True
        try:
            content = response.message.content
            if response.message.tool_calls:
                raise CapabilityRefusal("FORBIDDEN_TOOL_ACTIVITY")
            if getattr(response, "done_reason", None) == "length":
                raise CapabilityRefusal("GENERATION_BUDGET_EXHAUSTED")
            if (response.done is not True or getattr(response, "done_reason", None) not in {None, "stop"}
                    or type(content) is not str
                    or not content.strip() or len(content.encode()) > 2048):
                raise CapabilityRefusal("RESPONSE_MISSING_OR_INCOMPLETE")
        except (AttributeError, TypeError):
            raise CapabilityRefusal("RESPONSE_MISSING_OR_INCOMPLETE") from None
        try:
            raw = json.loads(content, object_pairs_hook=_proposal_pairs)
        except DuplicateProposalKeyError:
            raise CapabilityRefusal("DUPLICATE_JSON_KEY") from None
        except (ValueError, UnicodeError):
            raise CapabilityRefusal("INVALID_JSON") from None
        if self.evidence is not None:
            self.evidence.json_valid = True
        try:
            proposal = PlanningProposal.model_validate(raw)
        except ValidationError:
            raise CapabilityRefusal("INVALID_PROPOSAL_SCHEMA") from None
        if self.evidence is not None:
            self.evidence.proposal_validated = True
        return proposal


class MissionCapabilityPlanner:
    """Persist planning evidence, then delegate validation to existing engine."""

    def __init__(self, store: ProjectMissionStore, project_id: str, catalog: DocumentationCatalog,
                 source: ProposalSource, requirements: MissionRequirements | None = None) -> None:
        if project_id == "default" or getattr(store.project, "id", None) != project_id:
            raise CapabilityRefusal("PROJECT_MISMATCH")
        self.store, self.project_id, self.catalog, self.source = store, project_id, catalog, source
        self.requirements = requirements

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
                if self.requirements is None and type(self.source) is not FixtureProposalSource:
                    raise CapabilityRefusal("MISSION_REQUIREMENTS_MISSING")
                if self.requirements is not None and self.requirements.objective != mission.objective:
                    raise CapabilityRefusal("MISSION_REQUIREMENTS_INVALID")
                inventory = await self.store.list_capability_identities()
                proposal = await self.source.propose(mission.objective, self.catalog)
                if type(proposal) is not PlanningProposal:
                    raise CapabilityRefusal("MODEL_PROPOSAL_INVALID")
                if self.requirements is not None:
                    selected = validate_planning_candidate(
                        self.catalog, proposal, self.requirements, mission.objective)
                    entry = selected.entry
                else:
                    # Deterministic fixture-only planning retains its lexical
                    # selector. A model requires independent outcome constraints.
                    entry = self.catalog.get(proposal.catalog_id)
                    if self.catalog.select(mission.objective).catalog_id != entry.catalog_id:
                        raise CapabilityRefusal("PROPOSED_SOURCE_UNSUPPORTED")
                    selected = self.catalog.load(entry)
                contract = extract_openapi_read(selected.document)
                if proposal.operation_id != contract.operation_id:
                    raise CapabilityRefusal("UNKNOWN_CATALOG_OR_OPERATION")
                if now_tick > contract.valid_until_tick:
                    raise CapabilityRefusal("DOCUMENT_STALE")
                preview_driver = DeclarativeHttpReadDriver(
                    entry.reviewed_request, entry.inputs,
                    transport=httpx.MockTransport(
                        lambda _: httpx.Response(200, json=entry.isolated_sample)),
                    documentation=selected.evidence)
                descriptor = preview_driver.inspect(selected.document, now_tick=now_tick)
                identity = (descriptor.capability_id, descriptor.version, descriptor.digest)
                existing_raw = await self.store.get_capability(*identity)
                if existing_raw is not None:
                    try:
                        existing = CapabilityRecord.model_validate_json(existing_raw)
                    except ValueError:
                        raise CapabilityRefusal("CAPABILITY_RECORD_INVALID") from None
                    if existing.project_id != self.project_id:
                        raise CapabilityRefusal("PROJECT_MISMATCH")
                    if existing.state in {CapabilityState.STALE, CapabilityState.REVOKED}:
                        raise CapabilityRefusal("CAPABILITY_REVOKED_OR_STALE")
                    reusable = existing.validation_state == "PASSED"
                else:
                    reusable = False
                if existing_raw is not None and identity not in inventory:
                    # An inventory/record race cannot authorize execution.
                    raise CapabilityRefusal("CAPABILITY_INVENTORY_CHANGED")
                mission.required_capabilities = [contract.capability_id]
                mission.capability_gaps = [] if reusable else [contract.capability_id]
                mission.state_history.extend(["PLAN_CREATED", "CAPABILITY_INVENTORY_CHECKED",
                                              "DOCUMENTATION_SELECTED", "DOCUMENTATION_INSPECTED"])
                if not reusable:
                    mission.state_history.append(MissionState.GAP.value)
                mission.state_history.append("OPERATION_PROPOSED")
                mission.checkpoint.update({"catalog_id": entry.catalog_id,
                                           "inventory_count": len(inventory),
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
