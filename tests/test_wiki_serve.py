"""Tests for the READ-ONLY vault serve endpoints.

GET /api/wiki/manifest → page list + compiled_at.
GET /api/wiki/page?path=<rel> → raw markdown for one page.

The path-traversal matrix is the security core here. Escape attempts
share the missing-file 404 response — no oracle.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from api.routes import wiki


def _build_app() -> FastAPI:
    app = FastAPI()
    app.state.persistent_memory = MagicMock()
    app.state.llm_client = MagicMock()
    app.include_router(wiki.router)
    return app


@pytest.fixture
def seeded_vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the endpoints at a tmp vault with a realistic layout."""
    (tmp_path / "_index.md").write_text(
        "# Morgoth Vault\n\nRoot index.\n", encoding="utf-8",
    )
    (tmp_path / "contradictions.md").write_text(
        "# Contradictions\n", encoding="utf-8",
    )
    (tmp_path / "log.md").write_text(
        "# Compilation log\n\n- last run: `2026-07-04T12:00:00+00:00`\n"
        "- theses: 89\n- entities: 20\n",
        encoding="utf-8",
    )
    (tmp_path / "entities").mkdir()
    (tmp_path / "entities" / "btc-short-term-price.md").write_text(
        "# BTC short-term price\n\n## Summary\nThe first heading wins.\n",
        encoding="utf-8",
    )
    (tmp_path / "entities" / "mining-difficulty.md").write_text(
        "no heading here\nfilename should win\n", encoding="utf-8",
    )
    (tmp_path / "system").mkdir()
    (tmp_path / "system" / "_index.md").write_text(
        "# System\n", encoding="utf-8",
    )
    (tmp_path / "system" / "tools").mkdir()
    (tmp_path / "system" / "tools" / "get_bitcoin_onchain.md").write_text(
        "# get_bitcoin_onchain\n", encoding="utf-8",
    )
    # Non-md decoy — must NOT appear in the manifest.
    (tmp_path / "notes.txt").write_text("ignore me", encoding="utf-8")
    (tmp_path / "entities" / "malicious.py").write_text(
        "import os", encoding="utf-8",
    )
    monkeypatch.setattr(wiki, "VAULT_DIR", tmp_path)
    return tmp_path


# ---------- manifest -------------------------------------------------------

def test_manifest_lists_all_md_with_sections(seeded_vault: Path) -> None:
    client = TestClient(_build_app())
    resp = client.get("/api/wiki/manifest")
    assert resp.status_code == 200
    body = resp.json()
    assert body["compiled_at"] == "2026-07-04T12:00:00+00:00"
    paths = {p["path"] for p in body["pages"]}
    assert "_index.md" in paths
    assert "log.md" in paths
    assert "contradictions.md" in paths
    assert "entities/btc-short-term-price.md" in paths
    assert "entities/mining-difficulty.md" in paths
    assert "system/_index.md" in paths
    assert "system/tools/get_bitcoin_onchain.md" in paths


def test_manifest_excludes_non_md_files(seeded_vault: Path) -> None:
    client = TestClient(_build_app())
    resp = client.get("/api/wiki/manifest")
    paths = {p["path"] for p in resp.json()["pages"]}
    assert "notes.txt" not in paths
    assert "entities/malicious.py" not in paths


def test_manifest_sections_derived_from_top_level_dir(
    seeded_vault: Path,
) -> None:
    client = TestClient(_build_app())
    resp = client.get("/api/wiki/manifest")
    by_path = {p["path"]: p for p in resp.json()["pages"]}
    assert by_path["_index.md"]["section"] == "root"
    assert by_path["contradictions.md"]["section"] == "root"
    assert by_path["log.md"]["section"] == "root"
    assert by_path["entities/btc-short-term-price.md"]["section"] == "entities"
    assert by_path["system/_index.md"]["section"] == "system"
    assert by_path["system/tools/get_bitcoin_onchain.md"]["section"] == "system"


