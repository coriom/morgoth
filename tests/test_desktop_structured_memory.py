"""No-inference, no-database-mutation contract checks for Desktop memory reads."""
from datetime import datetime, timezone
import json
from pathlib import Path
import secrets
from uuid import uuid4

import httpx
import pytest

from agents.desktop_structured_memory import (_thesis, _contradiction,
    load_reader_config, MemoryReadUnavailable, install_structured_memory_routes, PREFIX)
from api.management_app import ManagementSecurity, create_management_app
from core.runtime import RuntimeHome
from project_manager import ProjectManager


def test_private_config_and_evidence_projection(tmp_path: Path) -> None:
    socket = tmp_path / "socket"
    socket.mkdir(mode=0o700)
    config = tmp_path / "reader.json"
    config.write_text(json.dumps({"schema_version":1, "socket_dir":str(socket),
                                  "port":55439,"database":"morgoth_test", "role":"desktop_reader"}))
    config.chmod(0o600)
    assert load_reader_config(config).role == "desktop_reader"
    config.chmod(0o644)
    with pytest.raises(MemoryReadUnavailable, match="INVALID_SOURCE_AUTHORITY"):
        load_reader_config(config)
    config.chmod(0o600)
    link = tmp_path / "link.json"
    link.symlink_to(config)
    with pytest.raises(MemoryReadUnavailable, match="INVALID_SOURCE_AUTHORITY"):
        load_reader_config(link)
    identity = uuid4()
    row = {"thesis_id":identity, "subject":"Sujet", "claim":"Affirmation enregistrée",
           "confidence":"medium", "status":"quarantined", "created_at":datetime.now(timezone.utc),
           "code_version":None, "objective_id":None, "quarantine_reason":"source invalide",
           "superseded_by":None, "evidence":json.dumps([{"source":"get_news","detail":"PRIVATE_CONTENT"},
                                                          {"type":"cycle_payload","prompt":"SECRET"}])}
    projected = _thesis(row, "alpha").model_dump(mode="json")
    assert projected["evidence_sources"] == [{"source":"get_news"}]
    assert projected["evidence_count"] == 2 and projected["evidence_omitted"]
    assert "PRIVATE_CONTENT" not in str(projected) and "SECRET" not in str(projected)
    for field, bad in (("status","unknown"),("confidence","certain"),("claim","x"*2049)):
        candidate = dict(row, **{field:bad})
        with pytest.raises(MemoryReadUnavailable, match="INCOMPATIBLE_RECORD"):
            _thesis(candidate, "alpha")
    edge = _contradiction({"contradiction_id":uuid4(),"subject_group":None,
        "thesis_id_a":identity,"thesis_id_b":uuid4(),"detected_at":datetime.now(timezone.utc),
        "resolution":None}, "alpha")
    assert edge.resolution is None


@pytest.mark.asyncio
async def test_no_authority_still_requires_management_auth_and_catalog(tmp_path: Path) -> None:
    home = tmp_path / "home"
    manager = ProjectManager(RuntimeHome(home, home / "projects"))
    manager.create_project("alpha", name="Alpha", domain="crypto")
    token = secrets.token_urlsafe(32)
    app = create_management_app(manager, ManagementSecurity(token, 8765))
    install_structured_memory_routes(app, manager, None)
    path = f"{PREFIX}/projects/alpha/theses"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765") as client:
        assert (await client.get(path)).status_code == 401
        headers = {"X-Morgoth-Management-Token":token}
        blocked = await client.get(path, headers=headers)
        assert blocked.status_code == 503 and blocked.json()["error"]["code"] == "SOURCE_NOT_AUTHORIZED"
        assert (await client.post(path, headers={**headers, "Content-Type":"application/json"})).status_code == 405
