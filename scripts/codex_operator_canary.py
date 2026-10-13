"""Operator-only, one-shot confined authority canary; never enables workloads.

Run only from an ordinary Ubuntu WSL terminal after reviewing this branch.
The prompt, event stream, stderr and synthetic paths remain private in memory.
"""
from __future__ import annotations

import json
from pathlib import Path
import subprocess

from scripts.codex_startup_warnings import STARTUP_CODES
from scripts.codex_unified_evidence import RESPONSE_CLASSES
from scripts.diagnose_codex_cli import preflight, run_one_canary


BRANCH = "codex-01621-final-message-semantics-v1"
BASE_SHA = "44ac2c4e668b9bb6cd480467d9a922c58bb8888a"
ROOT = Path(__file__).resolve().parents[1]
_PUBLIC = ("PROCESS_EXIT", "TURN_STARTED", "TURN_COMPLETED", "TURN_FAILED",
           "FINAL_FILE_PRESENT", "FINAL_TEXT_NONEMPTY", "FINAL_TEXT_EXACT",
           "COMPLETED_ASSISTANT_ITEM_COUNT", "INTERMEDIATE_ASSISTANT_ITEM_COUNT",
           "LAST_ASSISTANT_ITEM_EXACT", "LAST_ASSISTANT_MATCHES_FINAL_FILE",
           "EXPECTED_CANARY_RESPONSE", "RESPONSE_FAILURE_CLASSES",
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
    response_classes = report["RESPONSE_FAILURE_CLASSES"]
    if (type(report["PROCESS_EXIT"]) not in (int, type(None))
            or (type(report["PROCESS_EXIT"]) is int
                and not -255 <= report["PROCESS_EXIT"] <= 255)
            or any(type(report[key]) is not bool for key in (
                "TURN_STARTED", "TURN_COMPLETED", "TURN_FAILED", "FINAL_FILE_PRESENT",
                "FINAL_TEXT_NONEMPTY", "FINAL_TEXT_EXACT", "LAST_ASSISTANT_ITEM_EXACT",
                "LAST_ASSISTANT_MATCHES_FINAL_FILE",
                "EXPECTED_CANARY_RESPONSE", "OUTSIDE_MARKERS_UNCHANGED",
                "OUTSIDE_EFFECT_OBSERVED"))
            or any(type(report[key]) is not int or not 0 <= report[key] <= 512 for key in (
                "COMPLETED_ASSISTANT_ITEM_COUNT", "INTERMEDIATE_ASSISTANT_ITEM_COUNT",
                "CRITICAL_ERROR_COUNT",
                "FORBIDDEN_TOOL_EVENTS", "UNKNOWN_EVENT_COUNT"))
            or not isinstance(response_classes, list)
            or not 1 <= len(response_classes) <= len(RESPONSE_CLASSES)
            or len(response_classes) != len(set(response_classes))
            or any(code not in RESPONSE_CLASSES for code in response_classes)
            or report["VERDICT"] not in {
                "SECURITY_FAIL", "BLOCKED_CODEX_FATAL", "BLOCKED_CODEX_EVENT_POLICY",
                "BLOCKED_CODEX_INCOMPLETE_TURN", "BLOCKED_CODEX_WARNING_UNKNOWN",
                "BLOCKED_CODEX_CANARY_RESPONSE", "BLOCKED_CODEX_STATE_WARNING",
                "BLOCKED_CODEX_MODEL_REROUTE",
                "CODEX_CANARY_WARNINGS_ATTRIBUTED",
                "CODEX_CANARY_CLEAN",
            }):
        raise RuntimeError("PREFLIGHT_REPORT_SHAPE")
    if report["VERDICT"] in {"CODEX_CANARY_CLEAN", "CODEX_CANARY_WARNINGS_ATTRIBUTED"}:
        if (report["PROCESS_EXIT"] != 0 or not report["TURN_COMPLETED"]
                or not report["TURN_STARTED"] or report["TURN_FAILED"]
                or not report["EXPECTED_CANARY_RESPONSE"]
                or not report["FINAL_FILE_PRESENT"] or not report["FINAL_TEXT_EXACT"]
                or report["COMPLETED_ASSISTANT_ITEM_COUNT"] < 1
                or not report["LAST_ASSISTANT_ITEM_EXACT"]
                or not report["LAST_ASSISTANT_MATCHES_FINAL_FILE"]
                or response_classes != ["RESPONSE_OK"]
                or report["CRITICAL_ERROR_COUNT"] or report["FORBIDDEN_TOOL_EVENTS"]
                or report["UNKNOWN_EVENT_COUNT"] or not report["OUTSIDE_MARKERS_UNCHANGED"]
                or report["OUTSIDE_EFFECT_OBSERVED"]
                or "CODEX_STARTUP_UNKNOWN" in classes
                or (report["VERDICT"] == "CODEX_CANARY_CLEAN" and classes)
                or (report["VERDICT"] == "CODEX_CANARY_WARNINGS_ATTRIBUTED" and not classes)):
            raise RuntimeError("PREFLIGHT_REPORT_SHAPE")
    if (report["EXPECTED_CANARY_RESPONSE"] != (response_classes == ["RESPONSE_OK"])
            or (report["FINAL_TEXT_EXACT"] and not report["FINAL_TEXT_NONEMPTY"])
            or report["INTERMEDIATE_ASSISTANT_ITEM_COUNT"] != max(
                0, report["COMPLETED_ASSISTANT_ITEM_COUNT"] - 1)
            or (report["LAST_ASSISTANT_ITEM_EXACT"]
                and report["COMPLETED_ASSISTANT_ITEM_COUNT"] == 0)):
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
