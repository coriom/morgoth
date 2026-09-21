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


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    u = len(a | b)
    return (len(a & b) / u) if u else 0.0


def _pairwise_title_similarity(
    titles: list[str],
) -> tuple[float, tuple[str, str, float] | None]:
    """Mean pairwise Jaccard + closest pair (t1, t2, score). Reuses
    the _tokens helper from the drift/dup guard so the numbers here
    match what CreateObjectiveTool's dup guard sees."""
    if len(titles) < 2:
        return 0.0, None
    scores: list[float] = []
    top: tuple[str, str, float] | None = None
    for i in range(len(titles)):
        for j in range(i + 1, len(titles)):
            s = _jaccard(_tokens(titles[i]), _tokens(titles[j]))
            scores.append(s)
            if top is None or s > top[2]:
                top = (titles[i], titles[j], s)
    mean = sum(scores) / len(scores) if scores else 0.0
    return mean, top


def format_campaign_report(
    campaign: dict[str, Any],
    objectives: list[dict[str, Any]],
    theses: list[dict[str, Any]],
    contradictions: list[dict[str, Any]],
    prior_canonical_subjects: set[str] | None = None,
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

    # (a) ANGLE DIVERGENCE — mean pairwise Jaccard + the closest pair.
    # Low mean + a low top score → real angle divergence. High top
    # score = a near-duplicate slipped past CreateObjectiveTool's
    # retry-once dup guard; operator should see the pair verbatim.
    titles = [o.get("title", "") for o in objectives if o.get("title")]
    mean_sim, top_pair = _pairwise_title_similarity(titles)
    lines.append("")
    lines.append("ANGLE DIVERGENCE (pairwise Jaccard on titles):")
    if len(titles) < 2:
        lines.append("  n/a — need ≥2 objectives")
    else:
        lines.append(f"  mean similarity : {mean_sim:.2f}")
        if top_pair:
            lines.append(f"  closest pair    : {top_pair[2]:.2f}")
            lines.append(f"    · {top_pair[0]}")
            lines.append(f"    · {top_pair[1]}")

    # (b) SCORABILITY — triage the campaign's theses via the backtest
    # classifier (canonical_subject preferred, falls back to raw).
    # Do NOT re-implement the classifier here.
    from analysis.thesis_backtest_descriptive import triage as _triage
    rows_for_triage = [
        {
            "subject": t.get("canonical_subject") or t.get("subject", ""),
            "claim": t.get("claim", ""),
        }
        for t in theses
    ]
    counts, _mrs, _un, _subj = _triage(rows_for_triage) if rows_for_triage else (
        {"input": 0, "metric": 0, "relation": 0, "unreachable": 0, "subjective": 0},
        [], [], [],
    )
    lines.append("")
    lines.append("SCORABILITY (of the theses this campaign produced):")
    lines.append(f"  verifiable-metric      : {counts.get('metric', 0)}")
    lines.append(f"  verifiable-relation    : {counts.get('relation', 0)}")
    lines.append(f"  unreachable (no source): {counts.get('unreachable', 0)}")
    lines.append(f"  subjective / unmapped  : {counts.get('subjective', 0)}")

    # (c) NOVELTY — how many campaign theses landed on a canonical
    # subject that did NOT exist before started_at, vs subjects the
    # store already carried. Caller supplies the pre-campaign set;
    # empty set means every canonical is treated as new.
    prior = prior_canonical_subjects or set()
    campaign_canonicals = [
        (t.get("canonical_subject") or "").strip().lower()
        for t in theses if (t.get("canonical_subject") or "").strip()
    ]
    novel = sum(1 for c in campaign_canonicals if c not in prior)
    reused = sum(1 for c in campaign_canonicals if c in prior)
    lines.append("")
    lines.append("NOVELTY (canonical subjects introduced by this campaign):")
    lines.append(f"  novel (not seen before campaign start) : {novel}")
    lines.append(f"  reused (already in store)              : {reused}")

    return "\n".join(lines) + "\n"
