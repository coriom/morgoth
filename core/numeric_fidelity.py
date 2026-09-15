"""Numeric-fidelity gate for extracted theses.

Proven defect: the extractor mis-transcribes small-magnitude numbers.
On btc_funding the model reports a "clean" 0.00010000 when the tool
returned ~8e-5 or ~5.4e-5; 5 of 9 empirical VALUE-track misses collapse
to EXACTLY 1e-4 despite the tool never producing that value.

This gate runs at extraction time — deterministically. For every
number the model cited in evidence[].detail we look for a matching
value inside the SAME tool's raw output for THIS objective. Three
outcomes per thesis:

  · PASS     — every cited number is present (±1%) in the tool output,
               OR nothing was checkable (no number, no tool digest, or
               the number is a computed quantity that legitimately
               isn't a raw tool value).
  · REWRITE  — a cited number is off but the tool output has a value in
               the same order of magnitude (ratio 0.5-2x). We replace
               the cited number with the true value. Default action
               because a rewrite preserves the SUBJECT / CLAIM that the
               model correctly identified — only the transcription
               drifted. Dropping instead would discard signal.
  · DROP     — a cited number matches NOTHING even loosely in the tool
               output (ratio outside 0.5-2x AND not order-of-magnitude
               close). Safer to lose the thesis than to publish a
               fabricated datum.

Kill-switch NUMERIC_GATE_ENABLED (default true) skips the whole path.
Every action is persisted (subject, tool, cited, true, action, reason)
so `morgoth session-report` can surface the correction rate. An
invisible correction would hide the very defect being measured.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any

# Reuse the extractor from the descriptive backtest — one regex, one
# behaviour. The gate uses it BOTH on evidence[].detail (the cited
# number) AND on the tool digest (the source of truth). If we ever
# tune the regex, both call sites must move together.
from analysis.thesis_backtest_descriptive import extract_reported_value


# Tolerance for a MATCH — this is transcription, not estimation. 1 %
# admits float-rounding-in-display without admitting "0.00010000 vs
# 0.00008037" (which is 24 % off).
_MATCH_TOLERANCE = 0.01
# REWRITE band — the tool output value must be within the same order
# of magnitude for a rewrite to be safe. Outside this band → DROP.
_REWRITE_LOW = 0.5
_REWRITE_HIGH = 2.0


_NUMBER_REGEX = re.compile(r"(?<!\w)-?\d+(?:\.\d+)?(?![a-zA-Z])")


def _flag_enabled() -> bool:
    raw = os.environ.get("NUMERIC_GATE_ENABLED", "true").strip().lower()
    return raw not in ("false", "0", "no", "off")


@dataclass(frozen=True)
class FidelityAction:
    """One gate decision on one thesis."""
    action: str                 # "pass" | "rewrite" | "drop"
    reason: str                 # explanatory code (see MODULE docstring)
    subject: str
    tool: str | None
    cited_value: float | None
    true_value: float | None    # populated on REWRITE, else None
    corrected_detail: str | None  # populated on REWRITE


def _all_numbers_in(text: str) -> list[float]:
    """All standalone numbers in text — same word-boundary rules as
    extract_reported_value so the regex used to CITE a number and the
    one used to SEARCH for it stay in lockstep."""
    if not text:
        return []
    out: list[float] = []
    for m in _NUMBER_REGEX.finditer(text.replace(",", "")):
        try:
            out.append(float(m.group(0)))
        except ValueError:
            continue
    return out


def _closest(candidates: list[float], target: float) -> float | None:
    if not candidates:
        return None
    return min(candidates, key=lambda v: abs(v - target))


def _tool_digest_for(source: str, findings: list[str]) -> str:
    """Return concatenated raw output text for `source` across cycles.

    Findings are the ``_format_cycle_finding`` blobs — each starts with
    "TOOL RESULTS:\n- <tool>: <payload>\n- <tool2>: <payload>\n...".
    We extract every payload line whose leading token matches `source`
    and glue them together. FAILED lines are included (a failure means
    we have no value from the tool for that cycle — the gate then
    sees no candidates, which correctly forces DROP for a fabricated
    number and PASS-with-reason for an unverifiable one).
    """
    if not source or not findings:
        return ""
    prefix = f"- {source}:"
    prefix_fail = f"- {source} FAILED:"
    lines: list[str] = []
    for blob in findings:
        for ln in (blob or "").splitlines():
            ln = ln.rstrip()
            if ln.startswith(prefix) or ln.startswith(prefix_fail):
                lines.append(ln)
    return "\n".join(lines)


def check_thesis(
    thesis: dict[str, Any], findings: list[str],
) -> FidelityAction:
    """Apply the fidelity gate to a single thesis.

    Contract:
      · Every evidence[] entry is checked; the FIRST decisive outcome
        drives the whole thesis. This matches how the VALUE-track
        scorer already reads the first extractable number.
      · Ambiguity (no number, no tool digest, tool output empty of
        numbers) → PASS with a reason code.
      · REWRITE-vs-DROP: REWRITE when the tool output contains a value
        in the 0.5x-2x band around the cited number (transcription
        drift, subject/claim still valid); DROP when nothing in the
        tool output is remotely close (fabrication).
    """
    subj = str(thesis.get("subject", ""))
    ev_list = thesis.get("evidence") or []
    if not ev_list:
        return FidelityAction("pass", "no_evidence", subj, None, None, None, None)
    for e in ev_list:
        if not isinstance(e, dict):
            continue
        tool = str(e.get("source", "")).strip() or None
        detail = str(e.get("detail", ""))
        cited = extract_reported_value([e])
        if cited is None:
            # No verifiable number in this evidence entry — move on;
            # a later entry may still have one. If none do, we fall
            # through to the PASS branch below.
            continue
        if not tool:
            return FidelityAction("pass", "no_tool_named", subj, None, cited, None, None)
        digest = _tool_digest_for(tool, findings)
        if not digest:
            return FidelityAction("pass", "no_tool_output", subj, tool, cited, None, None)
        candidates = _all_numbers_in(digest)
        if not candidates:
            # Only FAILED lines — no numeric candidate to match against.
            return FidelityAction("pass", "no_candidates", subj, tool, cited, None, None)
        closest = _closest(candidates, cited)
        if closest is None:
            return FidelityAction("pass", "no_candidates", subj, tool, cited, None, None)
        # Exact-ish match — transcription is faithful.
        if closest == 0:
            if abs(cited) < 1e-9:
                return FidelityAction("pass", "exact", subj, tool, cited, closest, None)
        else:
            rel = abs(cited - closest) / abs(closest)
            if rel <= _MATCH_TOLERANCE:
                return FidelityAction("pass", "exact", subj, tool, cited, closest, None)
            ratio = abs(cited) / abs(closest) if closest else float("inf")
            if _REWRITE_LOW <= ratio <= _REWRITE_HIGH:
                # Same-order-of-magnitude drift → rewrite the detail
                # with the true value. Use repr-style formatting to
                # keep small numbers readable.
                new_detail = _substitute_number(detail, cited, closest)
                return FidelityAction(
                    "rewrite", "transcription_drift", subj, tool, cited,
                    closest, new_detail,
                )
        # No cited number matched → look at other evidence entries too;
        # if none of them match either, DROP after the loop.
    # If we reach here, we had ≥1 cited number and no evidence entry
    # matched exact-or-rewrite. Loop again to construct the DROP with
    # the first cited number as the reference.
    for e in ev_list:
        if not isinstance(e, dict):
            continue
        cited = extract_reported_value([e])
        if cited is None:
            continue
        tool = str(e.get("source", "")).strip() or None
        return FidelityAction(
            "drop", "no_match_in_tool_output", subj, tool, cited, None, None,
        )
    return FidelityAction("pass", "no_number_in_detail", subj, None, None, None, None)


def _substitute_number(detail: str, old: float, new: float) -> str:
    """Replace the first plausible standalone number in `detail` with
    `new`, formatted at the same visual scale. If we can't find the
    exact `old` value in the string (float rounding), fall back to
    appending a corrected marker rather than mutating unpredictably."""
    old_pattern = re.compile(r"(?<!\w)-?\d+(?:\.\d+)?(?!\w)")
    m = old_pattern.search(detail.replace(",", ""))
    if not m:
        return detail + f" [corrected: true value {new:g}]"
    # Format with enough precision to preserve significant digits.
    if abs(new) < 1e-3 or abs(new) >= 1e6:
        new_str = f"{new:g}"
    else:
        new_str = f"{new:.6g}"
    return old_pattern.sub(new_str, detail.replace(",", ""), count=1)
