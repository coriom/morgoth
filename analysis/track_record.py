"""Track-record aggregator + INJECTION-READY generation context.

Reuses the existing scorers (analysis.thesis_backtest*), no new fetchers
or scoring logic. Produces a per-(metric, claim-class) hit-rate table and
qualifies classes for injection into the generation prompt.

DOUBLE GATE (matches auto-approve pattern):
  · TRACK_RECORD_ENABLED env, default False.
  · Data qualification: n >= N_MIN AND rate is >= 2 SE away from chance.
Both must hold. As of commit-time no class qualifies (largest n is ~25
btc_difficulty at 40 %, chance-of-3 = 33 %; well inside CI). The block
stays EMPTY today; the prompt is byte-identical to pre-track-record.

Chance baseline is class-specific:
  · directional (up/down/flat): 1/3
  · level (high/low):           1/2
Margin: 2 standard errors from chance, i.e. |rate - chance| > 2*sqrt(
p*(1-p)/n). At n=20 the required deviation is ~21 % for directional,
~22 % for level. Justifying the margin: 2SE ≈ 95 % rejection-of-chance
under a normal approximation; conservative for small n.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from typing import Any, Iterable, Literal

TRACK_RECORD_ENV = "TRACK_RECORD_ENABLED"
N_MIN = 20
_DIRECTIONAL_CHANCE = 1.0 / 3.0
_LEVEL_CHANCE = 0.5


def track_record_enabled() -> bool:
    """Read TRACK_RECORD_ENABLED (default OFF). Same parse as SHADOW_DELEGATION."""
    raw = (os.environ.get(TRACK_RECORD_ENV) or "").strip().lower()
    return raw in ("1", "true", "on", "yes")


ClaimClass = Literal["directional", "level"]


@dataclass(frozen=True)
class ClassRow:
    metric: str
    claim_class: ClaimClass
    n: int
    hits: int
    rate: float
    chance: float
    margin_required: float
    qualifies: bool


def _std_error(chance: float, n: int) -> float:
    if n <= 0:
        return float("inf")
    return math.sqrt(chance * (1.0 - chance) / n)


def qualifies(rate: float, chance: float, n: int) -> tuple[bool, float]:
    """Return (qualifies, margin_required). n >= N_MIN AND deviation > 2SE."""
    se = _std_error(chance, n)
    margin = 2.0 * se
    if n < N_MIN:
        return False, margin
    return abs(rate - chance) > margin, margin


def aggregate_by_class(
    directional_records: Iterable[Any],
    level_records: Iterable[Any],
) -> list[ClassRow]:
    """Group scored records by (metric, class). Records have .metric,
    .outcome (hit/miss). Skips are already filtered out upstream."""
    buckets: dict[tuple[str, str], list[int]] = {}
    for r in directional_records:
        b = buckets.setdefault((r.metric, "directional"), [0, 0])
        b[0] += 1
        if r.outcome == "hit":
            b[1] += 1
    for r in level_records:
        b = buckets.setdefault((r.metric, "level"), [0, 0])
        b[0] += 1
        if r.outcome == "hit":
            b[1] += 1
    out: list[ClassRow] = []
    for (metric, claim_class), (n, hits) in sorted(buckets.items()):
        rate = hits / n if n else 0.0
        chance = _LEVEL_CHANCE if claim_class == "level" else _DIRECTIONAL_CHANCE
        qual, margin = qualifies(rate, chance, n)
        out.append(ClassRow(
            metric=metric, claim_class=claim_class, n=n, hits=hits,
            rate=rate, chance=chance, margin_required=margin, qualifies=qual,
        ))
    return out


def render_context_block(rows: list[ClassRow]) -> str:
    """Return the TRACK RECORD block for the generation prompt.

    Returns EMPTY STRING when nothing qualifies OR the flag is off — so
    the caller can concatenate unconditionally and the prompt stays
    byte-identical to today (non-regression test asserts this).
    """
    if not track_record_enabled():
        return ""
    qualifying = [r for r in rows if r.qualifies]
    if not qualifying:
        return ""
    lines = [
        "TRACK RECORD — measured hit-rates on prior theses "
        "(chance-baseline noted; be correspondingly careful):",
    ]
    for r in qualifying:
        direction = "above chance" if r.rate > r.chance else "below chance"
        lines.append(
            f"  · {r.metric} ({r.claim_class}): "
            f"{r.rate*100:.0f}% correct over n={r.n} — {direction} "
            f"(chance={r.chance*100:.0f}%)"
        )
    return "\n".join(lines) + "\n\n"
