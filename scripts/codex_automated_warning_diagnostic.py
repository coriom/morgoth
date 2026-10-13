"""One non-disclosing, confined Codex text diagnostic for operator-native use.

This independent entry point never opens a TTY or reveals private warnings.
It does not change the human-only review workflow or qualify workloads.
"""
from __future__ import annotations

import argparse
import json

from scripts import codex_local_warning_review as review


BRANCH = "codex-01621-autonomous-warning-resolution-v1"
BASE_SHA = "408e75cf66fc45e090977dca0e32a0b01a3089f4"


def verify_checkout() -> None:
    """Require the exact clean feature branch, tracked origin and base."""
    if (review._git("branch", "--show-current") != BRANCH
            or review._git("rev-parse", "HEAD") !=
            review._git("rev-parse", f"origin/{BRANCH}")
            or review._git("status", "--porcelain", "--untracked-files=all")
            or review._git("merge-base", BASE_SHA, "HEAD") != BASE_SHA
            or review._git("remote", "get-url", "origin") not in {
                "git@github.com:coriom/morgoth.git",
                "https://github.com/coriom/morgoth.git",
            }):
        raise review.ReviewUnavailable("PREFLIGHT_CHECKOUT")


def run_once() -> dict[str, object]:
    """Run at most one fixed probe and export fixed, bounded metadata only."""
    verify_checkout()
    binary, env = review.preflight()
    verify_checkout()
    private, _unknown_messages = review.run_one_text_request(binary, env)
    public = review.public_report(private)
    return {
        "TEXT_TRANSPORT_RESULT": public["TEXT_TRANSPORT_RESULT"],
        "PROCESS_EXIT": public["PROCESS_EXIT"],
        "TURN_COMPLETED": public["TURN_COMPLETED"],
        "PRE_TURN_DIAGNOSTIC_COUNT": public["PRE_TURN_DIAGNOSTIC_COUNT"],
        "EXACT_SOURCE_TEMPLATE_CLASSES": public["EXACT_SOURCE_TEMPLATE_CLASSES"],
        "UNKNOWN_DIAGNOSTIC_COUNT": public["UNKNOWN_DIAGNOSTIC_COUNT"],
        "FORBIDDEN_TOOL_EVENTS": public["FORBIDDEN_TOOL_EVENTS"],
        "CRITICAL_ERROR_COUNT": public["CRITICAL_ERROR_COUNT"],
        "UNKNOWN_EVENT_COUNT": public["UNKNOWN_EVENT_COUNT"],
        "VERDICT": public["VERDICT"],
        "PRODUCTION_QUALIFIED": False,
    }


def main(argv: list[str] | None = None) -> int:
    """Require fixed opt-in; errors are reduced to fixed prerequisite codes."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--one-synthetic-probe", action="store_true", required=True)
    args = parser.parse_args(argv)
    if not args.one_synthetic_probe:
        return 2
    try:
        report = run_once()
    except BaseException as exc:
        code = exc.code if isinstance(exc, review.ReviewUnavailable) else "PREFLIGHT_FAILED"
        report = {"VERDICT": code, "PRODUCTION_QUALIFIED": False}
    print(json.dumps(report, sort_keys=True))
    return 0 if report["VERDICT"] == "BLOCKED_SEPARATE_SECURITY_REVIEW" else 1


if __name__ == "__main__":
    raise SystemExit(main())
