"""Zones — the wall that constrains Morgoth-originated self-modify proposals.

These rules apply ONLY to proposals flowing through
``self_modify.gates.run_pipeline``. They do NOT constrain normal
human-driven development (Claude Code editing any file as usual).

Zones
-----
RED    Immutable to the self-modify pipeline: the engine, its guardrails,
       the tests that gate them, and secrets. An edit or new_file anywhere
       under a RED path is refused.

ORANGE Approval-gated with extra ceremony. Currently EMPTY — reserved for
       a future step where cycle prompts / tunable params get externalized
       to a config path that Morgoth may propose to change.

GREEN  Additive-only. NEW files under ``tools/data_feeds/`` are green
       (a new provider tool). An EDIT to an existing green-dir file is
       NOT green — edits go to red.

DEFAULT DENY
------------
``classify_proposal`` returns ``"red"`` unless the (path, change_type)
combination matches an explicit green or orange rule. New rules must be
added deliberately; the safe outcome for anything unrecognized is refusal.
"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Literal

Zone = Literal["red", "orange", "green"]
ChangeType = Literal["new_file", "edit"]

# --- Documented protection surface ------------------------------------------
# RED_ZONE_GLOBS is documentation for humans: what MUST be protected. The
# classifier itself relies on DEFAULT DENY, so this list does not need to be
# consulted at runtime — anything not explicitly green or orange is red.
RED_ZONE_GLOBS: tuple[str, ...] = (
    "core/**",
    "self_modify/**",
    "tests/**",
    "main.py",
    "api/**",
    "memory/**",
    "scripts/**",
    "IDENTITY.md",
    ".env",
    "requirements*.txt",
    ".git/**",
)

# GREEN: NEW files strictly under this directory (path traversal blocked).
GREEN_NEW_FILE_PREFIX_PARTS: tuple[str, ...] = ("tools", "data_feeds")

# ORANGE is currently empty; declaration kept for future rules.
ORANGE_RULES: tuple = ()


def classify_proposal(target_path: str, change_type: ChangeType) -> Zone:
    """Classify a proposal into ``red`` / ``orange`` / ``green``.

    Pure function — no I/O. Default deny: any (path, change_type) that does
    not match an explicit green or orange rule returns ``red``. Path
    traversal (``..``) and absolute paths are automatically red.
    """
    if change_type not in ("new_file", "edit"):
        return "red"

    normalized = (target_path or "").strip()
    if not normalized:
        return "red"

    path = PurePosixPath(normalized)
    parts = path.parts
    if not parts or parts[0].startswith("/") or ".." in parts or "." in parts:
        return "red"

    # ORANGE: empty. No rule can match.

    # GREEN: new file strictly inside tools/data_feeds/.
    green_prefix = GREEN_NEW_FILE_PREFIX_PARTS
    if (
        change_type == "new_file"
        and len(parts) > len(green_prefix)
        and parts[: len(green_prefix)] == green_prefix
    ):
        return "green"

    return "red"
