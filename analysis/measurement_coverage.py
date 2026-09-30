"""Warning-only coverage audit, using discovery metadata without running tools."""
from __future__ import annotations

from typing import Any
from core.domain import Domain, current_domain


FIELD_MAPS = ("rail_tool_fields", "field_phrases", "field_units", "field_contexts")
TOOL_MAPS = ("tool_served_phrases", "tool_sources", "source_cache_config")


def measurement_blind_spots(
    domain: Domain | None = None,
    tool_fields: dict[str, tuple[str, ...]] | None = None,
) -> list[dict[str, Any]]:
    """Find missing tool/field metadata, minus explicit per-map exemptions.

    Tool-level exemption keys are tool names; field-level keys are tool.field.
    Exemption values must be nonempty reasons. Nothing is executed or persisted.
    """
    domain = domain or current_domain()
    from core.tool_rail import effective_tool_rail
    active_sources = effective_tool_rail(domain).sources
    if tool_fields is None:
        from tools.discovery import discover_data_feed_tools
        from self_modify.digest_path import digest_field_names
        tool_fields = {
            cls.name: tuple(digest_field_names(getattr(cls, "digest_fields", ())))
            for cls in discover_data_feed_tools()
            if cls.name in active_sources and getattr(cls, "is_data_source", False)
        }
    else:
        tool_fields = {tool: fields for tool, fields in tool_fields.items() if tool in active_sources}
    exemptions = getattr(domain, "coverage_exemptions", {})
    out = []
    for map_name in FIELD_MAPS + TOOL_MAPS:
        mapping = getattr(domain, map_name, {})
        exempt = exemptions.get(map_name, {})
        for tool, fields in sorted(tool_fields.items()):
            if exempt.get(tool):
                continue
            if map_name in FIELD_MAPS:
                required = {f for f in fields if not exempt.get(f"{tool}.{f}")}
                available = mapping.get(tool, {})
                covered = set(available) if not isinstance(available, dict) else {k for k, v in available.items() if v}
                missing = sorted(required - covered)
                if missing or (tool not in mapping and not fields):
                    out.append({"map": map_name, "tool": tool, "fields": missing, "severity": "warning"})
            elif not mapping.get(tool):
                out.append({"map": map_name, "tool": tool, "fields": [], "severity": "warning"})
    for tool, identity in sorted(getattr(domain, "tool_sources", {}).items()):
        if tool in tool_fields and not getattr(domain, "source_aliases", {}).get(identity):
            if not exemptions.get("source_aliases", {}).get(tool):
                out.append({"map": "source_aliases", "tool": tool, "fields": [], "severity": "warning"})
    return out


def render_blind_spots(spots: list[dict[str, Any]]) -> list[str]:
    """Render only tool/field identities, never payloads or configuration values."""
    return [f"  WARN measurement coverage: {s['map']} {s['tool']}"
            + (f" missing {', '.join(s['fields'])}" if s['fields'] else " missing tool metadata")
            for s in spots]
