"""Fresh-process, no-DB/no-LLM proof of Project-scoped tool authority."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from core import domain
from tools import data_feeds


async def main() -> None:
    """Use only synthetic fixture tools and explicitly supplied Project state."""
    domain._DOMAINS_ROOT = Path(sys.argv[1])
    data_feeds.__path__.append(sys.argv[2])
    from core.project import current_project
    from core.tool_rail import STATIC_TOOLS, installed_catalog
    from core.brain import CHAT_TOOL_NAMES, DATA_SOURCE_TOOLS
    from api.server import build_tool_router
    from api.routes.tools import installed_tools, list_tools
    from core.tool_router import ToolAccessError
    from tools.discovery import discover_data_feed_tools, instantiate_tool

    project = current_project()
    pack = domain.current_domain()
    selected = pack.rail_tools[0]
    other = "fixture_other_domain_only" if selected == "fixture_crypto_only" else "fixture_crypto_only"
    catalog = installed_catalog()
    assert selected in catalog and other in catalog
    assert project.domain == pack.name
    assert DATA_SOURCE_TOOLS == frozenset({selected})
    assert selected in CHAT_TOOL_NAMES and other not in CHAT_TOOL_NAMES

    args = [MagicMock() for _ in range(5)]
    router = build_tool_router(*args)
    globals_ = {name for name, spec in STATIC_TOOLS.items() if spec.global_internal}
    assert set(router.list_names()) == globals_ | {selected}
    assert {s["function"]["name"] for s in router.get_schemas(CHAT_TOOL_NAMES)} == set(CHAT_TOOL_NAMES)
    assert other not in {s["function"]["name"] for s in router.get_schemas()}
    try:
        router.get_schemas([other])
    except ToolAccessError as exc:
        assert exc.code == "TOOL_NOT_ALLOWED_FOR_DOMAIN"
    else:
        raise AssertionError("inactive schema exposed")
    assert (await router.execute_tool(selected, {}))["success"] is True
    assert (await router.execute_tool(other, {}))["error"] == "TOOL_NOT_ALLOWED_FOR_DOMAIN"
    assert (await router.execute_tool("fixture_missing", {}))["error"] == "UNKNOWN_TOOL"
    classes = {cls.name: cls for cls in discover_data_feed_tools()}
    try:
        router.register(instantiate_tool(classes[other], args[0], args[1]))
    except ToolAccessError as exc:
        assert exc.code == "TOOL_NOT_ALLOWED_FOR_DOMAIN"
    else:
        raise AssertionError("inactive registration accepted")

    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(tool_router=router)))
    exposed = {row["name"] for row in await list_tools(request)}
    installed = {row["name"]: row["active"] for row in await installed_tools(request)}
    assert exposed == globals_ | {selected}
    assert installed[selected] is True and installed[other] is False
    print(json.dumps({"project": project.id, "domain": pack.name,
                      "schema": project.postgres_schema,
                      "chroma": project.chroma_prefix,
                      "vault": str(project.vault_dir),
                      "runtime": str(project.runtime_dir),
                      "selected": selected, "other": other,
                      "installed_both": True, "active_only_selected": True}))


if __name__ == "__main__":
    asyncio.run(main())
