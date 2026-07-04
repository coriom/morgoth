"""One-shot Morgoth Wiki compiler.

Reads structured knowledge (theses + contradictions) from PostgreSQL and writes
an Obsidian-compatible markdown vault under ~/Morgoth/vault.

Design:
- Per entity (one per distinct thesis subject) the PROSE summary comes from
  llama3.1:8b via the existing chat client (same path the synthesis uses),
  but the CLAIMS TABLE is built deterministically in Python from the DB rows.
  Following the synthesis lesson: prose from the model, data exact.
- Each LLM call is wrapped — a failure falls back to "(summary unavailable)"
  so compilation never crashes on one bad chat call.
- Idempotent: entities/ is cleared at the start of each run, then rewritten.
  _index.md, contradictions.md, log.md are overwritten.

Read-only against the database. Writes only under ~/Morgoth/vault.

Note: ~/Morgoth/vault should be gitignored before any commit hygiene work;
this script does not modify .gitignore.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from loguru import logger


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


from core.config import load_config  # noqa: E402
from core.contradictions import group_theses_by_subject  # noqa: E402
from core.llm_client import ChatMessage, OllamaLLMClient  # noqa: E402
from memory.persistent import PersistentMemory  # noqa: E402


VAULT_DIR = Path.home() / "Morgoth" / "vault"
ENTITIES_DIR = VAULT_DIR / "entities"
SYSTEM_DIR = VAULT_DIR / "system"
SYSTEM_TOOLS_DIR = SYSTEM_DIR / "tools"

# The wiki has its own subject-similarity threshold, intentionally LOWER than
# core.contradictions.SUBJECT_SIMILARITY_THRESHOLD (0.75). Rationale: in the
# wiki, over-merging two close concepts onto one page is benign (you still see
# both claims, just under one header). In the detector, a false contradiction
# would be costly — its threshold must stay high to avoid flagging unrelated
# subjects as opposing. The wiki therefore fuses more generously.
# Env override: WIKI_SIMILARITY_THRESHOLD (used for calibration sweeps).
import os as _os  # noqa: E402
WIKI_SUBJECT_SIMILARITY_THRESHOLD: float = float(
    _os.environ.get("WIKI_SIMILARITY_THRESHOLD", "0.6")
)


def _canonical_subject(subjects: list[str]) -> str:
    """Pick the canonical display subject for a semantic group.

    Rule: most frequent subject string wins; ties broken by the longest
    (= more descriptive variant). This favors what the model actually
    produced repeatedly and falls back to the most expressive label
    when every variant appears once.
    """
    cleaned = [(s or "").strip() for s in subjects if s and s.strip()]
    if not cleaned:
        return "untitled"
    freq: dict[str, int] = {}
    for s in cleaned:
        freq[s] = freq.get(s, 0) + 1
    # Sort by (-frequency, -length, original-index) for deterministic pick.
    first_seen: dict[str, int] = {}
    for idx, s in enumerate(cleaned):
        first_seen.setdefault(s, idx)
    best = sorted(
        freq.keys(),
        key=lambda s: (-freq[s], -len(s), first_seen[s]),
    )[0]
    return best


def slugify(value: str) -> str:
    """ASCII-safe slug for filenames; preserves wikilink legibility."""
    s = (value or "untitled").strip().lower()
    s = re.sub(r"[^a-z0-9\s-]", "", s)
    s = re.sub(r"\s+", "-", s).strip("-")
    return s[:80] or "untitled"


def _decode_evidence(raw: Any) -> list[dict[str, Any]]:
    """Decode an evidence column. asyncpg returns JSONB as str."""
    if raw is None:
        return []
    if isinstance(raw, list):
        return [e for e in raw if isinstance(e, dict)]
    if isinstance(raw, str):
        try:
            decoded = json.loads(raw)
        except (ValueError, TypeError):
            return []
        if isinstance(decoded, list):
            return [e for e in decoded if isinstance(e, dict)]
    return []


def _wikilink_source(src: str, known_tools: set[str]) -> str:
    """Wrap a source name as [[system/tools/<name>|<name>]] when known.

    Root-relative wikilinks so Obsidian resolves them from any depth of
    entity page. Unknown sources render as plain text — no dangling links.
    """
    if src and src in known_tools:
        return f"[[system/tools/{src}|{src}]]"
    return src


def _claims_table(
    theses: list[dict[str, Any]],
    known_tools: set[str] | None = None,
) -> str:
    """Deterministic markdown table — facts come directly from DB rows.

    Sources column entries are wrapped as [[system/tools/<name>|<name>]]
    when the tool name is in ``known_tools`` (i.e. it has a system page).
    """
    known = known_tools or set()
    if not theses:
        return "_(no claims)_"
    header = [
        "| Claim | Confidence | Status | Sources | Objective |",
        "|-------|------------|--------|---------|-----------|",
    ]
    rows: list[str] = []
    for t in theses:
        claim = (t.get("claim") or "").replace("|", "\\|")
        conf = t.get("confidence") or "—"
        status = t.get("status") or "—"
        evidence = _decode_evidence(t.get("evidence"))
        sources = sorted({e.get("source", "") for e in evidence if e.get("source")})
        linked = [_wikilink_source(s, known) for s in sources]
        sources_cell = ", ".join(linked) if linked else "—"
        obj_id = str(t.get("objective_id") or "")
        obj_short = obj_id[:8] if obj_id else "—"
        rows.append(f"| {claim} | {conf} | {status} | {sources_cell} | `{obj_short}` |")
    return "\n".join(header + rows)


def _entity_page(
    subject: str,
    theses: list[dict[str, Any]],
    summary: str,
    known_tools: set[str] | None = None,
) -> str:
    """Assemble the markdown for one entity page.

    Source names in both the claims table and evidence detail are wrapped
    as [[system/tools/<name>|<name>]] when they appear in ``known_tools``.
    """
    known = known_tools or set()
    contradicted = any(t.get("status") == "contradicted" for t in theses)
    header_flag = " ⚠️ contradicted" if contradicted else ""
    parts: list[str] = [
        f"# {subject}{header_flag}",
        "",
        "## Summary",
        "",
        summary,
        "",
        "## Claims",
        "",
        _claims_table(theses, known),
        "",
        "## Evidence detail",
        "",
    ]
    for t in theses:
        evidence = _decode_evidence(t.get("evidence"))
        claim = t.get("claim") or "—"
        conf = t.get("confidence") or "—"
        status = t.get("status") or "—"
        parts.append(f"- **{claim}** ({conf}, {status})")
        if not evidence:
            parts.append("  - _(no evidence recorded)_")
            continue
        for e in evidence:
            src = e.get("source", "?")
            detail = (e.get("detail") or "").replace("\n", " ")
            linked = _wikilink_source(src, known) if src != "?" else "`?`"
            parts.append(f"  - {linked}: {detail}")
    parts.append("")
    return "\n".join(parts)


async def _llm_summary(
    client: OllamaLLMClient,
    subject: str,
    theses: list[dict[str, Any]],
) -> str:
    """Ask the model for a short factual summary of what's known about a subject.

    Same output discipline as the synthesis prompt — no preamble, no tool-call
    narration. The model only sees the structured theses, not raw findings, so
    it cannot fabricate beyond what's been validated upstream.
    """
    bullets = "\n".join(
        "- claim={claim} | confidence={conf} | status={status} | sources={sources}".format(
            claim=t.get("claim", "?"),
            conf=t.get("confidence", "?"),
            status=t.get("status", "?"),
            sources=", ".join(
                sorted({e.get("source", "") for e in _decode_evidence(t.get("evidence")) if e.get("source")})
            )
            or "none",
        )
        for t in theses
    )
    # If the semantic group spans multiple surface forms, list them so the
    # model produces ONE coherent summary rather than separate per-variant ones.
    variants = sorted({(t.get("subject") or "").strip() for t in theses if t.get("subject")})
    variant_note = ""
    if len(variants) > 1:
        variant_note = (
            "These theses describe the same subject under variant phrasings:\n"
            + "\n".join(f"  · {v}" for v in variants)
            + "\nTreat them as one subject and write a single coherent summary.\n\n"
        )
    prompt = (
        f"SUBJECT: {subject}\n\n"
        + variant_note
        + f"THESES MORGOTH HAS ACCUMULATED ON THIS SUBJECT:\n{bullets}\n\n"
        "Write a SHORT prose summary (2-4 sentences) of what is known about "
        "this subject based ONLY on the theses above. Do not invent claims. "
        "If theses disagree (e.g. one 'contradicted'), name the disagreement.\n\n"
        "OUTPUT RULES:\n"
        "- No identity preamble or self-reference (do not start with 'I am', "
        "'I will', 'As an AI', 'As Morgoth').\n"
        "- No tool-call narration; do NOT emit 'UPDATE_OBJECTIVE' or status "
        "lines.\n"
        "- Begin directly with the summary."
    )
    messages = [
        ChatMessage(
            role="system",
            content="You write concise factual summaries from structured data.",
        ),
        ChatMessage(role="user", content=prompt),
    ]
    try:
        response = await client.chat(messages)
    except Exception as exc:
        logger.warning("Summary chat failed for subject {!r}: {}", subject, exc)
        return "(summary unavailable)"
    return (response.message.content or "").strip() or "(summary unavailable)"


def _contradictions_page(
    contradictions: list[dict[str, Any]],
    subject_to_canonical: dict[str, str] | None = None,
) -> str:
    """Render contradictions.md. Resolves each side to its canonical entity page.

    If both sides resolve to the SAME canonical page (the two opposed theses
    share a semantic subject), the contradiction is still listed with both
    links pointing to that single page — the contradiction exists within one
    entity, not between two.
    """
    if not contradictions:
        return "# Contradictions\n\n_No contradictions detected._\n"
    canon = subject_to_canonical or {}

    def _resolve(subject: str | None) -> tuple[str | None, str | None]:
        """Return (canonical_subject, slug) or (None, None) if subject is missing."""
        if not subject:
            return None, None
        canonical = canon.get(subject.strip(), subject.strip())
        return canonical, slugify(canonical)

    parts: list[str] = [
        "# Contradictions",
        "",
        f"_{len(contradictions)} contradiction(s) detected — most recent first._",
        "",
    ]
    for c in contradictions:
        group_subj = c.get("subject_group") or "(unknown subject)"
        detected = c.get("detected_at")
        sa = c.get("subject_a")
        sb = c.get("subject_b")
        ca = c.get("claim_a") or "—"
        cb = c.get("claim_b") or "—"
        canon_a, slug_a = _resolve(sa)
        canon_b, slug_b = _resolve(sb)
        link_a = (
            f"[[entities/{slug_a}|{canon_a}]]" if canon_a else "_(thesis removed)_"
        )
        link_b = (
            f"[[entities/{slug_b}|{canon_b}]]" if canon_b else "_(thesis removed)_"
        )
        parts.append(f"## {group_subj}")
        parts.append("")
        parts.append(f"- detected: `{detected}`")
        if canon_a and canon_b and canon_a == canon_b:
            parts.append(f"- both theses resolve to {link_a}:")
            parts.append(f"  - one side claims **{ca}**, the other claims **{cb}**")
        else:
            parts.append(f"- {link_a} — claims **{ca}**")
            parts.append(f"- {link_b} — claims **{cb}**")
        parts.append("")
    return "\n".join(parts)


def _index_page(
    entity_index: list[tuple[str, str, list[dict[str, Any]]]],
    total_theses: int,
    total_contradictions: int,
) -> str:
    parts: list[str] = [
        "# Morgoth Wiki",
        "",
        "_Auto-compiled from Morgoth's accumulated theses._",
        "",
        f"- Entities: **{len(entity_index)}**",
        f"- Theses: **{total_theses}**",
        f"- Contradictions: **{total_contradictions}**",
        "",
        "## Entities",
        "",
    ]
    for subject, slug, theses in entity_index:
        statuses = sorted({t.get("status") for t in theses if t.get("status")})
        flag = " ⚠️" if "contradicted" in statuses else ""
        status_str = " · ".join(statuses) if statuses else ""
        n = len(theses)
        parts.append(
            f"- [[entities/{slug}|{subject}]]{flag} — {n} thesis/theses ({status_str})"
        )
    parts.append("")
    parts.append("## Contradictions")
    parts.append("")
    parts.append("See [[contradictions]].")
    parts.append("")
    parts.append("## System")
    parts.append("")
    parts.append("See [[system/_index|what Morgoth IS]] — every registered tool with")
    parts.append("its description, flags, provenance, and usage.")
    parts.append("")
    return "\n".join(parts)


def _clear_entities_dir() -> None:
    """Remove stale entity pages so a subject without theses does not linger."""
    if ENTITIES_DIR.exists():
        for old in ENTITIES_DIR.glob("*.md"):
            old.unlink()
    ENTITIES_DIR.mkdir(parents=True, exist_ok=True)


def _clear_system_tools_dir() -> None:
    """Idempotent clear — a retired tool leaves no stale page behind."""
    if SYSTEM_TOOLS_DIR.exists():
        for old in SYSTEM_TOOLS_DIR.glob("*.md"):
            old.unlink()
    SYSTEM_TOOLS_DIR.mkdir(parents=True, exist_ok=True)


# ============================================================================
# SYSTEM VAULT — deterministic; NO LLM calls on this path.
#
# The system section documents WHAT MORGOTH IS: every registered tool with
# its ground-truth metadata (name, description, flags), its provenance
# (hand-built or born through the self-modify pipeline — proposal id +
# rationale + applied date), and its live usage stats. Prose already exists
# in the code (docstrings and description= attributes are human- or pipeline-
# written); regenerating it via LLM would only add hallucination risk.
# ============================================================================


def _registered_tools_offline(config: Any, pm: Any) -> list[Any]:
    """Build the full tool router with lightweight stand-ins for callables
    that don't exist offline (agent_manager, notifier, episodic_memory).

    Verified safe: every tool's __init__ just stores references. No I/O at
    construction. Falls back to data_feeds-only discovery if the offline
    build ever regresses.
    """
    from types import SimpleNamespace

    try:
        from api.server import build_tool_router

        router = build_tool_router(
            config, pm, SimpleNamespace(), SimpleNamespace(), SimpleNamespace()
        )
        return list(router._tools.values())  # noqa: SLF001 — inventory read
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning(
            "compile_wiki: build_tool_router failed offline ({}); "
            "falling back to data_feeds discovery. System vault will be partial.",
            exc,
        )
        from tools.discovery import discover_data_feed_tools, instantiate_tool

        return [instantiate_tool(cls, config, pm) for cls in discover_data_feed_tools()]


async def _load_applied_provenance(pm: Any) -> dict[str, dict[str, Any]]:
    """Return {target_path: {proposal_id, rationale, updated_at}} for
    every applied proposal whose file still exists in the live tree.

    An applied-then-reverted proposal (target_path missing) is silently
    skipped — the system vault documents WHAT IS, not what once was.
    """
    pool = pm._require_pool()  # noqa: SLF001
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT proposal_id, target_path, rationale, updated_at "
            "FROM self_modify_proposals WHERE status = 'applied' "
            "ORDER BY updated_at DESC"
        )
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        target = str(row["target_path"])
        if not (PROJECT_ROOT / target).exists():
            continue
        result[target] = {
            "proposal_id": str(row["proposal_id"]),
            "rationale": row.get("rationale") or "",
            "updated_at": row["updated_at"],
        }
    return result


async def _load_tool_usage(pm: Any) -> tuple[dict[str, int], dict[str, list[tuple[str, str]]]]:
    """Compute per-tool usage from the DB.

    Returns two maps keyed by tool name:
    - objectives_count[name] — number of objectives whose sources_used
      array contains this tool name
    - theses_fed[name] — list of (subject, slug) for theses whose evidence
      contains at least one entry with source == name

    Both are computed by pulling the rows and scanning client-side. The
    row counts are small (≤ a few hundred each) so this is cheap.
    """
    pool = pm._require_pool()  # noqa: SLF001
    async with pool.acquire() as conn:
        obj_rows = await conn.fetch(
            "SELECT sources_used FROM objectives WHERE sources_used IS NOT NULL"
        )
        thesis_rows = await conn.fetch(
            "SELECT subject, evidence FROM theses "
            "WHERE evidence IS NOT NULL AND status != 'stale'"
        )

    objectives_count: dict[str, int] = {}
    for row in obj_rows:
        raw = row["sources_used"]
        if isinstance(raw, str):
            try:
                sources = json.loads(raw)
            except (ValueError, TypeError):
                sources = []
        else:
            sources = raw or []
        for name in sources or []:
            if isinstance(name, str):
                objectives_count[name] = objectives_count.get(name, 0) + 1

    theses_fed: dict[str, list[tuple[str, str]]] = {}
    for row in thesis_rows:
        subject = row["subject"] or ""
        evidence = _decode_evidence(row["evidence"])
        seen_sources: set[str] = set()
        for e in evidence:
            src = e.get("source")
            if isinstance(src, str) and src:
                seen_sources.add(src)
        for name in seen_sources:
            theses_fed.setdefault(name, []).append((subject, slugify(subject)))
    return objectives_count, theses_fed


def _tool_page(
    tool: Any,
    provenance: dict[str, Any] | None,
    objectives_count: int,
    theses_fed: list[tuple[str, str]],
    is_data_source: bool = False,
) -> str:
    """Assemble one tool's system page. Deterministic — no LLM.

    is_data_source is passed IN by the caller from the runtime
    DATA_SOURCE_TOOLS set — reading the class flag directly would let a
    tool's page disagree with its rail membership (the reddit /
    web_search issue).
    """
    parts: list[str] = [
        f"# {tool.name}",
        "",
        "## Description",
        "",
        (getattr(tool, "description", None) or "_(no description)_").strip(),
        "",
        "## Flags",
        "",
        f"- data_source: **{is_data_source}**",
        f"- chat_tool:   **{bool(getattr(tool, 'is_chat_tool', True))}**",
        "",
        "## Provenance",
        "",
    ]
    if provenance is not None:
        pid = provenance["proposal_id"]
        applied_at = provenance["updated_at"]
        rationale = provenance["rationale"].strip() or "_(no rationale recorded)_"
        parts.extend(
            [
                f"- origin: **self-modify pipeline** (proposal `{pid[:8]}`)",
                f"- applied_at: `{applied_at}`",
                f"- rationale: {rationale}",
                "",
            ]
        )
    else:
        parts.extend(
            [
                "- origin: **hand-built** (not born through the self-modify pipeline)",
                "",
            ]
        )
    parts.extend(
        [
            "## Usage",
            "",
            f"- objectives that used it: **{objectives_count}**",
            f"- theses fed: **{len(theses_fed)}**",
            "",
            "## Theses fed",
            "",
        ]
    )
    if not theses_fed:
        parts.append("_(this tool has not fed any active thesis yet)_")
    else:
        # Dedupe (subject, slug) — a subject may repeat if multiple theses
        # under it cite this source.
        unique = sorted(set(theses_fed))
        for subject, slug in unique:
            parts.append(f"- [[entities/{slug}|{subject}]]")
    parts.append("")
    return "\n".join(parts)


def _system_index_page(rows: list[dict[str, Any]]) -> str:
    """Build vault/system/_index.md — one table over every tool."""
    parts: list[str] = [
        "# System — what Morgoth IS",
        "",
        "_Auto-compiled from the live tool registry. Deterministic — no LLM._",
        "",
        f"- Tools documented: **{len(rows)}**",
        "",
        "## Tools",
        "",
        "| Tool | data_source? | chat? | origin | #objectives | #theses fed |",
        "|------|--------------|-------|--------|-------------|-------------|",
    ]
    for r in rows:
        parts.append(
            f"| [[system/tools/{r['name']}|{r['name']}]] "
            f"| {'yes' if r['is_data_source'] else 'no'} "
            f"| {'yes' if r['is_chat_tool'] else 'no'} "
            f"| {r['origin']} "
            f"| {r['objectives_count']} "
            f"| {r['theses_fed']} |"
        )
    parts.append("")
    return "\n".join(parts)


async def compile_wiki(
    pm: PersistentMemory,
    llm: OllamaLLMClient,
    exclude_stale: bool = False,
) -> dict[str, Any]:
    """Compile the vault. Caller owns pm/llm lifecycle.

    Returns a counts dict so the CLI can print it AND the HTTP endpoint can
    return it as JSON. Read-only against the DB; writes only under VAULT_DIR.

    If ``exclude_stale`` is True, theses with status='stale' are dropped
    before grouping. Default False preserves the existing behavior (stale
    theses are rendered with a (stale) badge).
    """
    # 1. Read knowledge — read-only, no writes against DB.
    theses = await pm.get_theses(limit=1000)
    if exclude_stale:
        theses = [t for t in theses if t.get("status") != "stale"]
    # Only surface LIVE contradictions in the wiki — pairs voided by the
    # remediation script (timeframe guard, cross-window supersession) stay
    # in the DB as audit trail but do NOT clutter the vault.
    contradictions = await pm.get_contradictions(limit=500, unresolved_only=True)

    # 2. Group by SEMANTIC subject similarity (embeddings). The wiki uses
    # its own threshold (WIKI_SUBJECT_SIMILARITY_THRESHOLD), lower than the
    # detector's: over-merging two close concepts onto one page is benign,
    # while the detector must avoid flagging unrelated subjects as opposing.
    semantic_groups = group_theses_by_subject(
        theses, threshold=WIKI_SUBJECT_SIMILARITY_THRESHOLD
    )
    # Build canonical-subject pages and the surface→canonical map used
    # later when resolving contradiction links.
    groups: dict[str, list[dict[str, Any]]] = {}
    subject_to_canonical: dict[str, str] = {}
    for group in semantic_groups:
        canonical = _canonical_subject([t.get("subject", "") for t in group])
        # In the rare case two semantic groups land on the same canonical
        # string (e.g. both have a one-off identical subject), merge them.
        if canonical in groups:
            groups[canonical].extend(group)
        else:
            groups[canonical] = list(group)
        for t in group:
            subj = (t.get("subject") or "").strip()
            if subj:
                subject_to_canonical[subj] = canonical

    # 3. Prepare vault layout. Clear entities/ and system/tools/ for
    # idempotent rewrites.
    VAULT_DIR.mkdir(parents=True, exist_ok=True)
    _clear_entities_dir()
    _clear_system_tools_dir()

    # 3b. Build the SYSTEM section first — deterministic, no LLM — so
    # entity pages can cross-link source names to system/tools/<name>.md.
    config = await load_config()
    tools = _registered_tools_offline(config, pm)
    tool_names: set[str] = {t.name for t in tools}
    provenance_by_path = await _load_applied_provenance(pm)
    objectives_count, theses_fed = await _load_tool_usage(pm)

    system_rows: list[dict[str, Any]] = []
    # Source of truth for data-source labeling: runtime DATA_SOURCE_TOOLS
    # membership from core.brain — not the class flag. Static-set tools
    # (web_search etc.) live outside tools/data_feeds/ and never had the
    # class attribute set; deriving from the runtime set makes the wiki
    # match /api/tools and the reflect context.
    from core.brain import DATA_SOURCE_TOOLS

    for tool in sorted(tools, key=lambda t: t.name):
        try:
            src_file = Path(inspect.getfile(tool.__class__)).resolve()
            rel = str(src_file.relative_to(PROJECT_ROOT))
        except (TypeError, ValueError):
            rel = ""
        provenance = provenance_by_path.get(rel)
        is_ds = tool.name in DATA_SOURCE_TOOLS
        page = _tool_page(
            tool=tool,
            provenance=provenance,
            objectives_count=objectives_count.get(tool.name, 0),
            theses_fed=theses_fed.get(tool.name, []),
            is_data_source=is_ds,
        )
        (SYSTEM_TOOLS_DIR / f"{tool.name}.md").write_text(page, encoding="utf-8")
        system_rows.append(
            {
                "name": tool.name,
                "is_data_source": is_ds,
                "is_chat_tool": bool(getattr(tool, "is_chat_tool", True)),
                "origin": (
                    f"self-modify `#{provenance['proposal_id'][:8]}`"
                    if provenance
                    else "hand-built"
                ),
                "objectives_count": objectives_count.get(tool.name, 0),
                "theses_fed": len(theses_fed.get(tool.name, [])),
            }
        )
    (SYSTEM_DIR / "_index.md").write_text(
        _system_index_page(system_rows), encoding="utf-8"
    )

    # 4. Generate entity pages. Contradicted subjects first (sticky to top
    # of index too), then alphabetical for stable diffs.
    sorted_subjects = sorted(
        groups.keys(),
        key=lambda s: (
            not any(t.get("status") == "contradicted" for t in groups[s]),
            s.lower(),
        ),
    )
    entity_index: list[tuple[str, str, list[dict[str, Any]]]] = []
    for subject in sorted_subjects:
        ts = groups[subject]
        slug = slugify(subject)
        summary = await _llm_summary(llm, subject, ts)
        (ENTITIES_DIR / f"{slug}.md").write_text(
            _entity_page(subject, ts, summary, known_tools=tool_names),
            encoding="utf-8",
        )
        entity_index.append((subject, slug, ts))

    # 5. _index.md
    (VAULT_DIR / "_index.md").write_text(
        _index_page(entity_index, len(theses), len(contradictions)),
        encoding="utf-8",
    )

    # 6. contradictions.md — resolves each side through the canonical map
    (VAULT_DIR / "contradictions.md").write_text(
        _contradictions_page(contradictions, subject_to_canonical),
        encoding="utf-8",
    )

    # 7. log.md
    now = datetime.now(timezone.utc).isoformat()
    log_md = "\n".join(
        [
            "# Compilation log",
            "",
            f"- last run: `{now}`",
            f"- theses: {len(theses)}",
            f"- entities: {len(entity_index)}",
            f"- contradictions: {len(contradictions)}",
            "",
        ]
    )
    (VAULT_DIR / "log.md").write_text(log_md, encoding="utf-8")

    return {
        "theses_read": len(theses),
        "entities_written": len(entity_index),
        "contradictions": len(contradictions),
        "tools_documented": len(system_rows),
        "vault_path": str(VAULT_DIR),
    }


async def main() -> None:
    """CLI entrypoint: build deps, run compile_wiki, print summary."""
    import argparse

    parser = argparse.ArgumentParser(description="Compile Morgoth's wiki vault.")
    parser.add_argument(
        "--exclude-stale",
        action="store_true",
        help="Drop theses with status='stale' before grouping. Default: include them with a (stale) badge.",
    )
    args = parser.parse_args()

    config = await load_config()
    pm = PersistentMemory(config)
    await pm.initialize()
    llm = OllamaLLMClient(config)
    try:
        counts = await compile_wiki(pm, llm, exclude_stale=args.exclude_stale)
    finally:
        await pm.close()
        await llm.close()
    print("== Wiki compile complete ==")
    print(f"vault: {counts['vault_path']}")
    print(f"theses read: {counts['theses_read']}")
    print(f"entities written: {counts['entities_written']}")
    print(f"contradictions: {counts['contradictions']}")
    print(f"tools documented: {counts.get('tools_documented', 0)}")


if __name__ == "__main__":
    asyncio.run(main())
