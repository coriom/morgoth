"""Codex 0.162.0/0.162.1 startup templates; attribution only, never a gate bypass.

Sources are pinned in docs/CODEX_PROVIDER.md. JSONL does not preserve the
originating notification variant, so a match establishes a probable family.
"""
from __future__ import annotations

import re

from scripts.diagnose_codex_cli import classify_events


STARTUP_CODES = frozenset({
    "CODEX_STARTUP_CONFIG_WARNING", "CODEX_STARTUP_SKILL_WARNING",
    "CODEX_STARTUP_DEPRECATION", "CODEX_STARTUP_STATE_WARNING",
    "CODEX_STARTUP_RUNTIME_WARNING", "CODEX_STARTUP_UNKNOWN",
})

# Each prefix or sentence is copied from reviewed tagged startup producers.
# Skill-loader scan errors are logged, not proven to reach JSONL, so the skill
# category intentionally has no pattern. Generic topic words are insufficient.
TEMPLATES: dict[str, tuple[re.Pattern[str], ...]] = {
    "CODEX_STARTUP_CONFIG_WARNING": (
        re.compile(r"Under-development features enabled: [a-z0-9_]+(?:, [a-z0-9_]+)*\. Under-development features are incomplete and may behave unpredictably\. To suppress this warning, set `suppress_unstable_features_warning = true` in [^\n]+\."),
        re.compile(r"Codex is ignoring [1-9][0-9]* unrecognized configuration settings?\. Check for typos or deprecated settings\."),
        re.compile(r"Configured value for `[a-z_]+` is disallowed by requirements; falling back to required value [^\n]+"),
        re.compile(r"Project-local config, hooks, and exec policies are disabled in the following folders until the project is trusted, but skills still load\."),
    ),
    "CODEX_STARTUP_SKILL_WARNING": (),
    "CODEX_STARTUP_DEPRECATION": (
        re.compile(r"`\[features\]\.transcript_v2` is deprecated and ignored\."),
        re.compile(r"`\[features\.guardianv2\]\.thread_context` is deprecated and ignored\."),
        re.compile(r"`\[features\]\.web_search_(?:request|cached)` is deprecated because web search is enabled by default\."),
    ),
    "CODEX_STARTUP_STATE_WARNING": (
        re.compile(r"Codex rebuilt its local database(?: \([^\n]*\))?"),
    ),
    "CODEX_STARTUP_RUNTIME_WARNING": (
        re.compile(r"Codex's Linux sandbox uses bubblewrap and needs access to create user namespaces\."),
        re.compile(r"Codex's Linux sandbox uses bubblewrap, which is not supported on WSL1 because WSL1 cannot create the required user namespaces\. Use WSL2 for sandboxed shell commands\."),
        re.compile(r"Codex could not find bubblewrap on PATH\. Install bubblewrap with your OS package manager\. See the sandbox prerequisites: https://developers\.openai\.com/codex/concepts/sandboxing#prerequisites\. Codex will use the bundled bubblewrap in the meantime\."),
        re.compile(r"in-process app-server event stream lagged; dropped [1-9][0-9]* events"),
    ),
}


def classify_startup_warning(message: object) -> str:
    """Classify a private message only on one exact source-derived template."""
    if not isinstance(message, str) or len(message) > 8192:
        return "CODEX_STARTUP_UNKNOWN"
    matches = {code for code, patterns in TEMPLATES.items()
               if any(pattern.fullmatch(message) for pattern in patterns)}
    return next(iter(matches)) if len(matches) == 1 else "CODEX_STARTUP_UNKNOWN"


def reduce_startup_events(stdout: str, returncode: int, final_text: str,
                          marker: str, marker_unchanged: bool,
                          write_observed: bool) -> dict[str, object]:
    """Return only bounded event metadata from private JSONL in memory."""
    report = classify_events(stdout, returncode, final_text, marker,
                             marker_unchanged, write_observed,
                             classifier=classify_startup_warning,
                             startup_only=True)
    report["verdict"] = startup_verdict(report)
    return report


def startup_verdict(report: dict[str, object]) -> str:
    """Keep attribution distinct from workload qualification."""
    if report["forbidden_effect_observed"]:
        return "SECURITY_FAIL"
    if (not report["process_exited_successfully"] or not report["turn_completed"]
            or not report["final_assistant_present"]
            or not report["expected_canary_response"]
            or report["forbidden_tool_event_count"] or report["unknown_event_count"]
            or report["diagnostic_count"] != len(report["diagnostics"])):
        return "BLOCKED_CODEX_STARTUP_UNKNOWN"
    codes = {row["code"] for row in report["diagnostics"]}  # type: ignore[index]
    if not codes:
        return "CODEX_CANARY_CLEAN"
    if "CODEX_STARTUP_UNKNOWN" in codes:
        return "BLOCKED_CODEX_STARTUP_UNKNOWN"
    return "CODEX_STARTUP_DIAGNOSTIC_ATTRIBUTED"
