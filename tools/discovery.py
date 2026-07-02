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

from tools import data_feeds
from tools.base_tool import BaseTool


def discover_data_feed_tools() -> list[type[BaseTool]]:
    """Return every ``BaseTool`` subclass declared under tools.data_feeds."""
    classes: list[type[BaseTool]] = []
    seen: set[str] = set()
    for module_info in pkgutil.iter_modules(data_feeds.__path__):
        if module_info.name.startswith("_"):
            continue
        full_name = f"tools.data_feeds.{module_info.name}"
        mod = importlib.import_module(full_name)
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
