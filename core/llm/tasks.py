"""Enumerated LLM tasks — with explicit defaults and operator overrides.

Adding a task here means:
  · pick the current provider it uses today (that becomes the default),
  · pick a MORGOTH_LLM_<UPPER> env alias,
  · add a route call site that resolves through the registry.

Existing THESIS_GENERATOR env is honored as an ALIAS of
MORGOTH_LLM_THESIS so the earlier experiment script keeps working.
"""

from __future__ import annotations

# Task name constants — passed to registry.resolve().
THESIS = "thesis"          # brain._extract_theses  (default: codex-cli)
SYNTHESIS = "synthesis"    # brain._synthesize_objective (default: codex-cli)
CHAT = "chat"              # brain.process_message + tool loop (default: ollama)
REFLECT = "reflect"        # self_modify.reflect (default: codex-cli)
SHADOW = "shadow"          # self_modify.shadow (default: codex-cli)
SCOUT = "scout"            # self_modify.scout (default: codex-cli — reserved)

# High-quality tasks use Codex; chat remains local. Claude is explicit rollback.
# "default" as model means "provider picks its own default"
# (ollama uses config.primary_model; claude-cli/api use their own default).
DEFAULTS: dict[str, str] = {
    THESIS: "codex-cli:default",
    SYNTHESIS: "codex-cli:default",
    CHAT: "ollama:default",
    REFLECT: "codex-cli:default",
    SHADOW: "codex-cli:default",
    SCOUT: "codex-cli:default",
}

# Environment aliases the operator may set. Presence of the primary env
# (MORGOTH_LLM_<task>) wins; if only the legacy alias is set, it's used.
LEGACY_ALIASES: dict[str, tuple[str, ...]] = {
    THESIS: ("THESIS_GENERATOR",),
}


def all_tasks() -> tuple[str, ...]:
    return tuple(DEFAULTS.keys())
