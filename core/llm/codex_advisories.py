"""Exact, reviewed Codex 0.162.1 advisory messages; no topic matching.

JSONL loses the originating notification variant. These strings identify a
source template, not a structurally authenticated notification origin.
"""
from __future__ import annotations

CODE_MODE_DISABLED = (
    "Code Mode is unavailable because code-mode host is disabled. "
    "Code mode will fail closed; enable `features.code_mode_host` and install "
    "`codex-code-mode-host`."
)
# The pinned session startup code joins the fixed Bubblewrap CODEX_HOME with
# CONFIG_TOML_FILE. Only skip_host_skill_discovery is enabled in our argv.
SKIP_HOST_SKILL_DISCOVERY = (
    "Under-development features enabled: skip_host_skill_discovery. "
    "Under-development features are incomplete and may behave unpredictably. "
    "To suppress this warning, set `suppress_unstable_features_warning = true` "
    "in /home/codex/.codex/config.toml."
)
REVIEWED_ADVISORIES = frozenset((CODE_MODE_DISABLED, SKIP_HOST_SKILL_DISCOVERY))
