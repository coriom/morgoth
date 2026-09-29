"""Legacy Domain fields remain readable; storage authority now belongs to Project.

The original DDL, collection and vault routing checks use Project namespaces.
Domain-only declarations are compatibility data, never storage selection.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from core import domain as _dom


@pytest.fixture(autouse=True)
def _reset_domain_between_tests():
    """Every test in this file monkeypatches ``core.domain`` — reset
    the memoized pack before and after so state doesn't leak. Guard
    against monkeypatch teardown-order: if a test replaced
    ``current_domain`` with a lambda, cache_clear won't exist. The
    real function's cache is the one we care about — access it via
    the module's globals to bypass any monkeypatched binding still
    in flight during teardown."""
    def _safe_reset():
        try:
            _dom.reset_domain_cache()
        except AttributeError:
            # current_domain was replaced by a lambda earlier and
            # monkeypatch's undo runs AFTER this fixture's teardown.
            # Silent: the next test's setup will fire this same
            # branch and the state will be clean by then.
            pass
    _safe_reset()
    yield
    _safe_reset()


# ─── crypto's historical namespace is locked ───────────────────────

def test_crypto_pack_declares_historical_namespace(monkeypatch) -> None:
    """Crypto keeps: public schema, empty prefix, ~/Morgoth/vault."""
    monkeypatch.setenv("MORGOTH_DOMAIN", "crypto")
    # Cache is reset by the module-level fixture below.
    d = _dom.current_domain()
    assert d.postgres_schema == "public"
    assert d.chroma_prefix == ""
    assert d.vault_dir == "~/Morgoth/vault"
    # Cache is reset by the module-level fixture below.


def test_missing_namespace_fields_default_safely(monkeypatch, tmp_path) -> None:
    """Deprecated Domain fields keep historical defaults for pack readers only.
    New Project manifests MUST supply their own isolated namespaces.
    """
    pack_dir = tmp_path / "domains" / "minimal"
    pack_dir.mkdir(parents=True)
    (pack_dir / "domain.yaml").write_text("name: minimal\n", encoding="utf-8")
    monkeypatch.setattr(_dom, "_DOMAINS_ROOT", tmp_path / "domains")
    monkeypatch.setenv("MORGOTH_DOMAIN", "minimal")
    # Cache is reset by the module-level fixture below.
    d = _dom.current_domain()
    assert d.postgres_schema == "public"
    assert d.chroma_prefix == ""
    assert d.vault_dir.endswith("/Morgoth/vault")
    # Cache is reset by the module-level fixture below.


# ─── isolation: two packs, no crossover ────────────────────────────


def _write_pack(root: Path, name: str, schema: str, prefix: str,
                vault: str) -> None:
    d = root / "domains" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "domain.yaml").write_text(
        f"name: {name}\n"
        f"postgres_schema: {schema!r}\n"
        f"chroma_prefix: {prefix!r}\n"
        f"vault_dir: {vault!r}\n",
        encoding="utf-8",
    )


def _load_domain(name: str, root: Path, monkeypatch) -> object:
    """Load one domain pack from ``root`` — mimics a fresh process
    booting with ``MORGOTH_DOMAIN=<name>`` (module-level constants
    freeze the pack). Uses monkeypatch so cleanup is automatic."""
    monkeypatch.setattr(_dom, "_DOMAINS_ROOT", root / "domains")
    monkeypatch.setenv("MORGOTH_DOMAIN", name)
    _dom.reset_domain_cache()  # force fresh load with the new env var
    return _dom.current_domain()


def test_two_packs_declare_disjoint_namespaces(tmp_path, monkeypatch) -> None:
    """Author two packs — crypto-shaped + a throwaway ``testpack`` —
    and verify their namespace fields DON'T overlap. This is the
    root property; downstream isolation (Postgres schema, Chroma
    collections, vault files) follows mechanically."""
    _write_pack(tmp_path, "cryptoish", "public", "", "/tmp/vault_a")
    _write_pack(tmp_path, "testpack", "morgoth_testpack",
                "testpack_", "/tmp/vault_b")

    d1 = _load_domain("cryptoish", tmp_path, monkeypatch)
    d2 = _load_domain("testpack", tmp_path, monkeypatch)

    # Schemas disjoint.
    assert d1.postgres_schema != d2.postgres_schema
    # Prefixes disjoint (empty vs non-empty is fine — empty is crypto).
    assert d1.chroma_prefix != d2.chroma_prefix
    # Vault dirs disjoint.
    assert d1.vault_dir != d2.vault_dir


def test_persistent_memory_issues_schema_ddl_for_non_public(monkeypatch) -> None:
    """PersistentMemory.initialize must ``CREATE SCHEMA IF NOT EXISTS``
    and ``SET search_path`` for a non-public schema. This is the wire
    that makes a new project's rows land in its own schema."""
    from memory import persistent as _p

    monkeypatch.setenv("MORGOTH_DOMAIN", "crypto")  # deterministic pack
    # Cache is reset by the module-level fixture below.
    from core import project
    monkeypatch.setattr(project, "current_namespace", lambda: MagicMock(postgres_schema="weather"))

    executed: list[str] = []

    class _Conn:
        async def execute(_self, sql, *a, **kw):
            executed.append(sql)
        async def __aenter__(_self): return _self
        async def __aexit__(_self, *e): return False

    class _Pool:
        def acquire(_self):
            class _ACM:
                async def __aenter__(__s): return _Conn()
                async def __aexit__(__s, *e): return False
            return _ACM()

    async def _fake_create_pool(**kwargs):
        # Simulate init callback firing on a fresh connection.
        if kwargs.get("init"):
            await kwargs["init"](_Conn())
        return _Pool()

    monkeypatch.setattr(_p.asyncpg, "create_pool", _fake_create_pool)

    import asyncio
    cfg = MagicMock()
    cfg.postgres_url = "postgresql://ignored"
    pm = _p.PersistentMemory(cfg)
    asyncio.run(pm.initialize())

    joined = "\n".join(executed).lower()
    # The three load-bearing DDL/DML statements the domain namespace
    # relies on:
    assert 'set search_path to "weather"' in joined, executed
    assert ", public" not in joined
    assert 'create schema if not exists "weather"' in joined, executed
    # Cache is reset by the module-level fixture below.


