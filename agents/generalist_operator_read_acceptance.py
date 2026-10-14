"""One consented public GET using the generic driver and disposable Project SQL.

This command is intentionally disconnected from Brain, Scheduler and production
approval. The sole reviewed destination and candidate live in committed data.
"""

from __future__ import annotations

import asyncio
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from uuid import UUID, uuid4

from agents.generalist_approval_broker import ProjectApprovalBroker, ReviewedReadRequest
from agents.generalist_capability import CapabilityRefusal, _unique_pairs
from agents.generalist_documentation import reviewed_offline_snapshot
from agents.generalist_mission_store import ProjectMissionStore
from agents.generalist_operator_public_read import DisposablePostgres, _open_store
from agents.generalist_orchestrator import DeclarativeHttpReadDriver, GeneralistMissionOrchestrator
from agents.generalist_pinned_transport import PinnedPublicTransport


ROOT = Path(__file__).resolve().parents[1]
BRANCH = "morgoth-generalist-real-transfer-v1"
BASE = "7ecb85ab1998fb8c2b2b84e09a999e6407af4bc4"
FIXTURES = ROOT / "tests" / "fixtures"
POLICY = FIXTURES / "generalist_openlibrary_reviewed_acceptance_v1.json"
_active_transport: PinnedPublicTransport | None = None


def _git(*args: str) -> str:
    try:
        result = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True,
                                stdin=subprocess.DEVNULL, text=True, timeout=15,
                                env={**os.environ, "GIT_TERMINAL_PROMPT": "0",
                                     "GIT_SSH_COMMAND": "ssh -o BatchMode=yes"},
                                check=False)
    except (OSError, subprocess.SubprocessError):
        raise CapabilityRefusal("CHECKOUT_UNVERIFIED") from None
    if result.returncode or len(result.stdout) > 4096:
        raise CapabilityRefusal("CHECKOUT_UNVERIFIED")
    return result.stdout.strip()


def verify_checkout() -> None:
    """Require exact branch, clean tree, base ancestry and live origin equality."""
    head = _git("rev-parse", "HEAD")
    remote = _git("ls-remote", "origin", f"refs/heads/{BRANCH}")
    remote_head = remote.split("\t", 1)[0] if remote else ""
    if (not re.fullmatch(r"[0-9a-f]{40}", head)
            or _git("branch", "--show-current") != BRANCH
            or _git("status", "--porcelain", "--untracked-files=all")
            or _git("merge-base", BASE, "HEAD") != BASE
            or _git("remote", "get-url", "origin") != "git@github.com:coriom/morgoth.git"
            or head != _git("rev-parse", f"origin/{BRANCH}")
            or head != remote_head):
        raise CapabilityRefusal("CHECKOUT_UNVERIFIED")


def _reviewed_inputs() -> tuple[str, ReviewedReadRequest, dict[str, object], object, object]:
    """Load only the committed acceptance policy and digest-checked data."""
    try:
        if POLICY.is_symlink() or not POLICY.is_file() or POLICY.stat().st_size > 4096:
            raise ValueError("policy file")
        raw = json.loads(POLICY.read_text(encoding="utf-8"), object_pairs_hook=_unique_pairs)
        if set(raw) != {"source_identity", "snapshot_file", "snapshot_sha256",
                        "proposal_file", "proposal_sha256", "reviewed_request",
                        "inputs", "isolated_sample"}:
            raise ValueError("policy shape")
        for name in ("snapshot_file", "proposal_file"):
            if not re.fullmatch(r"generalist_[a-z0-9_]+\.(?:json|txt)", raw[name]):
                raise ValueError("policy filename")
        snapshot = reviewed_offline_snapshot(FIXTURES / raw["snapshot_file"],
                                             raw["source_identity"], raw["snapshot_sha256"])
        proposal_file = FIXTURES / raw["proposal_file"]
        if proposal_file.is_symlink() or not proposal_file.is_file():
            raise ValueError("proposal file")
        data = proposal_file.read_bytes()
        if not 1 <= len(data) <= 16384 or sha256(data).hexdigest() != raw["proposal_sha256"]:
            raise ValueError("proposal digest")
        reviewed = ReviewedReadRequest(**raw["reviewed_request"])
        if reviewed.query != {} or type(raw["inputs"]) is not dict:
            raise ValueError("reviewed query")
        return data.decode("utf-8"), reviewed, raw["inputs"], raw["isolated_sample"], snapshot
    except (OSError, UnicodeError, ValueError, KeyError, TypeError):
        raise CapabilityRefusal("REVIEWED_POLICY_INVALID") from None


async def _recover(mission_id: UUID, key: str, dsn: str) -> dict[str, object]:
    memory, store = await _open_store(dsn)
    try:
        raw = await store.get_receipt(mission_id, key)
        if raw is None:
            raise CapabilityRefusal("RECEIPT_RECOVERY_FAILED")
        receipt = json.loads(raw)
        return {"status": receipt["status"], "key": receipt["execution_receipt"],
                "sha256": receipt["evidence"]["response_sha256"]}
    finally:
        await memory.close()


