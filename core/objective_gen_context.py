"""Knowledge-grounded context builder for the objective-generation prompt.

Kept out of ``core/brain.py`` to prevent that file from bloating —
the builder is a pure read from the persistence layer plus a small
render, no cycle-loop state.

Contract with the caller (``core/brain.py``'s no-active-objectives
branch):

- **Non-blocking**: any DB / registry failure logs a warning and
  returns ``""``. The generation cycle must never crash from a
  contextualization error; the caller falls back to the bootstrap
  prompt (see ``build_generation_context`` docstring below).
- **Deterministic**: same DB state → same string. No LLM anywhere in
  this module; the context is purely observations the model reads.

The four sections rendered, in the order they are surfaced:

1. RECENT OBJECTIVES (newest first) — headed by an explicit
   ``DIVERGE from these`` instruction. This is the reflect
   negative-list pattern transposed: forbid the mode, never
   prescribe a topic.
2. DATA SOURCES with usage counts — reuses the offline registry +
   ``_load_tool_usage`` from scripts.compile_wiki. A 0-usage tool
   is an unexplored territory signal the model can act on;
   editorializing beyond the counts would prescribe a topic.
3. ACTIVE THESIS SUBJECTS — the distinct-subject set from
   ``get_theses(status='active')``.
4. OPEN CONTRADICTIONS — an unresolved contradiction is a live
   research lead. Count + up to 3 subject groups.

Bootstrap fallback (documented at the caller): when this builder
returns ``""`` (empty DB — no titles, no theses, no tools — OR a
handled failure), the caller retains the historical STEP-1
price-scan prompt so a zero-knowledge Morgoth still generates.
"""

from __future__ import annotations

from typing import Any

from loguru import logger


# Section-cap defaults. Kept as module constants so tests can flex
# them without patching the render loop.
_RECENT_TITLES_LIMIT = 10
_ACTIVE_THESIS_SUBJECTS_LIMIT = 10
_OPEN_CONTRADICTIONS_SHOWN = 3
# Fetch a wider window for contradictions so the header count is
# accurate even when we only render three subject groups.
_OPEN_CONTRADICTIONS_SCAN = 50


async def _recent_objective_titles(pm: Any, limit: int) -> list[str]:
    """Newest-first list of ``objectives.title`` — for the divergence hint.

    ``pm.get_objectives()`` sorts by priority ASC then created_at ASC
    (oldest first), which is the wrong axis for divergence. This
    dedicated query hits the pool directly, read-only.
    """
    pool = pm._require_pool()  # noqa: SLF001 — same pattern as ProposalStore
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT title FROM objectives "
            "ORDER BY created_at DESC LIMIT $1",
            limit,
        )
    return [row["title"] for row in rows if row.get("title")]


async def build_generation_context(pm: Any, config: Any) -> str:
    """Return a rendered multi-section context block, or ``""``.

    ``""`` means: caller should use the bootstrap fallback (either
    the DB is empty / pre-first-run, or a section loader raised).
    Every DB touch is wrapped so a single failure does not deprive
    the model of the other sections — the returned string carries
    whatever we could recover.
    """
    # Import inside the function so brain.py's tests can patch these
    # symbols without pulling the offline registry at module load.
    try:
        from core.brain import DATA_SOURCE_TOOLS
        from scripts.compile_wiki import (
            _load_tool_usage,
            _registered_tools_offline,
        )
    except Exception as exc:
        logger.warning(
            "generation-context: imports failed (non-blocking): {}", exc,
        )
        return ""

    titles: list[str] = []
    thesis_subjects: list[str] = []
    contradictions: list[dict[str, Any]] = []
    tools: list[Any] = []
    objectives_count: dict[str, int] = {}

    # Each loader is independently guarded — a failure in one does
    # not silence the whole context.
    try:
        titles = await _recent_objective_titles(pm, limit=_RECENT_TITLES_LIMIT)
    except Exception as exc:
        logger.warning(
            "generation-context: recent titles load failed: {}", exc,
        )
    try:
        theses = await pm.get_theses(status="active", limit=25)
        seen: set[str] = set()
        for th in theses:
            subject = (th.get("subject") or "").strip()
            if subject and subject not in seen:
                seen.add(subject)
                thesis_subjects.append(subject)
            if len(thesis_subjects) >= _ACTIVE_THESIS_SUBJECTS_LIMIT:
                break
    except Exception as exc:
        logger.warning(
            "generation-context: theses load failed: {}", exc,
        )
    try:
        contradictions = await pm.get_contradictions(
            limit=_OPEN_CONTRADICTIONS_SCAN, unresolved_only=True,
        )
    except Exception as exc:
        logger.warning(
            "generation-context: contradictions load failed: {}", exc,
        )
    try:
        tools = _registered_tools_offline(config, pm)
        objectives_count, _theses_fed = await _load_tool_usage(pm)
    except Exception as exc:
        logger.warning(
            "generation-context: tools/usage load failed: {}", exc,
        )

    # Pre-bootstrap detection: with NO titles AND NO theses AND NO
    # tools we cannot say anything meaningful — the caller should
    # fall back to the bootstrap prompt.
    if not titles and not thesis_subjects and not tools:
        return ""

    sections: list[str] = []

    if titles:
        sections.append(
            "Your next objective must DIVERGE from these — do not "
            "rephrase them.\n"
            "RECENT OBJECTIVES (newest first):\n"
            + "\n".join(f"- {t}" for t in titles)
        )

    if tools:
        # Only data_source tools are relevant to objective generation —
        # utility tools (notify, recall, remember, etc.) are means, not
        # topics. Rendering matches reflect's tool-line format so the
        # model sees consistent framing across generation and reflect.
        data_source_lines: list[str] = []
        for t in sorted(tools, key=lambda x: x.name):
            if t.name not in DATA_SOURCE_TOOLS:
                continue
            n = objectives_count.get(t.name, 0)
            desc = (getattr(t, "description", "") or "").strip()
            data_source_lines.append(
                f"- {t.name} (objectives_using={n}): {desc}"
            )
        if data_source_lines:
            sections.append(
                "DATA SOURCES (usage counts — a 0 signals unexplored "
                "territory):\n" + "\n".join(data_source_lines)
            )

    if thesis_subjects:
        sections.append(
            "ACTIVE THESIS SUBJECTS:\n"
            + "\n".join(f"- {s}" for s in thesis_subjects)
        )

    if contradictions:
        total = len(contradictions)
        groups: list[str] = []
        for c in contradictions[:_OPEN_CONTRADICTIONS_SHOWN]:
            group = (c.get("subject_group") or "").strip()
            if not group:
                # Fallback shape if the join failed on one side —
                # surface whichever subject is available.
                a = (c.get("subject_a") or "").strip()
                b = (c.get("subject_b") or "").strip()
                group = f"{a} vs {b}".strip(" vs")
            if group:
                groups.append(group)
        head = f"OPEN CONTRADICTIONS ({total}):"
        section_lines = [head] + [f"- {g}" for g in groups]
        sections.append("\n".join(section_lines))

    if not sections:
        return ""
    return "\n\n".join(sections) + "\n\n"
