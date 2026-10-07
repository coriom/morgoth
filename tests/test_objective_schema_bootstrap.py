"""Legacy/public and managed objective DDL retain one runtime owner."""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from memory import persistent
from scripts.init_db import EXTRA_TABLE_STATEMENTS


@pytest.mark.asyncio
async def test_legacy_public_path_uses_canonical_objective_ddl(app_config, monkeypatch):
    """The default/public branch uses the same base DDL before migrations."""
    executed: list[str] = []
    pool_options: dict[str, object] = {}

    class FakeConnection:
        async def execute(self, statement: str, *_args: object) -> str:
            executed.append(statement)
            return "OK"

    connection = FakeConnection()

    class FakePool:
        @asynccontextmanager
        async def acquire(self):
            yield connection

        async def close(self) -> None:
            return None

    async def fake_create_pool(**options: object) -> FakePool:
        pool_options.update(options)
        await options["init"](connection)
        await options["setup"](connection)
        return FakePool()

    monkeypatch.setattr("core.project.current_namespace",
                        lambda: SimpleNamespace(postgres_schema="public"))
    monkeypatch.setattr(persistent.asyncpg, "create_pool", fake_create_pool)
    memory = persistent.PersistentMemory(app_config)
    await memory.initialize()
    await memory.close()
    assert pool_options["server_settings"] == {"search_path": "public"}
    assert executed[:2] == ["SET search_path TO public", "SET search_path TO public"]
    assert persistent.CREATE_EXTENSION_SQL in executed
    base = [sql for sql in executed if "CREATE TABLE IF NOT EXISTS objectives (" in sql]
    assert len(base) == 1
    assert executed.index(base[0]) < next(i for i, sql in enumerate(executed)
                                       if "ALTER TABLE objectives ADD COLUMN" in sql)
    assert not any("CREATE TABLE IF NOT EXISTS objectives (" in sql
                   for sql in EXTRA_TABLE_STATEMENTS)
    assert any("CREATE TABLE IF NOT EXISTS ui_widgets (" in sql
               for sql in EXTRA_TABLE_STATEMENTS)
    assert any("CREATE TABLE IF NOT EXISTS api_leads (" in sql
               for sql in EXTRA_TABLE_STATEMENTS)
