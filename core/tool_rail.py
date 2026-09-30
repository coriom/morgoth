"""Installed tool catalog and the active Domain's authority boundary.

Discovery describes installation. Only Domain rail membership activates research
tools; a small, canonical core policy owns universal orchestration tools.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Iterable, Mapping


@dataclass(frozen=True)
class ToolKind:
    """Catalog classification, independent of whether a Domain activates it."""

    global_internal: bool
    data_source: bool
    chat: bool


# This is the single core classification for statically installed tools.
# Order preserves the historical chat-schema order for the default Project.
STATIC_TOOLS: dict[str, ToolKind] = {
    "web_search": ToolKind(False, True, True),
    "fred_series_observations": ToolKind(False, True, True),
    "fred_series_search": ToolKind(False, False, True),
    "technical_analysis": ToolKind(False, False, True),
    "remember": ToolKind(True, False, True),
    "recall": ToolKind(True, False, True),
    "create_objective": ToolKind(True, False, True),
    "update_objective": ToolKind(True, False, True),
    "execute_python": ToolKind(True, False, False),
    "read_file": ToolKind(True, False, False),
    "write_file": ToolKind(True, False, False),
    "create_agent": ToolKind(True, False, False),
    "notify": ToolKind(True, False, False),
}


class ToolRailError(ValueError):
    """Invalid Domain rail or installed catalog; fail before runtime use."""


@dataclass(frozen=True)
class EffectiveToolRail:
    """One process's immutable installed, active, source and chat sets."""

    installed: frozenset[str]
    allowed: frozenset[str]
    sources: frozenset[str]
    chat: tuple[str, ...]

    def is_allowed(self, name: str) -> bool:
        """Check the sole active-tool permission predicate."""
        return name in self.allowed

    def denial_code(self, name: str) -> str:
        """Distinguish installed-but-inactive from a nonexistent tool."""
        return "TOOL_NOT_ALLOWED_FOR_DOMAIN" if name in self.installed else "UNKNOWN_TOOL"


def installed_catalog(discovered: Iterable[type] | None = None) -> dict[str, ToolKind]:
    """List installed implementations without granting them runtime authority."""
    if discovered is None:
        from tools.discovery import discover_data_feed_tools
        discovered = discover_data_feed_tools()
    catalog = dict(STATIC_TOOLS)
    for cls in discovered:
        name = cls.name
        if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", name):
            raise ToolRailError("installed tool has malformed name")
        if name in catalog:
            raise ToolRailError(f"duplicate installed tool name: {name}")
        catalog[name] = ToolKind(False, bool(getattr(cls, "is_data_source", False)),
                                 bool(getattr(cls, "is_chat_tool", True)))
    return catalog


def validate_declared_rail(names: tuple[str, ...], catalog: Mapping[str, ToolKind]) -> None:
    """Require every declared research tool to be installed and non-global."""
    if len(set(names)) != len(names):
        raise ToolRailError("Domain rail contains duplicate names")
    if any(not re.fullmatch(r"[a-z][a-z0-9_]*", name) for name in names):
        raise ToolRailError("Domain rail contains malformed name")
    missing = set(names) - set(catalog)
    if missing:
        raise ToolRailError(f"Domain rail references uninstalled tools: {sorted(missing)}")
    global_names = {name for name in names if catalog[name].global_internal}
    if global_names:
        raise ToolRailError(f"Domain rail cannot declare global internal tools: {sorted(global_names)}")


def effective_tool_rail(domain: Any, catalog: Mapping[str, ToolKind] | None = None) -> EffectiveToolRail:
    """Resolve Catalog + Global Core Tools + Domain Rail exactly once."""
    catalog = catalog if catalog is not None else installed_catalog()
    names = tuple(domain.rail_tools)
    validate_declared_rail(names, catalog)
    global_names = {name for name, kind in catalog.items() if kind.global_internal}
    allowed = frozenset(global_names | set(names))
    sources = frozenset(name for name in allowed if catalog[name].data_source)
    chat = tuple(name for name in catalog if name in allowed and catalog[name].chat)
    return EffectiveToolRail(frozenset(catalog), allowed, sources, chat)
