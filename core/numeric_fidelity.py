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


_NUMBER_REGEX = re.compile(
    r"(?<!\w)-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?"
    r"(?=[TBMKtbmk](?![a-zA-Z])|(?!\w))"
)
_TIME_UNIT_RE = re.compile(
    r"^[-\s]{0,3}"
    r"(?:h|hr|hrs|hour|hours|d|day|days|min|minute|minutes|"
    r"sec|second|seconds|wk|week|weeks|mo|month|months|yr|year|years)"
    r"(?![a-zA-Z])",
    re.IGNORECASE,
)

# Magnitude vocabulary — for scale normalisation. Words apply anywhere
# in the detail (the model doesn't usually chain multiple scales in one
# thesis). Single-letter suffixes count only when directly attached to
# the number ("$2.77T"), never as a bare word — otherwise a subject like
# "M2 money supply" would mis-scale.
_MAG_WORDS: tuple[tuple[str, float], ...] = (
    ("trillion", 1e12), ("billion", 1e9), ("million", 1e6), ("thousand", 1e3),
)
_MAG_SUFFIX: dict[str, float] = {"T": 1e12, "B": 1e9, "M": 1e6, "K": 1e3}
# Implicit-scale ladder — tried only when NO explicit word/suffix is
# present AND the resulting comparison lands within PASS tolerance.
# Cap: implicit scaling never justifies REWRITE (prevents widening the
# fabrication drop band).
_IMPLICIT_SCALES: tuple[float, ...] = (1e3, 1e6, 1e9, 1e12)


def _flag_enabled() -> bool:
    raw = os.environ.get("NUMERIC_GATE_ENABLED", "true").strip().lower()
    return raw not in ("false", "0", "no", "off")


@dataclass(frozen=True)
class FidelityAction:
    """One gate decision on one thesis."""
    action: str                 # "pass" | "rewrite" | "drop"
    # 2026-09-25: new reason `unverified_no_current_reference` — the
    # gate could not find the cited tool's output in the CURRENT-cycle
    # findings. Action is DROP (was `pass no_tool_output`): a citation
    # without a reference is unverifiable, and unverifiable must not
    # PASS. Session-report counts this in the NUMERIC FIDELITY line.
    reason: str                 # explanatory code (see MODULE docstring)
    subject: str
    tool: str | None
    cited_value: float | None
    true_value: float | None    # populated on REWRITE, else None
    corrected_detail: str | None  # populated on REWRITE


def _find_value_match(text: str) -> "re.Match[str] | None":
    """Return the first NON-WINDOW number match in text, or None.

    Single source of truth for citation-side scans. finditer walks
    every candidate; each is checked against the time-unit tail;
    windows are skipped and the search continues. None only when
    EVERY number in text is a window (or there are no numbers).
    """
    if not text:
        return None
    cleaned = text.replace(",", "")
    for m in _NUMBER_REGEX.finditer(cleaned):
        tail = cleaned[m.end() : m.end() + 10]
        if _TIME_UNIT_RE.match(tail):
            continue
        return m
    return None


def _all_numbers_in(text: str) -> list[float]:
    """All standalone numbers in text — same word-boundary rules as
    extract_reported_value so the regex used to CITE a number and the
    one used to SEARCH for it stay in lockstep. Time-window numbers
    ("24h", "24 hours") are skipped so a citation whose value happens
    to equal a duration doesn't spuriously match."""
    if not text:
        return []
    cleaned = text.replace(",", "")
    out: list[float] = []
    for m in _NUMBER_REGEX.finditer(cleaned):
        tail = cleaned[m.end() : m.end() + 10]
        if _TIME_UNIT_RE.match(tail):
            continue
        try:
            out.append(float(m.group(0)))
        except ValueError:
            continue
    return out


def _closest(candidates: list[float], target: float) -> float | None:
    if not candidates:
        return None
    return min(candidates, key=lambda v: abs(v - target))


def _looks_like_percentage(detail: str, number_str: str) -> bool:
    """A number is a percentage if a % sign appears within a few chars
    AFTER the numeric token. "change of -2.01%" → percent; "-2.01" in a
    market-cap thesis without % → not percent. Also treats a "%" anywhere
    in the detail as percentage-context; percentages must NEVER be scaled
    ("2.01%" is not 2.01e12)."""
    if "%" not in detail:
        return False
    # A % anywhere in the detail is enough — percentage-shaped theses
    # never legitimately mix with magnitude words in our corpus.
    return True


