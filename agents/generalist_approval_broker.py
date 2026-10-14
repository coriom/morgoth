"""Disposable Project-bound approval and one-attempt claim for reviewed GETs.

Only the opt-in operator command invokes approval. This is not a production
credential service: same-UID Python with test-DB access is inside the trust
boundary. No Brain, API route or tool router imports this module.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
import json
import os
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from agents.generalist_capability import CapabilityRefusal
from agents.generalist_mission_engine import CapabilityRecord, CapabilityState, MissionRecord, MissionState
from agents.generalist_mission_store import ProjectMissionStore

if TYPE_CHECKING:
    from agents.generalist_orchestrator import CapabilityDescriptor


_HOST = "api.github.com"
_ENDPOINT = "/repos/octocat/Hello-World/issues"
_OPERATION = "list_open_issues"
_SCOPE = "public_issues.read"


@dataclass(frozen=True)
class ReviewedReadRequest:
    """Trusted caller's exact destination; never constructed from documentation."""

    host: str
    endpoint: str
    operation: str
    scope: str
    query: dict[str, object]


def _reviewed_request(desc: CapabilityDescriptor,
                      reviewed: ReviewedReadRequest | None) -> ReviewedReadRequest:
    if reviewed is None:
        query = _fixed_request(desc)
        return ReviewedReadRequest(_HOST, _ENDPOINT, _OPERATION, _SCOPE, query)
    contract = desc.invocation_contract
    if (type(reviewed) is not ReviewedReadRequest or type(reviewed.query) is not dict
            or contract.get("method") != "GET" or contract.get("host") != reviewed.host
            or contract.get("endpoint") != reviewed.endpoint
            or contract.get("operation_id") != reviewed.operation
            or contract.get("scope") != reviewed.scope
            or contract.get("query") != reviewed.query
            or desc.operation != reviewed.operation
            or desc.scopes != frozenset({reviewed.scope})):
        raise CapabilityRefusal("UNREVIEWED_DESTINATION")
    return reviewed


def _fixed_request(desc: CapabilityDescriptor) -> dict[str, object]:
    """Refuse unreviewed authority even if documentation or a grant requests it."""
    contract = desc.invocation_contract
    expected = {"host": _HOST, "method": "GET", "path_template": "/repos/{owner}/{repo}/issues",
                "owner": "octocat", "repo": "Hello-World", "operation_id": _OPERATION,
                "state": "open", "scope": _SCOPE}
    if (any(contract.get(key) != value for key, value in expected.items())
            or type(contract.get("per_page")) is not int
            or not 1 <= contract["per_page"] <= 3
            or desc.operation != _OPERATION or desc.scopes != frozenset({_SCOPE})):
        raise CapabilityRefusal("UNREVIEWED_DESTINATION")
    return {"state": "open", "per_page_max": contract["per_page"]}


