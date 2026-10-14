"""Operator-only one-shot public GitHub read in a self-owned disposable test DB.

Run from an ordinary foreground terminal. No production Project, credential,
service or model is touched. The local PostgreSQL cluster and schema are removed
after a fresh-process receipt recovery check.
"""

from __future__ import annotations

import asyncio
import getpass
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from urllib.parse import quote
from uuid import UUID, uuid4

import asyncpg

from agents.generalist_approval_broker import ProjectApprovalBroker
from agents.generalist_capability import CapabilityRefusal
from agents.generalist_mission_store import ProjectMissionStore
from agents.generalist_orchestrator import GeneralistMissionOrchestrator, PublicIssuesDriver
from core.project import current_namespace
from memory.persistent import PersistentMemory


ROOT = Path(__file__).resolve().parents[1]
BRANCH = "morgoth-generalist-approval-live-read-v1"
BASE = "d6a6d59f696a288eae936ec790b3f1bdaad74c6f"
DOCUMENT = ROOT / "tests/fixtures/generalist_github_issues_contract_v1.json"
MOCKED_ROWS = [{"number": 1, "title": "Synthetic issue", "state": "open",
                "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-02T00:00:00Z"}]


def _git(*args: str) -> str:
    """Read bounded Git identity without exposing arbitrary command output."""
    try:
        result = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True,
                                text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        raise CapabilityRefusal("CHECKOUT_UNVERIFIED") from None
    if result.returncode or len(result.stdout) > 4096:
        raise CapabilityRefusal("CHECKOUT_UNVERIFIED")
    return result.stdout.strip()


def verify_checkout() -> None:
    """Require the pushed, clean feature branch descended from the exact base."""
    if (_git("branch", "--show-current") != BRANCH
            or _git("rev-parse", "HEAD") != _git("rev-parse", f"origin/{BRANCH}")
            or _git("status", "--porcelain", "--untracked-files=all")
            or _git("merge-base", BASE, "HEAD") != BASE
            or _git("remote", "get-url", "origin") != "git@github.com:coriom/morgoth.git"):
        raise CapabilityRefusal("CHECKOUT_UNVERIFIED")


