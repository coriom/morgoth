"""Chantier 2/5 · domain-pack namespace isolation.

The active domain pack declares three storage-namespace fields —
``postgres_schema``, ``chroma_prefix``, ``vault_dir`` — and every
downstream write MUST land inside that namespace. Two processes
running two different packs (crypto + a throwaway test pack) must
NOT share rows, Chroma collections, or vault files.

Crypto's historical values (public schema, empty prefix, ~/Morgoth/
vault) are locked separately by the crypto-pack grep tests. This
file focuses on the ROUTING: given a pack, does the write land in
its namespace?

The isolation test uses two Domain instances at the DATA layer — no
live Postgres or Chroma required. What we're locking is that
``PersistentMemory.initialize`` issues the ``SET search_path`` and
``CREATE SCHEMA`` calls for the pack's schema, and that
``EpisodicMemory``'s collection surface is prefixed. Behaviour under
morgoth test (bwrap + no live services) is deterministic — a
different domain writes to a different set of names, period.
"""

from __future__ import annotations

import importlib
import sys
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
    """A minimal pack without namespace fields must default to
    values that leave EXISTING crypto-shaped storage untouched.

    Rationale: an operator authoring a new pack should not have to
    remember three fields to keep production intact. The safe default
    is: ``postgres_schema=public`` (same as crypto), ``chroma_prefix=""``
    (same as crypto), ``vault_dir=~/Morgoth/vault`` (same as crypto).
    That means a bare pack with only the tokens declared is INDIS-
    tinguishable from crypto at the storage layer — which is
    intentional for chantier 2's "no production migration" contract."""
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
    that makes a new domain's rows land in its own schema."""
    from memory import persistent as _p

    monkeypatch.setenv("MORGOTH_DOMAIN", "crypto")  # deterministic pack
    # Cache is reset by the module-level fixture below.
    # Force the pack into the state we want to test: non-public schema.
    real_pack = _dom.current_domain()
    fake = real_pack.__class__(**{
        **{f: getattr(real_pack, f) for f in real_pack.__dataclass_fields__},
        "postgres_schema": "weather",
    })
    monkeypatch.setattr(_dom, "current_domain", lambda: fake, raising=True)

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
    assert 'set search_path to "weather", public' in joined, executed
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
    """A domain with ``chroma_prefix="w_"`` exposes collections
    ``w_conversations`` etc. — routing is transparent to callers who
    keep passing the logical name. This is the wire that keeps two
    domains' Chroma data disjoint inside the SAME persist dir."""
    from memory import episodic as _e

    monkeypatch.setenv("MORGOTH_DOMAIN", "crypto")
    # Cache is reset by the module-level fixture below.
    real_pack = _dom.current_domain()
    fake = real_pack.__class__(**{
        **{f: getattr(real_pack, f) for f in real_pack.__dataclass_fields__},
        "chroma_prefix": "w_",
    })
    monkeypatch.setattr(_dom, "current_domain", lambda: fake, raising=True)

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


def test_vault_dir_expands_from_pack(monkeypatch, tmp_path) -> None:
    """compile_wiki.VAULT_DIR is read at module import from the pack's
    ``vault_dir`` (with ~ and $ expansion). Domain A's vault path and
    domain B's vault path do NOT overlap — a wiki compile in one
    domain never touches the other domain's markdown."""
    monkeypatch.setenv("MORGOTH_DOMAIN", "crypto")
    # Cache is reset by the module-level fixture below.
    real_pack = _dom.current_domain()

    # Domain A: crypto's default ~/Morgoth/vault.
    fake_a = real_pack.__class__(**{
        **{f: getattr(real_pack, f) for f in real_pack.__dataclass_fields__},
        "vault_dir": "~/Morgoth/vault",
    })
    monkeypatch.setattr(_dom, "current_domain", lambda: fake_a, raising=True)
    if "scripts.compile_wiki" in sys.modules:
        del sys.modules["scripts.compile_wiki"]
    import scripts.compile_wiki as cw_a
    a_dir = cw_a.VAULT_DIR
    assert str(a_dir).endswith("/Morgoth/vault")

    # Domain B: overridden to a temp dir.
    fake_b = real_pack.__class__(**{
        **{f: getattr(real_pack, f) for f in real_pack.__dataclass_fields__},
        "vault_dir": str(tmp_path / "weather_vault"),
    })
    monkeypatch.setattr(_dom, "current_domain", lambda: fake_b, raising=True)
    del sys.modules["scripts.compile_wiki"]
    import scripts.compile_wiki as cw_b
    b_dir = cw_b.VAULT_DIR
    assert b_dir == tmp_path / "weather_vault"

    # Disjoint.
    assert a_dir != b_dir
    # Cache is reset by the module-level fixture below.
