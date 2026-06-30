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
import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from loguru import logger


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


from core.config import load_config  # noqa: E402
from core.llm_client import ChatMessage, OllamaLLMClient  # noqa: E402
from memory.persistent import PersistentMemory  # noqa: E402


VAULT_DIR = Path.home() / "Morgoth" / "vault"
ENTITIES_DIR = VAULT_DIR / "entities"


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


def _claims_table(theses: list[dict[str, Any]]) -> str:
    """Deterministic markdown table — facts come directly from DB rows."""
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
        sources_cell = ", ".join(sources) if sources else "—"
        obj_id = str(t.get("objective_id") or "")
        obj_short = obj_id[:8] if obj_id else "—"
        rows.append(f"| {claim} | {conf} | {status} | {sources_cell} | `{obj_short}` |")
    return "\n".join(header + rows)


def _entity_page(subject: str, theses: list[dict[str, Any]], summary: str) -> str:
    """Assemble the markdown for one entity page."""
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
        _claims_table(theses),
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
            parts.append(f"  - `{src}`: {detail}")
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
    prompt = (
        f"SUBJECT: {subject}\n\n"
        f"THESES MORGOTH HAS ACCUMULATED ON THIS SUBJECT:\n{bullets}\n\n"
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


def _contradictions_page(contradictions: list[dict[str, Any]]) -> str:
    if not contradictions:
        return "# Contradictions\n\n_No contradictions detected._\n"
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
        link_a = f"[[entities/{slugify(sa)}|{sa}]]" if sa else "_(thesis removed)_"
        link_b = f"[[entities/{slugify(sb)}|{sb}]]" if sb else "_(thesis removed)_"
        parts.append(f"## {group_subj}")
        parts.append("")
        parts.append(f"- detected: `{detected}`")
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
    return "\n".join(parts)


def _clear_entities_dir() -> None:
    """Remove stale entity pages so a subject without theses does not linger."""
    if ENTITIES_DIR.exists():
        for old in ENTITIES_DIR.glob("*.md"):
            old.unlink()
    ENTITIES_DIR.mkdir(parents=True, exist_ok=True)


async def main() -> None:
    config = await load_config()
    pm = PersistentMemory(config)
    await pm.initialize()
    llm = OllamaLLMClient(config)

    try:
        # 1. Read knowledge — read-only, no writes against DB.
        theses = await pm.get_theses(limit=1000)
        contradictions = await pm.get_contradictions(limit=500)

        # 2. Group by subject (exact string; the vault is a human view, minor
        # surface variants becoming separate pages is acceptable per the spec).
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for t in theses:
            subject = (t.get("subject") or "untitled").strip() or "untitled"
            groups[subject].append(t)

        # 3. Prepare vault layout. Clear entities/ for idempotent rewrite.
        VAULT_DIR.mkdir(parents=True, exist_ok=True)
        _clear_entities_dir()

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
                _entity_page(subject, ts, summary), encoding="utf-8"
            )
            entity_index.append((subject, slug, ts))

        # 5. _index.md
        (VAULT_DIR / "_index.md").write_text(
            _index_page(entity_index, len(theses), len(contradictions)),
            encoding="utf-8",
        )

        # 6. contradictions.md
        (VAULT_DIR / "contradictions.md").write_text(
            _contradictions_page(contradictions), encoding="utf-8"
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

        # 8. stdout summary
        print("== Wiki compile complete ==")
        print(f"vault: {VAULT_DIR}")
        print(f"theses read: {len(theses)}")
        print(f"entities written: {len(entity_index)}")
        print(f"contradictions: {len(contradictions)}")
    finally:
        await pm.close()
        await llm.close()


if __name__ == "__main__":
    asyncio.run(main())