def _run_fixed(argv: list[str], *, timeout: int = 15) -> None:
    """Run an owned local PostgreSQL setup step with private output."""
    try:
        result = subprocess.run(argv, capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        raise CapabilityRefusal("DISPOSABLE_POSTGRES_UNAVAILABLE") from None
    if result.returncode:
        raise CapabilityRefusal("DISPOSABLE_POSTGRES_UNAVAILABLE")


class DisposablePostgres:
    """Private Unix-socket cluster owned by this command, never system PostgreSQL."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.cluster = root / "cluster"
        self.socket = root / "socket"
        self.log = root / "postgres.log"
        self.bin = Path("/usr/lib/postgresql/16/bin")
        self.started = False

    def __enter__(self) -> str:
        """Create one local-only morgoth_test database under a private directory."""
        if not all((self.bin / name).is_file() for name in ("initdb", "pg_ctl", "createdb")):
            raise CapabilityRefusal("DISPOSABLE_POSTGRES_UNAVAILABLE")
        self.socket.mkdir(mode=0o700)
        try:
            _run_fixed([str(self.bin / "initdb"), "-D", str(self.cluster), "-A", "trust", "--no-instructions"])
            options = f"-c listen_addresses='' -c unix_socket_directories={self.socket} -c port=5432"
            _run_fixed([str(self.bin / "pg_ctl"), "-D", str(self.cluster), "-l", str(self.log),
                        "-o", options, "-w", "start"])
            self.started = True
            _run_fixed([str(self.bin / "createdb"), "-h", str(self.socket), "-p", "5432",
                        "-U", getpass.getuser(), "morgoth_test"])
        except BaseException:
            if self.started:
                self.__exit__()
            raise
        return f"postgresql://{getpass.getuser()}@/morgoth_test?host={quote(str(self.socket), safe='')}"

    def __exit__(self, *_args: object) -> None:
        """Stop only this cluster; TemporaryDirectory removes every artifact."""
        if self.started:
            try:
                _run_fixed([str(self.bin / "pg_ctl"), "-D", str(self.cluster), "-m", "immediate",
                            "-w", "stop"])
            finally:
                self.started = False


async def _open_store(dsn: str) -> tuple[PersistentMemory, ProjectMissionStore]:
    """Open only the selected disposable Project schema."""
    preflight = await asyncpg.connect(dsn)
    try:
        if await preflight.fetchval("SELECT current_database()") != "morgoth_test":
            raise CapabilityRefusal("DISPOSABLE_TEST_DATABASE_REQUIRED")
    finally:
        await preflight.close()
    memory = PersistentMemory(SimpleNamespace(postgres_url=dsn))
    await memory.initialize()
    store = ProjectMissionStore(memory, current_namespace())
    await store.initialize_optional()
    return memory, store


async def _recover(mission_id: UUID, key: str, dsn: str) -> dict[str, object]:
    """In a new interpreter, read the committed receipt without another GET."""
    memory, store = await _open_store(dsn)
    try:
        raw = await store.get_receipt(mission_id, key)
        if raw is None:
            raise CapabilityRefusal("RECEIPT_RECOVERY_FAILED")
        result = json.loads(raw)
        return {"state": result["status"],
                "receipt": result["execution_receipt"],
                "response_sha256": result["evidence"]["response_sha256"]}
    finally:
        await memory.close()


async def _one_read(dsn: str) -> dict[str, object]:
    """Validate fixture evidence, ask approval, claim, perform one GET and recover."""
    memory, store = await _open_store(dsn)
    try:
        driver = PublicIssuesDriver()
        broker = ProjectApprovalBroker(store)
        engine = GeneralistMissionOrchestrator(store, store.project.id, driver,
                                               approval_broker=broker)
        document = DOCUMENT.read_text(encoding="utf-8")
        desc = driver.inspect(document, now_tick=1)
        mission = await engine.receive("Public open Issues status", desc.capability_id)
        await engine.assess(mission.mission_id, document, MOCKED_ROWS, now_tick=1)
        grant_id = await broker.approve_interactively(mission.mission_id, desc)
        result = await engine.execute(mission.mission_id, document, grant=None,
                                      grant_id=grant_id, now_tick=1)
        key = result["execution_receipt"]
        expected_hash = result["evidence"]["response_sha256"]
    finally:
        await memory.close()
    env = {"PATH": "/usr/bin:/bin", "HOME": os.environ["HOME"], "LANG": "C.UTF-8",
           "MORGOTH_HOME": os.environ["MORGOTH_HOME"],
           "MORGOTH_PROJECT": os.environ["MORGOTH_PROJECT"],
           "MORGOTH_TEST_POSTGRES_URL": dsn}
    child = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "agents.generalist_operator_public_read", "--recover",
        str(mission.mission_id), key, cwd=ROOT, env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        output, _ = await asyncio.wait_for(child.communicate(), timeout=15)
    except TimeoutError:
        child.kill()
        await child.wait()
        raise CapabilityRefusal("RECEIPT_RECOVERY_FAILED") from None
    if child.returncode or len(output) > 1024:
        raise CapabilityRefusal("RECEIPT_RECOVERY_FAILED")
    recovered = json.loads(output)
    if (recovered["state"] != "MISSION_COMPLETE" or recovered["receipt"] != key
            or recovered["response_sha256"] != expected_hash):
        raise CapabilityRefusal("RECEIPT_RECOVERY_FAILED")
    return {"VERDICT": "PUBLIC_READ_CONFIRMED", "ISSUE_COUNT": result["evidence"]["issue_count"],
            "RESPONSE_SHA256": expected_hash, "RECEIPT_RECOVERED_FRESH_PROCESS": True,
            "NETWORK_REQUEST_COUNT": 1, "PRODUCTION_APPROVAL": False}


def main() -> int:
    """Run only an explicit one-shot command; never print raw external data."""
    try:
        if len(sys.argv) == 4 and sys.argv[1] == "--recover":
            result = asyncio.run(_recover(UUID(sys.argv[2]), sys.argv[3],
                                          os.environ["MORGOTH_TEST_POSTGRES_URL"]))
        elif sys.argv == [sys.argv[0], "--one-public-get"]:
            verify_checkout()
            with tempfile.TemporaryDirectory(prefix="morgoth_approved_read_") as tmp:
                home = Path(tmp)
                project_id = f"fixture_github_{uuid4().hex[:10]}"
                root = home / "projects" / project_id
                root.mkdir(parents=True)
                (root / "project.yaml").write_text(json.dumps({
                    "id": project_id, "name": "Disposable approved public read", "domain": "crypto",
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
        print(json.dumps({"VERDICT": code, "PRODUCTION_APPROVAL": False}, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