class ProjectApprovalBroker:
    """Verify an exact local operator approval and consume it before one GET."""

    def __init__(self, store: ProjectMissionStore, *,
                 reviewed_request: ReviewedReadRequest | None = None) -> None:
        self.store = store
        self.reviewed_request = reviewed_request

    async def _issue_after_consent(self, mission_id: UUID, desc: CapabilityDescriptor,
                                   *, provenance: str, lifetime_seconds: int = 120) -> UUID:
        """Persist an approval only after the trusted caller obtained local consent.

        Private to the operator CLI and disposable SQL tests. Arbitrary Python
        callers with test-DB access are trusted; this is NOT production auth.
        """
        if not provenance.startswith("foreground_operator_uid:") or not provenance[24:].isdigit():
            raise CapabilityRefusal("OPERATOR_APPROVAL_REQUIRED")
        if not 1 <= lifetime_seconds <= 120:
            raise CapabilityRefusal("APPROVAL_EXPIRY_INVALID")
        reviewed = _reviewed_request(desc, self.reviewed_request)
        query = reviewed.query
        pool = self.store._pool()
        grant_id = uuid4()
        async with pool.acquire() as conn:
            async with conn.transaction():
                mission_row = await conn.fetchrow(
                    "SELECT record::text AS record FROM generalist_missions WHERE project_id=$1 AND mission_id=$2 FOR UPDATE",
                    self.store.project.id, mission_id)
                cap_row = await conn.fetchrow(
                    """SELECT record::text AS record FROM generalist_capabilities WHERE project_id=$1
                       AND capability_id=$2 AND version=$3 AND document_digest=$4 FOR UPDATE""",
                    self.store.project.id, desc.capability_id, desc.version, desc.digest)
                if mission_row is None or cap_row is None:
                    raise CapabilityRefusal("APPROVAL_TARGET_MISSING")
                mission = MissionRecord.model_validate_json(mission_row["record"])
                cap = CapabilityRecord.model_validate_json(cap_row["record"])
                if (mission.state not in {MissionState.PENDING, MissionState.READY}
                        or mission.project_id != self.store.project.id
                        or mission.checkpoint.get("digest") != desc.digest
                        or cap.project_id != self.store.project.id
                        or cap.validation_state != "PASSED"
                        or cap.state not in {CapabilityState.PENDING_APPROVAL, CapabilityState.TEST_ONLY_APPROVED}
                        or cap.invocation_contract != desc.invocation_contract):
                    raise CapabilityRefusal("APPROVAL_TARGET_INVALID")
                await conn.execute(
                    """INSERT INTO generalist_approval_grants
                       (grant_id,project_id,mission_id,capability_id,version,document_digest,
                        operation,method,host,endpoint,query_constraints,scopes,
                        approval_provenance,expires_at,max_invocations,max_response_bytes,max_duration_seconds)
                       VALUES ($1,$2,$3,$4,$5,$6,$7,'GET',$8,$9,$10::jsonb,$11,$12,$13,1,131072,8)""",
                    grant_id, self.store.project.id, mission_id, desc.capability_id,
                    desc.version, desc.digest, desc.operation, reviewed.host, reviewed.endpoint,
                    json.dumps(query), sorted(desc.scopes), provenance,
                    datetime.now(timezone.utc) + timedelta(seconds=lifetime_seconds))
        return grant_id

    async def approve_interactively(self, mission_id: UUID, desc: CapabilityDescriptor) -> UUID:
        """Require foreground controlling-terminal consent for one disposable GET."""
        from scripts.codex_local_warning_review import ReviewUnavailable, open_operator_terminal
        _fixed_request(desc)
        try:
            with open_operator_terminal() as tty:
                tty.write("Approve ONE unauthenticated GET of public issues for octocat/Hello-World "
                          "(state=open, per_page<=3) in disposable morgoth_test?\n"
                          "Type APPROVE PUBLIC ISSUES READ to continue: ")
                tty.flush()
                answer = tty.readline(48)
                approved = answer in {"APPROVE PUBLIC ISSUES READ\n",
                                      "APPROVE PUBLIC ISSUES READ\r\n"}
        except ReviewUnavailable:
            raise CapabilityRefusal("INTERACTIVE_OPERATOR_REQUIRED") from None
        if not approved:
            raise CapabilityRefusal("OPERATOR_APPROVAL_DECLINED")
        return await self._issue_after_consent(mission_id, desc,
                                               provenance=f"foreground_operator_uid:{os.geteuid()}")

    async def approve_reviewed_interactively(self, mission_id: UUID,
                                             desc: CapabilityDescriptor) -> UUID:
        """Consent to one exact generic GET in a foreground disposable Project."""
        from scripts.codex_local_warning_review import ReviewUnavailable, open_operator_terminal
        reviewed = _reviewed_request(desc, self.reviewed_request)
        if self.reviewed_request is None or reviewed.query:
            raise CapabilityRefusal("UNREVIEWED_DESTINATION")
        try:
            with open_operator_terminal() as tty:
                tty.write("Disposable morgoth_test only. Approve ONE unauthenticated HTTPS GET:\n"
                          f"https://{reviewed.host}{reviewed.endpoint}\n"
                          "No production grant. Type APPROVE ONE PUBLIC GET to continue: ")
                tty.flush()
                approved = tty.readline(48) in {"APPROVE ONE PUBLIC GET\n",
                                               "APPROVE ONE PUBLIC GET\r\n"}
        except ReviewUnavailable:
            raise CapabilityRefusal("INTERACTIVE_OPERATOR_REQUIRED") from None
        if not approved:
            raise CapabilityRefusal("OPERATOR_APPROVAL_DECLINED")
        return await self._issue_after_consent(
            mission_id, desc, provenance=f"foreground_operator_uid:{os.geteuid()}")

    async def claim(self, grant_id: UUID, mission: MissionRecord, revision: int,
                    desc: CapabilityDescriptor, attempted_record: str) -> None:
        """Atomically spend one grant and checkpoint ATTEMPTED before external I/O."""
        reviewed = _reviewed_request(desc, self.reviewed_request)
        query = reviewed.query
        pool = self.store._pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """SELECT *, query_constraints::text AS query_text FROM generalist_approval_grants WHERE grant_id=$1
                       AND project_id=$2 AND mission_id=$3 FOR UPDATE""",
                    grant_id, self.store.project.id, mission.mission_id)
                if row is None:
                    raise CapabilityRefusal("PERMISSION_REQUIRED")
                if (row["revoked"] or row["used_invocations"] >= row["max_invocations"]
                        or row["expires_at"] <= datetime.now(timezone.utc)
                        or row["approval_provenance"] != f"foreground_operator_uid:{os.geteuid()}"
                        or (row["capability_id"], row["version"], row["document_digest"],
                            row["operation"], row["method"], row["host"], row["endpoint"])
                        != (desc.capability_id, desc.version, desc.digest, desc.operation,
                            "GET", reviewed.host, reviewed.endpoint)
                        or json.loads(row["query_text"]) != query
                        or set(row["scopes"]) != desc.scopes
                        or row["max_response_bytes"] != 131072
                        or row["max_duration_seconds"] != 8):
                    raise CapabilityRefusal("PERMISSION_REQUIRED")
                cap_row = await conn.fetchrow(
                    """SELECT record::text AS record FROM generalist_capabilities WHERE project_id=$1
                       AND capability_id=$2 AND version=$3 AND document_digest=$4 FOR UPDATE""",
                    self.store.project.id, desc.capability_id, desc.version, desc.digest)
                if cap_row is None:
                    raise CapabilityRefusal("CAPABILITY_NOT_FOUND")
                cap = CapabilityRecord.model_validate_json(cap_row["record"])
                if (cap.state in {CapabilityState.REVOKED, CapabilityState.STALE}
                        or cap.validation_state != "PASSED"
                        or cap.invocation_contract != desc.invocation_contract):
                    raise CapabilityRefusal("CAPABILITY_REVOKED_OR_STALE")
                updated = await conn.fetchrow(
                    """UPDATE generalist_missions SET state=$4, record=$5::jsonb,
                       revision=revision+1, updated_at=NOW()
                       WHERE mission_id=$1 AND project_id=$2 AND revision=$3
                         AND state IN ('PENDING_HUMAN_APPROVAL','APPROVED_CAPABILITY_SELECTED')
                       RETURNING revision""",
                    mission.mission_id, self.store.project.id, revision,
                    MissionState.ATTEMPTED.value, attempted_record)
                if updated is None:
                    raise CapabilityRefusal("MISSION_CONCURRENT_CHANGE")
                await conn.execute(
                    "UPDATE generalist_approval_grants SET used_invocations=used_invocations+1 WHERE grant_id=$1",
                    grant_id)

    async def revoke(self, grant_id: UUID) -> None:
        """Revoke an unspent grant in this Project; in-flight claims already won."""
        pool = self.store._pool()
        async with pool.acquire() as conn:
            await conn.execute("UPDATE generalist_approval_grants SET revoked=TRUE WHERE grant_id=$1 AND project_id=$2",
                               grant_id, self.store.project.id)