def _explicit_scale(detail: str, number_str: str) -> float:
    """Return the multiplier implied by a magnitude word or a single-
    letter suffix directly attached to `number_str`. 1.0 when none.
    Percentage-shaped details always return 1.0 (short-circuit)."""
    if _looks_like_percentage(detail, number_str):
        return 1.0
    lowered = detail.lower()
    for word, mult in _MAG_WORDS:
        if word in lowered:
            return mult
    # Suffix letter directly after the number (no space): "$2.77T".
    idx = detail.find(number_str)
    if idx >= 0:
        j = idx + len(number_str)
        if j < len(detail):
            ch = detail[j]
            if ch in _MAG_SUFFIX:
                return _MAG_SUFFIX[ch]
    return 1.0


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
            # 2026-09-25: was `pass no_tool_output`. An unverifiable
            # citation must NOT pass — findings_current is empty or
            # lacks the named tool → drop the thesis.
            return FidelityAction(
                "drop", "unverified_no_current_reference",
                subj, tool, cited, None, None,
            )
        candidates = _all_numbers_in(digest)
        if not candidates:
            # Only FAILED lines — the tool ran but yielded no data
            # this cycle. Same rule: unverifiable → drop.
            return FidelityAction(
                "drop", "unverified_no_current_reference",
                subj, tool, cited, None, None,
            )
        # Grab the ORIGINAL number-string as it appears in the detail —
        # needed by _explicit_scale to test the suffix-letter case.
        # Routed through _find_value_match so the same time-window
        # guard used by extract_reported_value governs this citation
        # path — no raw regex bypass (2026-09-17 audit).
        m = _find_value_match(detail)
        number_str = m.group(0) if m else str(cited)
        # 1) Bare-value comparison first.
        bare = cited
        closest = _closest(candidates, bare)
        if closest is None:
            return FidelityAction("pass", "no_candidates", subj, tool, cited, None, None)
        def _rel(a, b):
            return (abs(a - b) / abs(b)) if b else (0.0 if abs(a) < 1e-9 else float("inf"))
        rel = _rel(bare, closest)
        if rel <= _MATCH_TOLERANCE:
            return FidelityAction("pass", "exact", subj, tool, cited, closest, None)
        # 2) Explicit scale (word or suffix) — re-run the comparison at
        #    the scaled magnitude. If it now matches, PASS with a distinct
        #    reason so scale_normalised events are countable separately.
        mult = _explicit_scale(detail, number_str)
        if mult != 1.0:
            scaled = cited * mult
            closest2 = _closest(candidates, scaled) or closest
            rel2 = _rel(scaled, closest2)
            if rel2 <= _MATCH_TOLERANCE:
                return FidelityAction(
                    "pass", "scale_normalised", subj, tool, cited, closest2, None,
                )
            # In REWRITE band after scaling → rewrite with the true value.
            ratio2 = abs(scaled) / abs(closest2) if closest2 else float("inf")
            if _REWRITE_LOW <= ratio2 <= _REWRITE_HIGH:
                new_detail = _substitute_number(detail, cited, closest2)
                return FidelityAction(
                    "rewrite", "transcription_drift_scaled", subj, tool, cited,
                    closest2, new_detail,
                )
        # 3) Implicit scale — try 1e3/1e6/1e9/1e12; ONLY accept as PASS.
        #    Never used to justify REWRITE (would widen the fabrication
        #    band). Percentages short-circuit — they must never be scaled.
        if mult == 1.0 and not _looks_like_percentage(detail, number_str):
            for imp in _IMPLICIT_SCALES:
                scaled = cited * imp
                c = _closest(candidates, scaled) or closest
                if _rel(scaled, c) <= _MATCH_TOLERANCE:
                    return FidelityAction(
                        "pass", "scale_normalised_implicit", subj, tool, cited, c, None,
                    )
        # 4) Fall back to the bare-value REWRITE band (unchanged path).
        ratio = abs(cited) / abs(closest) if closest else float("inf")
        if _REWRITE_LOW <= ratio <= _REWRITE_HIGH:
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
    """Replace the first plausible standalone VALUE number (i.e. not a
    time-window) in `detail` with `new`, formatted at the same visual
    scale. If we can't find a value number (only windows or nothing),
    append a corrected marker rather than mutating unpredictably.

    Routed through _find_value_match so a REWRITE never rewrites a
    duration (e.g. "24" in "24-hour") into a market-cap value.
    """
    cleaned = detail.replace(",", "")
    m = _find_value_match(cleaned)
    if not m:
        return detail + f" [corrected: true value {new:g}]"
    if abs(new) < 1e-3 or abs(new) >= 1e6:
        new_str = f"{new:g}"
    else:
        new_str = f"{new:.6g}"
    # Substitute exactly the matched span (start:end) — safe because
    # _find_value_match returned that positional match on `cleaned`.
    return cleaned[: m.start()] + new_str + cleaned[m.end():]
