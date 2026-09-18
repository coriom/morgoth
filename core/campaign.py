"""Campaign primitives — accumulation block, drift/dup guards, report.

A campaign locks the generator onto ONE subject for N days. Reuses
focus_directives' single-active + tombstone plumbing (start_campaign
tombstones any active row before inserting). Objective N+1 sees the
theses produced under this campaign so far, plus previous objective
titles — the DIVERGE-from-recent-titles instruction is replaced with
"stay within <subject>; diverge in ANGLE, not in topic".
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any


# Accumulation block cap — most-recent N thesis entries and N prior
# objective titles are injected; the rest are elided. Chosen so the
# block fits comfortably in one Ollama context page and still gives
# the model enough state to attack an uncovered angle.
CAMPAIGN_THESES_CAP: int = 15
CAMPAIGN_TITLES_CAP: int = 10
# Jaccard-similarity threshold above which a newly-generated title is
# treated as a near-duplicate of a prior campaign title. 0.7 catches
# "BTC dominance vs price" ≈ "BTC dominance and price" without merging
# "BTC dominance" and "BTC volatility" (which share only "btc").
CAMPAIGN_DUP_JACCARD: float = 0.7


_TOKEN_RE = re.compile(r"[a-z0-9]+")
_STOPWORDS: frozenset[str] = frozenset({
    "the", "a", "an", "of", "and", "or", "in", "on", "at", "to", "for",
    "with", "vs", "versus", "over", "under", "how", "what", "why", "is",
    "are", "does", "do", "impact", "effect", "relationship", "between",
    "investigate", "examine", "analyze", "analyse", "determine",
})


def _tokens(text: str) -> set[str]:
    """Lowercased alphanumeric tokens minus stopwords."""
    if not text:
        return set()
    return {t for t in _TOKEN_RE.findall(text.lower()) if t not in _STOPWORDS}


def title_matches_subject(title: str, subject: str) -> bool:
    """True iff the title shares ≥1 non-stopword token with the subject.
    Used as a drift guard on newly-generated campaign objectives —
    "Ethereum hashrate" fails against subject "BTC dominance"."""
    return bool(_tokens(title) & _tokens(subject))


def titles_near_duplicate(new_title: str, prior_title: str) -> bool:
    """Jaccard similarity ≥ CAMPAIGN_DUP_JACCARD on non-stopword tokens."""
    a = _tokens(new_title)
    b = _tokens(prior_title)
    if not a or not b:
        return False
    inter = len(a & b)
    union = len(a | b)
    return (inter / union) >= CAMPAIGN_DUP_JACCARD if union else False


def build_accumulation_block(
    subject: str,
    campaign_theses: list[dict[str, Any]],
    campaign_titles: list[str],
) -> str:
    """Render the state-so-far block injected into the generation prompt.

    Two lists — capped, most-recent-first — plus the standing instruction
    to attack what is NOT yet covered. This is what turns repetition
    into cumulative progress.
    """
    ths = list(campaign_theses)[:CAMPAIGN_THESES_CAP]
    tls = list(campaign_titles)[:CAMPAIGN_TITLES_CAP]
    lines: list[str] = [
        f"CAMPAIGN SUBJECT: {subject}",
        "This is a MULTI-DAY campaign. Stay within the subject; diverge in ANGLE, not in topic.",
    ]
    if tls:
        lines.append("")
        lines.append("ANGLES ALREADY INVESTIGATED (do NOT repeat these):")
        for t in tls:
            lines.append(f"  - {t}")
    if ths:
        lines.append("")
        lines.append("WHAT HAS BEEN ESTABLISHED (recent theses):")
        for t in ths:
            ev = t.get("evidence") or []
            detail = ""
            if ev and isinstance(ev[0], dict):
                detail = str(ev[0].get("detail", ""))[:80]
            lines.append(
                f"  - {t.get('subject', '?')}: {t.get('claim', '?')}"
                + (f"  ({detail})" if detail else "")
            )
    lines.append("")
    lines.append(
        "PICK A NEW ANGLE on the subject that no prior objective has taken. "
        "The angle must MENTION the subject explicitly in the title."
    )
    return "\n".join(lines)


def format_campaign_report(
    campaign: dict[str, Any],
    objectives: list[dict[str, Any]],
    theses: list[dict[str, Any]],
    contradictions: list[dict[str, Any]],
) -> str:
    """Plain-text campaign report — screenshot-friendly, no LLM prose."""
    subj = campaign.get("subject", "?")
    started = campaign.get("started_at")
    ended = campaign.get("ended_at") or campaign.get("ends_at")
    dur_h = 0.0
    if isinstance(started, datetime) and isinstance(ended, datetime):
        dur_h = max(0.0, (ended - started).total_seconds() / 3600.0)
    src_counts: dict[str, int] = {}
    for o in objectives:
        for s in (o.get("sources_used") or []):
            src_counts[s] = src_counts.get(s, 0) + 1
    lines = [
        f"=== CAMPAIGN REPORT — {subj} ===",
        f"  id           : {campaign.get('campaign_id')}",
        f"  status       : {campaign.get('status')}",
        f"  started_at   : {started}",
        f"  ends_at/end  : {ended}",
        f"  duration     : {dur_h:.1f} h",
        f"  objectives   : {len(objectives)}",
        f"  theses       : {len(theses)}",
        f"  contradictions in-subject : {len(contradictions)}",
        "",
        "ANGLES COVERED:",
    ]
    for o in objectives:
        lines.append(f"  - [{o.get('status', '?'):>13}] {o.get('title', '?')}")
    lines.append("")
    lines.append("THESES PRODUCED (subject: claim  ← evidence.detail):")
    for t in theses:
        ev = t.get("evidence") or []
        det = str(ev[0].get("detail", ""))[:80] if ev and isinstance(ev[0], dict) else ""
        lines.append(f"  - {t.get('subject')}: {t.get('claim')}  ← {det}")
    lines.append("")
    lines.append("SOURCES USED (tool → objectives touching it):")
    for name, n in sorted(src_counts.items(), key=lambda kv: -kv[1]):
        lines.append(f"  - {name:<32} {n}")
    unverif = sum(
        1 for o in objectives
        if not (o.get("sources_used") or [])
    )
    lines.append("")
    lines.append(f"UNVERIFIABLE (objectives with 0 sources): {unverif}")
    if contradictions:
        lines.append("")
        lines.append("CONTRADICTIONS raised within the subject:")
        for c in contradictions:
            lines.append(f"  - {c.get('subject_group')} ({c.get('detected_at')})")
    return "\n".join(lines) + "\n"