def test_persistent_memory_uses_plain_search_path_for_public(monkeypatch) -> None:
    """For the crypto pack (public schema) the init MUST NOT emit a
    ``CREATE SCHEMA public`` (Postgres reserves it) and MUST set the
    search_path to public. Locks the "crypto path is untouched" leg
    of the chantier-2 contract."""
    from memory import persistent as _p

    monkeypatch.setenv("MORGOTH_DOMAIN", "crypto")
    # Cache is reset by the module-level fixture below.

    executed: list[str] = []

    class _Conn:
        async def execute(_self, sql, *a, **kw):
            executed.append(sql)

    class _Pool:
        def acquire(_self):
            class _ACM:
                async def __aenter__(__s): return _Conn()
                async def __aexit__(__s, *e): return False
            return _ACM()

    async def _fake_create_pool(**kwargs):
        if kwargs.get("init"):
            await kwargs["init"](_Conn())
        return _Pool()

    monkeypatch.setattr(_p.asyncpg, "create_pool", _fake_create_pool)

    import asyncio
    cfg = MagicMock()
    cfg.postgres_url = "postgresql://ignored"
    pm = _p.PersistentMemory(cfg)
    asyncio.run(pm.initialize())

    joined = "\n".join(executed).lower()
    assert "set search_path to public" in joined
    assert "create schema if not exists" not in joined, (
        "crypto pack is public — must not attempt CREATE SCHEMA public"
    )


def test_episodic_memory_prefixes_collections_for_non_empty_prefix(monkeypatch) -> None:
    """A project with ``chroma_prefix="w_"`` exposes collections
    ``w_conversations`` etc. — routing is transparent to callers who
    keep passing the logical name. This is the wire that keeps two
    projects' Chroma names disjoint, in addition to separate state dirs."""
    from memory import episodic as _e

    monkeypatch.setenv("MORGOTH_DOMAIN", "crypto")
    # Cache is reset by the module-level fixture below.
    from core import project
    monkeypatch.setattr(project, "current_namespace", lambda: MagicMock(
        chroma_prefix="w_", is_legacy=False, chroma_dir=Path("/tmp/weather/chroma_db")))

    em = _e.EpisodicMemory("data/chroma_db")
    assert em.collections == tuple(
        "w_" + c for c in _e.DEFAULT_COLLECTIONS
    )
    assert em._physical_name("conversations") == "w_conversations"
    # Idempotent: passing an already-prefixed name doesn't double-prefix.
    assert em._physical_name("w_research") == "w_research"
    # Cache is reset by the module-level fixture below.


def test_episodic_memory_no_prefix_for_crypto(monkeypatch) -> None:
    """Crypto's chroma_prefix is empty — collections stay named as
    they were before chantier 2 so the historical Chroma persist dir
    is bit-identical."""
    from memory import episodic as _e

    monkeypatch.setenv("MORGOTH_DOMAIN", "crypto")
    # Cache is reset by the module-level fixture below.
    em = _e.EpisodicMemory("data/chroma_db")
    assert em.collections == _e.DEFAULT_COLLECTIONS
    assert em._physical_name("conversations") == "conversations"


def test_vault_dir_resolves_from_project(monkeypatch, tmp_path) -> None:
    """Wiki constants use the canonical Project namespace, not Domain fields."""
    from core import project
    from runpy import run_path
    for name in ("a", "b"):
        vault = tmp_path / name
        monkeypatch.setattr(project, "current_namespace", lambda: MagicMock(vault_dir=vault))
        # Independent module namespace; don't poison existing API imports.
        result = run_path(str(Path(__file__).parents[1] / "scripts/compile_wiki.py"), run_name="project_vault_test")
        assert result["VAULT_DIR"] == vault
