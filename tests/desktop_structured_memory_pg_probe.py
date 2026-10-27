"""Explicit disposable PostgreSQL proof; never connects to an existing cluster."""
from __future__ import annotations

import asyncio
import getpass
import os
from pathlib import Path
import secrets
import subprocess
import tempfile
from uuid import uuid4

import asyncpg
import httpx

from agents.desktop_structured_memory import (PREFIX, StructuredMemoryReader,
    install_structured_memory_routes, load_reader_config)
from api.management_app import ManagementSecurity, create_management_app
from core.runtime import RuntimeHome
from project_manager import ProjectManager

BIN = Path("/usr/lib/postgresql/16/bin")


def run(*args: str) -> None:
    subprocess.run(args, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)


async def prove(socket: Path, root: Path) -> None:
    home = root / "management"
    manager = ProjectManager(RuntimeHome(home, home / "projects"))
    manager.create_project("alpha", name="Alpha", domain="crypto")
    manager.create_project("beta", name="Beta", domain="crypto")
    projects = {p.id:p for p in manager._catalog()}
    admin = await asyncpg.connect(host=str(socket), port=55439, database="morgoth_test", user=getpass.getuser())
    ids = [uuid4() for _ in range(4)]
    try:
        for project in (projects["alpha"], projects["beta"]):
            schema = project.postgres_schema
            await admin.execute(f'CREATE SCHEMA "{schema}"')
            await admin.execute(f'''CREATE TABLE "{schema}".theses (
                thesis_id UUID PRIMARY KEY, subject TEXT, claim TEXT, confidence TEXT,
                evidence JSONB, status TEXT, objective_id TEXT, created_at TIMESTAMPTZ,
                canonical_subject TEXT, quarantine_reason TEXT, superseded_by UUID,
                code_version TEXT)''')
            await admin.execute(f'''CREATE TABLE "{schema}".contradictions (
                contradiction_id UUID PRIMARY KEY, subject_group TEXT, thesis_id_a UUID,
                thesis_id_b UUID, detected_at TIMESTAMPTZ, resolution TEXT)''')
        a = projects["alpha"].postgres_schema
        b = projects["beta"].postgres_schema
        await admin.execute("CREATE ROLE desktop_memory_reader LOGIN")
        await admin.execute(f'GRANT USAGE ON SCHEMA "{a}" TO desktop_memory_reader')
        await admin.execute(f'GRANT SELECT ON "{a}".theses, "{a}".contradictions TO desktop_memory_reader')
        states = ["active", "contradicted", "quarantined", "superseded"]
        for index, identity in enumerate(ids):
            await admin.execute(f'INSERT INTO "{a}".theses VALUES ($1,$2,$3,$4,$5::jsonb,$6,NULL,now(),NULL,$7,$8,$9)',
                identity, f"Sujet {index}", f"Affirmation {index}", "medium",
                '[{"source":"get_news","detail":"omitted private evidence"}]' if index else '[]',
                states[index], "à revoir" if index == 2 else None,
                ids[0] if index == 3 else None, "fixture-v1")
        await admin.execute(f'INSERT INTO "{b}".theses VALUES ($1,$2,$3,$4,$5::jsonb,$6,NULL,now(),NULL,NULL,NULL,NULL)',
            uuid4(), "PRIVATE BETA", "foreign claim", "low", '[]', "active")
        for index, resolution in enumerate((None, "historical resolution")):
            await admin.execute(f'INSERT INTO "{a}".contradictions VALUES ($1,$2,$3,$4,now(),$5)',
                uuid4(), "Sujet partagé", ids[index], ids[index + 1], resolution)
    finally:
        await admin.close()
    config = home / "memory-reader.json"
    config.write_text(__import__("json").dumps({"schema_version":1,"socket_dir":str(socket),"port":55439,
        "database":"morgoth_test","role":"desktop_memory_reader"}))
    config.chmod(0o600)
    token = secrets.token_urlsafe(32)
    app = create_management_app(manager, ManagementSecurity(token, 8765))
    install_structured_memory_routes(app, manager, StructuredMemoryReader(load_reader_config(config)))
    headers = {"X-Morgoth-Management-Token": token}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765") as client:
        path = f"{PREFIX}/projects/alpha"
        assert (await client.get(path + "/theses")).status_code == 401
        page = await client.get(path + "/theses?limit=2", headers=headers)
        assert page.status_code == 200, page.text
        assert len(page.json()["theses"]) == 2 and page.json()["next_offset"] == 2
        all_rows = (await client.get(path + "/theses", headers=headers)).json()["theses"]
        assert len(all_rows) == 4 and {row["status"] for row in all_rows} == set(states)
        assert "omitted private evidence" not in str(all_rows)
        quarantine = await client.get(path + "/theses?status=quarantined", headers=headers)
        assert quarantine.status_code == 200, (quarantine.status_code, quarantine.json().get("error",{}).get("code"))
        assert quarantine.json()["theses"][0]["quarantine_reason"] == "à revoir"
        assert (await client.get(path + f"/theses/{ids[3]}", headers=headers)).json()["superseded_by"] == str(ids[0])
        contradictions = (await client.get(path + "/contradictions", headers=headers)).json()["contradictions"]
        assert len(contradictions) == 2 and {c["resolution"] for c in contradictions} == {None, "historical resolution"}
        assert (await client.get(path + f"/contradictions/{contradictions[0]['contradiction_id']}", headers=headers)).status_code == 200
        beta = (await client.get(f"{PREFIX}/projects/beta/theses", headers=headers)).json()["error"]["code"]
        assert beta == "INVALID_SOURCE_AUTHORITY", beta
        assert (await client.get(f"{PREFIX}/projects/beta/theses/{ids[0]}", headers=headers)).status_code != 200
        assert (await client.get(path + f"/theses/{uuid4()}", headers=headers)).status_code == 404
        assert (await client.get(path + "/theses?limit=21", headers=headers)).status_code == 422
        assert (await client.get(path + "/theses?status=forged", headers=headers)).status_code == 422
        admin = await asyncpg.connect(host=str(socket), port=55439, database="morgoth_test", user=getpass.getuser())
        try:
            await admin.execute(f'UPDATE "{a}".theses SET claim=$1 WHERE thesis_id=$2', "x"*2049, ids[0])
            incompatible = await client.get(path + f"/theses/{ids[0]}", headers=headers)
            assert incompatible.status_code == 409 and incompatible.json()["error"]["code"] == "INCOMPATIBLE_RECORD"
            await admin.execute(f'UPDATE "{a}".theses SET claim=$1 WHERE thesis_id=$2', "Affirmation 0", ids[0])
            await admin.execute(f'GRANT UPDATE ON "{a}".theses TO desktop_memory_reader')
            denied = await client.get(path + "/theses", headers=headers)
            assert denied.status_code == 503 and denied.json()["error"]["code"] == "INVALID_SOURCE_AUTHORITY"
            await admin.execute(f'REVOKE UPDATE ON "{a}".theses FROM desktop_memory_reader')
            await admin.execute(f'ALTER TABLE "{a}".theses DROP COLUMN code_version')
            drift = await client.get(path + "/theses", headers=headers)
            assert drift.status_code == 503 and drift.json()["error"]["code"] == "SOURCE_UNINITIALIZED_OR_INCOMPATIBLE"
        finally:
            await admin.close()
    print("D9_DISPOSABLE_POSTGRES_AUTH_READ_PASS=true RECORDS=4 CONTRADICTIONS=2")


def main() -> None:
    if not BIN.joinpath("initdb").is_file() or os.geteuid() == 0:
        raise SystemExit("D9_POSTGRES_PREREQUISITE_UNAVAILABLE")
    with tempfile.TemporaryDirectory(prefix="morgoth-d9-pg-") as name:
        root = Path(name)
        socket = root / "socket"
        socket.mkdir(mode=0o700)
        data = root / "data"
        run(str(BIN / "initdb"), "-D", str(data), "--auth-local=trust", "--auth-host=reject", "--no-instructions")
        run(str(BIN / "pg_ctl"), "-D", str(data), "-l", str(root / "pg.log"),
            "-o", f"-k {socket} -h '' -p 55439", "start")
        try:
            run(str(BIN / "createdb"), "-h", str(socket), "-p", "55439", "morgoth_test")
            asyncio.run(prove(socket, root))
        finally:
            run(str(BIN / "pg_ctl"), "-D", str(data), "-m", "immediate", "stop")


if __name__ == "__main__":
    main()