def test_manifest_title_is_first_h1_when_present(seeded_vault: Path) -> None:
    client = TestClient(_build_app())
    resp = client.get("/api/wiki/manifest")
    by_path = {p["path"]: p for p in resp.json()["pages"]}
    assert by_path["entities/btc-short-term-price.md"]["title"] == "BTC short-term price"


def test_manifest_title_falls_back_to_stem_when_no_heading(
    seeded_vault: Path,
) -> None:
    client = TestClient(_build_app())
    resp = client.get("/api/wiki/manifest")
    by_path = {p["path"]: p for p in resp.json()["pages"]}
    assert by_path["entities/mining-difficulty.md"]["title"] == "mining-difficulty"


def test_manifest_pages_sorted(seeded_vault: Path) -> None:
    client = TestClient(_build_app())
    resp = client.get("/api/wiki/manifest")
    paths = [p["path"] for p in resp.json()["pages"]]
    assert paths == sorted(paths)


def test_manifest_compiled_at_falls_back_to_mtime_when_log_shape_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "log.md").write_text("no expected marker", encoding="utf-8")
    (tmp_path / "_index.md").write_text("# x\n", encoding="utf-8")
    monkeypatch.setattr(wiki, "VAULT_DIR", tmp_path)
    client = TestClient(_build_app())
    body = client.get("/api/wiki/manifest").json()
    assert body["compiled_at"] is not None


def test_manifest_missing_vault_returns_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(wiki, "VAULT_DIR", tmp_path / "nonexistent")
    client = TestClient(_build_app())
    body = client.get("/api/wiki/manifest").json()
    assert body == {"compiled_at": None, "pages": []}


# ---------- page -----------------------------------------------------------

def test_page_returns_content_and_mtime(seeded_vault: Path) -> None:
    client = TestClient(_build_app())
    resp = client.get("/api/wiki/page", params={"path": "_index.md"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["path"] == "_index.md"
    assert body["content"].startswith("# Morgoth Vault")
    assert body["mtime"].endswith("+00:00")


def test_page_returns_nested_content(seeded_vault: Path) -> None:
    client = TestClient(_build_app())
    resp = client.get(
        "/api/wiki/page",
        params={"path": "entities/btc-short-term-price.md"},
    )
    assert resp.status_code == 200
    assert "BTC short-term price" in resp.json()["content"]


# ---------- traversal matrix (the security core) --------------------------

@pytest.mark.parametrize("bad_path", [
    "../../../etc/passwd",
    "/etc/passwd",
    "entities/../../.env",
    "entities/x.py",
    "",
    "..",
    "entities/..",
    "\\etc\\passwd",
    "entities/no-such-file.md",
    "no-such-file.md",
])
def test_page_traversal_and_missing_share_404(
    seeded_vault: Path, bad_path: str,
) -> None:
    """Escape attempts and legitimately-missing files return the SAME
    404 detail — no oracle for the filesystem shape."""
    client = TestClient(_build_app())
    resp = client.get("/api/wiki/page", params={"path": bad_path})
    assert resp.status_code == 404
    assert resp.json()["detail"] == "page not found"


def test_page_symlink_escape_returns_404(
    seeded_vault: Path,
) -> None:
    """A symlink inside the vault pointing outside must not leak content."""
    outside = seeded_vault.parent / "outside_secret.md"
    outside.write_text("SECRET", encoding="utf-8")
    (seeded_vault / "hop.md").symlink_to(outside)
    client = TestClient(_build_app())
    resp = client.get("/api/wiki/page", params={"path": "hop.md"})
    assert resp.status_code == 404


def test_page_non_md_suffix_rejected(seeded_vault: Path) -> None:
    """Even a legitimate file that exists but isn't .md must 404."""
    client = TestClient(_build_app())
    resp = client.get(
        "/api/wiki/page",
        params={"path": "entities/malicious.py"},
    )
    assert resp.status_code == 404


def test_page_missing_query_param_rejected(seeded_vault: Path) -> None:
    """No ``path`` → 422 (FastAPI validation), not 500."""
    client = TestClient(_build_app())
    resp = client.get("/api/wiki/page")
    assert resp.status_code == 422