async def _one_read(dsn: str) -> dict[str, object]:
    global _active_transport
    document, reviewed, inputs, sample, source = _reviewed_inputs()
    memory, store = await _open_store(dsn)
    try:
        _active_transport = PinnedPublicTransport(reviewed.host)
        driver = DeclarativeHttpReadDriver(reviewed, inputs, transport=_active_transport,
                                           documentation=source)
        broker = ProjectApprovalBroker(store, reviewed_request=reviewed)
        engine = GeneralistMissionOrchestrator(store, store.project.id, driver,
                                               approval_broker=broker)
        desc = driver.inspect(document, now_tick=1)
        driver.validate(document, sample, now_tick=1)
        mission = await engine.receive("Read one selected public bibliographic work",
                                       desc.capability_id)
        pending = await engine.assess(mission.mission_id, document, sample, now_tick=1)
        if pending.state.value != "PENDING_HUMAN_APPROVAL":
            raise CapabilityRefusal("MISSION_STATE_INVALID")
        grant_id = await broker.approve_reviewed_interactively(mission.mission_id, desc)
        result = await engine.execute(mission.mission_id, document, grant=None,
                                      grant_id=grant_id, now_tick=1)
        if _active_transport.request_count != 1:
            raise CapabilityRefusal("NETWORK_ATTEMPT_COUNT_INVALID")
        evidence = result["evidence"]
        if (result["status"] != "MISSION_COMPLETE"
                or type(evidence.get("result")) is not dict
                or not evidence["result"].get("title")):
            raise CapabilityRefusal("EXTERNAL_RESPONSE_INVALID")
        key = result["execution_receipt"]
        response_hash = evidence["response_sha256"]
        mission_id = mission.mission_id
        reuse = await engine.receive("Reuse compatible public work capability",
                                     desc.capability_id)
        reused = await engine.assess(reuse.mission_id, document, sample, now_tick=1)
        if reused.state.value != "APPROVED_CAPABILITY_SELECTED":
            raise CapabilityRefusal("CAPABILITY_REUSE_FAILED")
    finally:
        await memory.close()
    env = {key: os.environ[key] for key in ("MORGOTH_HOME", "MORGOTH_PROJECT")}
    env.update({"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "MORGOTH_TEST_POSTGRES_URL": dsn})
    child = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "agents.generalist_operator_read_acceptance", "--recover",
        str(mission_id), key, cwd=ROOT, env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        output, _ = await asyncio.wait_for(child.communicate(), timeout=15)
    except TimeoutError:
        child.kill()
        await child.wait()
        raise CapabilityRefusal("RECEIPT_RECOVERY_FAILED") from None
    if child.returncode or len(output) > 1024:
        raise CapabilityRefusal("RECEIPT_RECOVERY_FAILED")
    try:
        recovered = json.loads(output)
        if recovered != {"status": "MISSION_COMPLETE", "key": key, "sha256": response_hash}:
            raise ValueError("receipt")
    except (ValueError, TypeError):
        raise CapabilityRefusal("RECEIPT_RECOVERY_FAILED") from None
    return {"MISSION_STATE": "MISSION_COMPLETE", "CAPABILITY_ORIGIN": "OFFLINE_REVIEWED_SOURCE",
            "DOCUMENTATION_DIGEST_VERIFIED": True, "GENERIC_DRIVER_USED": True,
            "SERVICE_SPECIFIC_EXECUTION_CODE_ADDED": False,
            "APPROVAL_TYPE": "FOREGROUND_LOCAL_OPERATOR_DISPOSABLE",
            "NETWORK_REQUEST_COUNT": 1, "RESULT_SCHEMA_VALID": True,
            "RESULT_SOURCE_VERIFIED": True, "RECEIPT_RECOVERED_FRESH_PROCESS": True,
            "CAPABILITY_REUSE_ELIGIBLE": True, "PRODUCTION_QUALIFIED": False,
            "RESPONSE_SHA256": response_hash, "VERDICT": "LIVE_PUBLIC_READ_CONFIRMED"}


def main() -> int:
    """Run only the explicit operator-approved one-shot or receipt recovery."""
    try:
        if len(sys.argv) == 4 and sys.argv[1] == "--recover":
            result = asyncio.run(_recover(UUID(sys.argv[2]), sys.argv[3],
                                          os.environ["MORGOTH_TEST_POSTGRES_URL"]))
        elif sys.argv == [sys.argv[0], "--one-reviewed-get"]:
            verify_checkout()
            with tempfile.TemporaryDirectory(prefix="morgoth_reviewed_get_") as tmp:
                home = Path(tmp)
                project_id = f"fixture_read_{uuid4().hex[:10]}"
                project = home / "projects" / project_id
                project.mkdir(parents=True)
                (project / "project.yaml").write_text(json.dumps({
                    "id": project_id, "name": "Disposable reviewed GET", "domain": "crypto",
                    "postgres_schema": project_id, "chroma_prefix": project_id + "_",
                    "vault_dir": str(home / "vault" / project_id),
                    "runtime_dir": str(home / "runtime" / project_id),
                }), encoding="utf-8")
                os.environ["MORGOTH_HOME"] = str(home)
                os.environ["MORGOTH_PROJECT"] = project_id
                with DisposablePostgres(home) as dsn:
                    result = asyncio.run(_one_read(dsn))
        else:
            raise CapabilityRefusal("OPERATOR_ARGUMENTS_INVALID")
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception as exc:
        code = exc.code if isinstance(exc, CapabilityRefusal) else "OPERATOR_READ_FAILED"
        count = _active_transport.request_count if _active_transport is not None else 0
        print(json.dumps({"VERDICT": code, "NETWORK_REQUEST_COUNT": count,
                          "PRODUCTION_QUALIFIED": False}, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
