"""Bounded subprocess proof: configuration only, or explicitly morgoth_test."""
from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace
from urllib.parse import urlparse

from core.project import current_project
from core.domain import current_domain
from memory.episodic import EpisodicMemory
from scripts.compile_wiki import VAULT_DIR
from api.token import TOKEN_DIR


async def main() -> None:
    """Never load application dotenv, start an engine, or invoke tools/LLMs."""
    project = current_project()
    memory = EpisodicMemory()
    result = dict(domain=current_domain().name, schema=project.postgres_schema,
                  collections=memory.collections, vault=str(VAULT_DIR),
                  runtime=str(project.runtime_dir), chroma=str(memory._persist_directory),
                  token=str(TOKEN_DIR), environment_clean="SYNTHETIC_PARENT_ONLY" not in os.environ and "POSTGRES_URL" not in os.environ)
    dsn = os.environ.get("MORGOTH_TEST_POSTGRES_URL")
    if dsn:
        if urlparse(dsn).path != "/morgoth_test":
            raise RuntimeError("refusing non-test database")
        from memory.persistent import PersistentMemory
        pm = PersistentMemory(SimpleNamespace(postgres_url=dsn))
        try:
            await pm.initialize()
            await pm.execute("INSERT INTO knowledge (category, key, value) VALUES ('project-proof', 'same-key', $1)", project.id)
            for _ in range(3):
                row = await pm.fetchrow("SELECT current_schema() AS schema, current_schemas(false) AS schemas")
                assert row["schema"] == project.postgres_schema
                assert row["schemas"] == [project.postgres_schema]
            rows = await pm.fetch("SELECT value FROM knowledge WHERE category = 'project-proof' AND key = 'same-key'")
            assert [r["value"] for r in rows] == [project.id]
            # A public-only sentinel table must not be visible via search_path.
            import asyncpg
            try:
                await pm.fetch("SELECT * FROM project_public_escape_probe")
            except asyncpg.UndefinedTableError:
                pass
            else:
                raise AssertionError("public storage escaped into isolated project")
            result["database_isolated"] = True
        finally:
            await pm.close()
    print(json.dumps(result))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:
        # A test assertion/driver error must never print a DSN or env values.
        import traceback
        frames = traceback.extract_tb(exc.__traceback__)
        print(json.dumps({"failure_type": type(exc).__name__, "line": frames[-1].lineno}))
        raise SystemExit(1) from None
