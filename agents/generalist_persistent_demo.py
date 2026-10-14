"""Bounded opt-in two-service demo using disposable morgoth_test Projects.

Usage: MORGOTH_TEST_POSTGRES_URL=<disposable URL> python -m agents.generalist_persistent_demo
No production Project, external service, model, scheduler or approval API is used.
"""

from __future__ import annotations

import asyncio
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from uuid import UUID, uuid4

import asyncpg

from agents.generalist_capability import parse_contract
from agents.generalist_mission_engine import GeneralistFixtureEngine
from agents.generalist_mission_store import ProjectMissionStore
from core.project import current_namespace
from memory.persistent import PersistentMemory


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = {
    "task": ("generalist_task_contract_v2.json",
             [{"project_name": "Beta", "task_state": "blocked"},
              {"project_name": "Alpha", "task_state": "open"}]),
    "inventory": ("generalist_inventory_contract_v2.json",
                  [{"sku": "B2", "stock_level": "low"},
                   {"sku": "A1", "stock_level": "ready"}]),
}
SCOPES = frozenset({"fixture.read", "fixture.write", "fixture.report"})


def _fixture(name: str) -> tuple[str, list[dict[str, str]]]:
    filename, rows = FIXTURES[name]
    return (ROOT / "tests" / "fixtures" / filename).read_text(encoding="utf-8"), rows


async def _child(phase: str, name: str, mission_id: str | None) -> dict[str, object]:
    """Open a fresh process-local pool for exactly one fixture checkpoint."""
    dsn = os.environ["MORGOTH_TEST_POSTGRES_URL"]
    preflight = await asyncpg.connect(dsn)
    try:
        if await preflight.fetchval("SELECT current_database()") != "morgoth_test":
            raise RuntimeError("DISPOSABLE_TEST_DATABASE_REQUIRED")
    finally:
        await preflight.close()
    project = current_namespace()
    memory = PersistentMemory(SimpleNamespace(postgres_url=dsn))
    await memory.initialize()
    try:
        store = ProjectMissionStore(memory, project)
        await store.initialize_optional()
        engine = GeneralistFixtureEngine(store, project.id)
        document, rows = _fixture(name)
        contract, _ = parse_contract(document, now_tick=1)
        if phase == "prepare":
            mission = await engine.receive(f"Organize synthetic {name} and report", contract.service_id)
            pending = await engine.assess(mission.mission_id, document, now_tick=1, records=rows)
            return {"mission_id": str(mission.mission_id), "state": pending.state.value}
        if phase == "complete" and mission_id is not None:
            ident = UUID(mission_id)
            await engine.approve_test_fixture(ident, explicit_test_authorization=True)
            report = await engine.execute(ident, document, rows, scopes=SCOPES, now_tick=1)
            repeated = await engine.execute(ident, document, rows, scopes=SCOPES, now_tick=1)
            second = await engine.execute(ident, document, rows, scopes=SCOPES, now_tick=2, tick=2)
            state = (await engine._mission(ident))[0].state.value
            return {"state": state, "report_sha256": sha256(report.encode()).hexdigest(),
                    "duplicate_suppressed": repeated == report, "periodic_tick": second == report}
        if phase == "reuse":
            mission = await engine.receive(f"Reuse synthetic {name} capability", contract.service_id)
            selected = await engine.assess(mission.mission_id, document, now_tick=2, records=rows)
            report = await engine.execute(mission.mission_id, document, rows, scopes=SCOPES, now_tick=2)
            return {"state": selected.state.value, "reuse": True,
                    "report_sha256": sha256(report.encode()).hexdigest()}
        raise ValueError("invalid fixture phase")
    finally:
        await memory.close()


async def _spawn(phase: str, name: str, project_id: str, home: Path,
                 dsn: str, mission_id: str | None = None) -> dict[str, object]:
    """Run one phase in a separate interpreter, bounded and without retries."""
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
           "HOME": os.environ.get("HOME", "/tmp"), "LANG": "C.UTF-8",
           "MORGOTH_HOME": str(home), "MORGOTH_PROJECT": project_id,
           "MORGOTH_TEST_POSTGRES_URL": dsn}
    argv = [sys.executable, "-m", "agents.generalist_persistent_demo", "--child",
            phase, name, mission_id or "-"]
    proc = await asyncio.create_subprocess_exec(*argv, cwd=ROOT, env=env,
                                                 stdout=asyncio.subprocess.PIPE,
                                                 stderr=asyncio.subprocess.DEVNULL)
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError("FIXTURE_CHILD_TIMEOUT") from None
    if proc.returncode != 0 or len(stdout) > 2048:
        raise RuntimeError("FIXTURE_CHILD_FAILED")
    return json.loads(stdout)


async def run_demo() -> dict[str, object]:
    """Demonstrate two Projects and process-boundary recovery in morgoth_test."""
    dsn = os.environ.get("MORGOTH_TEST_POSTGRES_URL")
    if not dsn:
        raise RuntimeError("DISPOSABLE_TEST_DATABASE_REQUIRED")
    with tempfile.TemporaryDirectory(prefix="morgoth_generalist_") as tmp:
        home = Path(tmp)
        projects: list[str] = []
        for name in FIXTURES:
            project_id = f"fixture_{name}_{uuid4().hex[:8]}"
            projects.append(project_id)
            root = home / "projects" / project_id
            root.mkdir(parents=True)
            manifest = {"id": project_id, "name": "Disposable fixture", "domain": "crypto",
                        "postgres_schema": project_id, "chroma_prefix": project_id + "_",
                        "vault_dir": str(home / "vault" / project_id),
                        "runtime_dir": str(home / "runtime" / project_id)}
            (root / "project.yaml").write_text(json.dumps(manifest), encoding="utf-8")
        timeline: list[dict[str, object]] = []
        try:
            for name, project_id in zip(FIXTURES, projects, strict=True):
                pending = await _spawn("prepare", name, project_id, home, dsn)
                timeline.append({"service": name, "state": pending["state"],
                                 "process": "prepare"})
                complete = await _spawn("complete", name, project_id, home, dsn,
                                        str(pending["mission_id"]))
                timeline.append({"service": name, "state": complete["state"],
                                 "process": "fresh_resume", "duplicate_suppressed": complete["duplicate_suppressed"],
                                 "periodic_tick": complete["periodic_tick"]})
                reused = await _spawn("reuse", name, project_id, home, dsn)
                timeline.append({"service": name, "state": reused["state"],
                                 "process": "fresh_reuse"})
        finally:
            conn = await asyncpg.connect(dsn)
            try:
                if await conn.fetchval("SELECT current_database()") != "morgoth_test":
                    raise RuntimeError("DISPOSABLE_TEST_DATABASE_REQUIRED")
                for project_id in projects:
                    await conn.execute(f'DROP SCHEMA IF EXISTS "{project_id}" CASCADE')
            finally:
                await conn.close()
        return {"fixture_only": True, "production_approval": False,
                "cross_process": True, "timeline": timeline}


def main() -> int:
    """Run the bounded disposable fixture command or one internal phase."""
    if len(sys.argv) == 5 and sys.argv[1] == "--child":
        result = asyncio.run(_child(sys.argv[2], sys.argv[3],
                                    None if sys.argv[4] == "-" else sys.argv[4]))
    elif len(sys.argv) == 1:
        result = asyncio.run(run_demo())
    else:
        raise SystemExit("FIXTURE_ARGUMENTS_INVALID")
    sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
