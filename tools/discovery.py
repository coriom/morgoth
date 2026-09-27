"""Auto-discovery of data-feed tools.

Enumerates every ``BaseTool`` subclass declared under ``tools.data_feeds``
so that ``core.brain`` and ``api.server`` don't have to hand-list them.
This is the structural prerequisite for step 2 of the self-modify
sequence: a green-zone new file under ``tools/data_feeds/`` becomes
active on restart without editing any red-zone file.

Discovery scope
---------------
- ``tools.data_feeds`` package only (skips ``__init__``).
- Emits classes whose ``__module__`` matches the discovering module,
  so a helper class re-imported from elsewhere isn't returned twice.
- Sorted by ``cls.name`` for deterministic order.
"""

from __future__ import annotations

import importlib
import pkgutil
from typing import Any

from loguru import logger

from tools import data_feeds
from tools.base_tool import BaseTool


# 2026-09-29: discovery isolation. A single broken tool file (syntax
# error, bad import) MUST NOT take down every other tool. Load errors
# are captured here so a "no broken files" test can lock the rail
# against silent regression.
_load_errors: list[tuple[str, str]] = []


def discovery_load_errors() -> list[tuple[str, str]]:
    """Return [(module_name, error_repr)] for tools that failed to
    import on the last discovery pass. Empty in the healthy state."""
    return list(_load_errors)


def discover_data_feed_tools() -> list[type[BaseTool]]:
    """Return every ``BaseTool`` subclass declared under tools.data_feeds.

    ISOLATION (2026-09-29): each module is imported in its own
    try/except; a broken file is EXCLUDED, logged loudly, and
    recorded in ``_load_errors``. A production hazard the operator
    called out: pre-fix, one SyntaxError in one tool broke
    discover_data_feed_tools for ALL tools (200-test cascade), and
    a single bad file at restart would have taken the whole rail
    down. Now: N-1 tools stay live; the broken one shows up in the
    load-errors report.
    """
    global _load_errors
    _load_errors = []
    classes: list[type[BaseTool]] = []
    seen: set[str] = set()
    for module_info in pkgutil.iter_modules(data_feeds.__path__):
        if module_info.name.startswith("_"):
            continue
        full_name = f"tools.data_feeds.{module_info.name}"
        try:
            mod = importlib.import_module(full_name)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "discovery: FAILED to import {} — {}: {}. "
                "Tool EXCLUDED from the rail; other tools unaffected.",
                full_name, type(exc).__name__, str(exc)[:200],
            )
            _load_errors.append((full_name, f"{type(exc).__name__}: {exc}"))
            continue
        for attr_name in dir(mod):
            obj = getattr(mod, attr_name)
            if not isinstance(obj, type):
                continue
            if not issubclass(obj, BaseTool) or obj is BaseTool:
                continue
            if getattr(obj, "__module__", None) != full_name:
                continue
            if not getattr(obj, "name", None):
                continue
            if obj.name in seen:
                continue
            seen.add(obj.name)
            classes.append(obj)
    classes.sort(key=lambda cls: cls.name)
    return classes


def instantiate_tool(
    cls: type[BaseTool],
    config: Any,
    persistent_memory: Any,
) -> BaseTool:
    """Instantiate a discovered tool, passing ``persistent_memory`` only if
    the constructor asks for it.

    Every data-feed tool takes ``config`` as its first positional arg.
    Some also take ``persistent_memory`` (e.g. GetCryptoPriceTool caches
    into the DB); auto-discovery must honor either.
    """
    import inspect

    sig = inspect.signature(cls.__init__)
    if "persistent_memory" in sig.parameters:
        return cls(config, persistent_memory=persistent_memory)  # type: ignore[call-arg]
    return cls(config)  # type: ignore[call-arg]
