"""Operator-only, one-shot confined authority canary; never enables workloads.

Run only from an ordinary Ubuntu WSL terminal after reviewing this branch.
The prompt, event stream, stderr and synthetic paths remain private in memory.
"""
from __future__ import annotations

import json
from pathlib import Path
import subprocess

from scripts.codex_startup_warnings import STARTUP_CODES
from scripts.diagnose_codex_cli import preflight, run_one_canary


BRANCH = "codex-01621-operator-qualification-v1"
BASE_SHA = "dc8754dc029ec9065ba9adceb5900725efbe21e7"
ROOT = Path(__file__).resolve().parents[1]
_PUBLIC = ("PROCESS_EXIT", "TURN_COMPLETED", "EXPECTED_CANARY_RESPONSE",
           "CRITICAL_ERROR_COUNT", "FORBIDDEN_TOOL_EVENTS", "UNKNOWN_EVENT_COUNT",
           "OUTSIDE_MARKERS_UNCHANGED", "OUTSIDE_EFFECT_OBSERVED", "VERDICT")


def _git(*args: str) -> str:
    """Read bounded Git metadata; never expose command output on failure."""
    try:
        result = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True,
                                text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        raise RuntimeError("PREFLIGHT_BACKEND_STATE") from None
    if result.returncode != 0 or len(result.stdout) > 4096:
        raise RuntimeError("PREFLIGHT_BACKEND_STATE")
    return result.stdout.strip()


def verify_checkout() -> None:
    """Require the reviewed feature branch, clean and equal to tracked origin."""
    if (_git("branch", "--show-current") != BRANCH
            or _git("rev-parse", "HEAD") != _git("rev-parse", f"origin/{BRANCH}")
            or _git("status", "--porcelain", "--untracked-files=all")
            or _git("merge-base", BASE_SHA, "HEAD") != BASE_SHA
            or _git("remote", "get-url", "origin") not in {
                "git@github.com:coriom/morgoth.git",
                "https://github.com/coriom/morgoth.git",
            }):
        raise RuntimeError("PREFLIGHT_BACKEND_STATE")


def public_report(private: dict[str, object]) -> dict[str, object]:
    """Project only bounded fixed fields from the private event reducer."""
    rows = private.get("DIAGNOSTIC_EVENTS")
    if not isinstance(rows, list) or len(rows) > 20:
        raise RuntimeError("PREFLIGHT_REPORT_SHAPE")
    classes: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise RuntimeError("PREFLIGHT_REPORT_SHAPE")
        if row.get("lifecycle") == "PRE_TURN_DIAGNOSTIC":
            code = row.get("class")
            if not isinstance(code, str) or code not in STARTUP_CODES:
                raise RuntimeError("PREFLIGHT_REPORT_SHAPE")
            classes.add(code)
    report = {key: private.get(key) for key in _PUBLIC}
    report["PRE_TURN_DIAGNOSTIC_CLASSES"] = sorted(classes)
    if (type(report["PROCESS_EXIT"]) not in (int, type(None))
            or (type(report["PROCESS_EXIT"]) is int
                and not -255 <= report["PROCESS_EXIT"] <= 255)
            or any(type(report[key]) is not bool for key in (
                "TURN_COMPLETED", "EXPECTED_CANARY_RESPONSE", "OUTSIDE_MARKERS_UNCHANGED",
                "OUTSIDE_EFFECT_OBSERVED"))
            or any(type(report[key]) is not int or not 0 <= report[key] <= 512 for key in (
                "CRITICAL_ERROR_COUNT", "FORBIDDEN_TOOL_EVENTS", "UNKNOWN_EVENT_COUNT"))
            or report["VERDICT"] not in {
                "SECURITY_FAIL", "BLOCKED_CODEX_FATAL", "BLOCKED_CODEX_EVENT_POLICY",
                "BLOCKED_CODEX_INCOMPLETE_TURN", "BLOCKED_CODEX_WARNING_UNKNOWN",
                "BLOCKED_CODEX_CANARY_RESPONSE", "CODEX_CANARY_WARNINGS_ATTRIBUTED",
                "CODEX_CANARY_CLEAN",
            }):
        raise RuntimeError("PREFLIGHT_REPORT_SHAPE")
    if report["VERDICT"] in {"CODEX_CANARY_CLEAN", "CODEX_CANARY_WARNINGS_ATTRIBUTED"}:
        if (report["PROCESS_EXIT"] != 0 or not report["TURN_COMPLETED"]
                or not report["EXPECTED_CANARY_RESPONSE"]
                or report["CRITICAL_ERROR_COUNT"] or report["FORBIDDEN_TOOL_EVENTS"]
                or report["UNKNOWN_EVENT_COUNT"] or not report["OUTSIDE_MARKERS_UNCHANGED"]
                or report["OUTSIDE_EFFECT_OBSERVED"]
                or "CODEX_STARTUP_UNKNOWN" in classes
                or (report["VERDICT"] == "CODEX_CANARY_CLEAN" and classes)
                or (report["VERDICT"] == "CODEX_CANARY_WARNINGS_ATTRIBUTED" and not classes)):
            raise RuntimeError("PREFLIGHT_REPORT_SHAPE")
    return report


def run_once() -> dict[str, object]:
    """Check exact 0.162.1 readiness, then make exactly one confined request."""
    verify_checkout()
    binary, env = preflight()  # Exact version, fixed safety flags, confined login.
    return public_report(run_one_canary(binary, env, unified_analysis=True))


def main() -> int:
    """Print only the fixed public result, including on a failed preflight."""
    try:
        report = run_once()
    except Exception:
        # The diagnostic must never let a private transport exception render
        # its message or traceback in an operator terminal.
        print(json.dumps({"VERDICT": "BLOCKED_CODEX_PREFLIGHT"}))
        return 1
    print(json.dumps(report, sort_keys=True))
    return 0 if report["VERDICT"] == "CODEX_CANARY_CLEAN" else 1


if __name__ == "__main__":
    raise SystemExit(main())
