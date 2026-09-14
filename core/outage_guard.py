"""Network-outage guard for the autonomous cycle loop.

The 8B research loop cannot recognise a systemic network outage on its
own: a ten-minute DNS gap costs 5 cycles per objective because each
cycle just retries the same failing tools. This module supplies the
classifier + trigger predicates the cycle loop uses to stop early and
requeue the objective.

Contract:
  · A tool failure is NETWORK when its error text matches one of the
    documented patterns (DNS, connection refused/reset, timeout,
    unreachable). Everything else (4xx/5xx, KeyError from a missing
    required arg, parse errors) is NOT network and MUST NOT trigger
    the guard — a malformed tool call is a model problem, not an
    outage.
  · An outage cycle is one where the model made ≥1 tool call, EVERY
    call failed, and EVERY failure was NETWORK. Zero-call cycles are
    NOT outage (nothing to classify).
  · Consecutive outage cycles are counted per-objective; the counter
    resets on ANY non-outage cycle.
"""

from __future__ import annotations

import os
from typing import Any, Iterable


# Case-insensitive substring markers. Chosen from the actual error
# strings observed in the DNS outage that motivated this guard
# ("[Errno -3] Temporary failure in name resolution", plus the
# adjacent failure modes ConnectionError families raise). No regex —
# substring match is deterministic, cheap, and readable.
_NETWORK_PATTERNS: tuple[str, ...] = (
    "temporary failure in name resolution",
    "name or service not known",
    "no address associated with hostname",
    "connection refused",
    "connection reset",
    "connection aborted",
    "connect timeout",
    "read timeout",
    "network is unreachable",
    "no route to host",
    "getaddrinfo failed",
    "nodename nor servname",
    "operation timed out",
)


# Number of consecutive all-network-failure cycles that trigger the
# per-objective abort. Two is deliberately low: a 10-minute DNS gap
# spans ~1-2 cycles at the default 10-minute cadence, and we would
# rather requeue one salvageable objective than burn all 5 slots.
# Env override MORGOTH_OUTAGE_ABORT_CYCLES for operator tuning.
def outage_abort_cycles() -> int:
    raw = os.environ.get("MORGOTH_OUTAGE_ABORT_CYCLES", "").strip()
    if raw:
        try:
            v = int(raw)
            if v >= 1:
                return v
        except ValueError:
            pass
    return 2


def classify_failure(error_text: str | None) -> str:
    """Return 'network' if error_text matches any known network pattern,
    else 'other'. Empty/None → 'other' (unknown is not network — err
    on the side of NOT firing the guard on ambiguous evidence)."""
    if not error_text:
        return "other"
    lowered = str(error_text).lower()
    for pat in _NETWORK_PATTERNS:
        if pat in lowered:
            return "network"
    return "other"


def cycle_is_all_network_outage(
    tool_results: Iterable[dict[str, Any]],
) -> tuple[bool, list[str]]:
    """Inspect a cycle's tool_results (as produced by BrainResponse).

    Returns (is_outage, sample_error_texts). is_outage is True iff:
      · there is ≥1 tool call in the cycle,
      · every call has success=False, and
      · every failure classifies as 'network'.

    Any success, any non-network failure, or zero calls → (False, []).
    A malformed tool call (KeyError, missing required param) reads
    as 'other' via the classifier and cleanly disqualifies the cycle
    from triggering — the guard MUST NOT fire when the model is
    calling tools wrong.
    """
    calls = list(tool_results or [])
    if not calls:
        return False, []
    errors: list[str] = []
    for tr in calls:
        inner = tr.get("result") or {}
        if inner.get("success", True):
            return False, []
        err_txt = str(inner.get("error") or "")
        if classify_failure(err_txt) != "network":
            return False, []
        errors.append(err_txt)
    return True, errors[:3]
