"""GET /api/wiki/graph — wikilink node/edge graph over the vault.

The graph endpoint walks VAULT_DIR *.md, parses ``[[target]]`` /
``[[target|label]]`` markers, and returns:
  - nodes: id (rel path sans .md) + title + section (root|entities|
    system|missing) + degree
  - edges: {source, target} deduped

Missing targets (referenced but not present) get a synthetic node
with ``section: "missing"`` — a dangling link is information.
Self-links do not form an edge. Sections match /manifest.
"""
from __future__ import annotations

import sys
from pathlib import Path
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
def linked_vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A small vault with wikilinks in realistic shapes."""
    # Root pages.
    (tmp_path / "_index.md").write_text(
        "# Index\n\nLinks: [[entities/btc-short-term-price]] and "
        "[[entities/mining-difficulty|difficulty]] and [[system/thesis-hub]].\n",
        encoding="utf-8",
    )
    (tmp_path / "contradictions.md").write_text(
        "# Contradictions\n\nSee [[entities/btc-short-term-price.md]] "
        "for context (trailing .md must be stripped).\n",
        encoding="utf-8",
    )
    # Entities.
    (tmp_path / "entities").mkdir()
    (tmp_path / "entities" / "btc-short-term-price.md").write_text(
        "# BTC short-term price\n\nRelated: [[entities/mining-difficulty]] "
        "and [[system/thesis-hub]] and [[entities/does-not-exist]].\n"
        # Duplicate link — dedupe test.
        "Also: [[entities/mining-difficulty]] again.\n"
        # Self link — no edge.
        "Self: [[entities/btc-short-term-price]].\n",
        encoding="utf-8",
    )
    (tmp_path / "entities" / "mining-difficulty.md").write_text(
        "# Mining difficulty\n\nBacklink to [[entities/btc-short-term-price]].\n",
        encoding="utf-8",
    )
    # System.
    (tmp_path / "system").mkdir()
    (tmp_path / "system" / "thesis-hub.md").write_text(
        "# Thesis hub\n\nHub: "
        + " ".join(f"[[entities/e-{i}]]" for i in range(1, 6))
        + "\n",  # 5 references to non-existent entities → all missing
        encoding="utf-8",
    )
    # Non-md decoy.
    (tmp_path / "notes.txt").write_text("[[entities/should-be-ignored]]",
                                        encoding="utf-8")
    monkeypatch.setattr(wiki, "VAULT_DIR", tmp_path)
    return tmp_path


# ---------- nodes ----------------------------------------------------

def test_graph_nodes_include_all_real_pages_with_id_sans_md(
    linked_vault: Path,
) -> None:
    client = TestClient(_build_app())
    resp = client.get("/api/wiki/graph")
    assert resp.status_code == 200
    ids = {n["id"] for n in resp.json()["nodes"]}
    # Real pages — ids are rel path sans .md, matching the wikilink
    # transform's cleanTarget.
    for real in (
        "_index", "contradictions",
        "entities/btc-short-term-price", "entities/mining-difficulty",
        "system/thesis-hub",
    ):
        assert real in ids, f"missing real node: {real}"


def test_graph_missing_nodes_tagged(linked_vault: Path) -> None:
    """Dangling wikilinks yield synthetic nodes with section=missing."""
    client = TestClient(_build_app())
    by_id = {n["id"]: n for n in client.get("/api/wiki/graph").json()["nodes"]}
    # entities/does-not-exist referenced from btc-short-term-price
    assert by_id["entities/does-not-exist"]["section"] == "missing"
    # thesis-hub's 5 unresolved e-1..e-5
    for i in range(1, 6):
        assert by_id[f"entities/e-{i}"]["section"] == "missing"


def test_graph_sections_match_manifest(linked_vault: Path) -> None:
    client = TestClient(_build_app())
    by_id = {n["id"]: n for n in client.get("/api/wiki/graph").json()["nodes"]}
    assert by_id["_index"]["section"] == "root"
    assert by_id["contradictions"]["section"] == "root"
    assert by_id["entities/btc-short-term-price"]["section"] == "entities"
    assert by_id["system/thesis-hub"]["section"] == "system"


def test_graph_titles_use_h1_when_present(linked_vault: Path) -> None:
    client = TestClient(_build_app())
    by_id = {n["id"]: n for n in client.get("/api/wiki/graph").json()["nodes"]}
    assert by_id["entities/btc-short-term-price"]["title"] == "BTC short-term price"
    assert by_id["system/thesis-hub"]["title"] == "Thesis hub"


def test_missing_node_title_falls_back_to_slug(linked_vault: Path) -> None:
    """Missing pages get a human-ish label from the last segment
    (dashes → spaces) so the graph render isn't dominated by
    ``entities/e-1``-style hashes."""
    client = TestClient(_build_app())
    by_id = {n["id"]: n for n in client.get("/api/wiki/graph").json()["nodes"]}
    assert by_id["entities/does-not-exist"]["title"] == "does not exist"


# ---------- edges ----------------------------------------------------

def test_graph_edges_include_expected_links(linked_vault: Path) -> None:
    client = TestClient(_build_app())
    edges = {(e["source"], e["target"])
             for e in client.get("/api/wiki/graph").json()["edges"]}
    assert ("_index", "entities/btc-short-term-price") in edges
    assert ("_index", "entities/mining-difficulty") in edges
    assert ("_index", "system/thesis-hub") in edges
    # Contradictions → BTC (trailing .md was stripped in extraction).
    assert ("contradictions", "entities/btc-short-term-price") in edges
    # Cross-entity backlink.
    assert ("entities/mining-difficulty", "entities/btc-short-term-price") in edges


def test_graph_dedupes_duplicate_wikilinks(linked_vault: Path) -> None:
    """The btc-short-term-price page references mining-difficulty
    twice — exactly one edge must be emitted."""
    client = TestClient(_build_app())
    edges = [(e["source"], e["target"])
             for e in client.get("/api/wiki/graph").json()["edges"]]
    dup_target = ("entities/btc-short-term-price", "entities/mining-difficulty")
    assert edges.count(dup_target) == 1


def test_graph_drops_self_links(linked_vault: Path) -> None:
    """A page referencing itself does NOT emit an edge."""
    client = TestClient(_build_app())
    edges = {(e["source"], e["target"])
             for e in client.get("/api/wiki/graph").json()["edges"]}
    assert ("entities/btc-short-term-price",
            "entities/btc-short-term-price") not in edges


def test_graph_edges_directional_but_stable_ordering(linked_vault: Path) -> None:
    """Edges are sorted so the JSON response is deterministic — the
    frontend can safely use it as a cache key."""
    client = TestClient(_build_app())
    edges = [(e["source"], e["target"])
             for e in client.get("/api/wiki/graph").json()["edges"]]
    assert edges == sorted(edges)


# ---------- degree ---------------------------------------------------

def test_graph_degree_counts_incoming_plus_outgoing(linked_vault: Path) -> None:
    """Node degree is undirected — sum of incoming + outgoing edges.
    The btc-short-term-price hub should dominate the small graph."""
    client = TestClient(_build_app())
    by_id = {n["id"]: n for n in client.get("/api/wiki/graph").json()["nodes"]}
    # btc-short-term-price is linked from _index, contradictions,
    # entities/mining-difficulty (3 in) and links out to
    # mining-difficulty, thesis-hub, does-not-exist (3 out) — total 6.
    assert by_id["entities/btc-short-term-price"]["degree"] == 6
    # thesis-hub: 5 outgoing to missing e-1..e-5, 2 incoming (_index + btc).
    assert by_id["system/thesis-hub"]["degree"] == 7


def test_graph_missing_nodes_carry_degree(linked_vault: Path) -> None:
    client = TestClient(_build_app())
    by_id = {n["id"]: n for n in client.get("/api/wiki/graph").json()["nodes"]}
    assert by_id["entities/does-not-exist"]["degree"] == 1


# ---------- misc / robustness ---------------------------------------

def test_graph_ignores_non_md_files(linked_vault: Path) -> None:
    """A .txt file with a wikilink in it must not produce nodes/edges."""
    client = TestClient(_build_app())
    ids = {n["id"] for n in client.get("/api/wiki/graph").json()["nodes"]}
    assert "entities/should-be-ignored" not in ids


def test_graph_empty_vault_returns_empty_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(wiki, "VAULT_DIR", tmp_path)
    client = TestClient(_build_app())
    body = client.get("/api/wiki/graph").json()
    assert body == {"nodes": [], "edges": []}


def test_graph_missing_vault_dir_returns_empty_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(wiki, "VAULT_DIR", tmp_path / "nope")
    client = TestClient(_build_app())
    body = client.get("/api/wiki/graph").json()
    assert body == {"nodes": [], "edges": []}
