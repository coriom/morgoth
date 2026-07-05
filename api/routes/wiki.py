"""Wiki compile + read-only serve endpoints.

POST /api/wiki/compile   → rebuild the vault (delegates to scripts/compile_wiki).
GET  /api/wiki/manifest  → list of all .md pages with section + compiled_at.
GET  /api/wiki/page      → raw markdown content for one page.

The vault (``~/Morgoth/vault``) is the single source of truth for the wiki.
Obsidian keeps working directly against these files; this API just exposes
them READ-ONLY to the dashboard front-end. There is no write endpoint for
page content — the only mutating call is ``compile``, which regenerates the
vault from persistent state, not from any request payload.

Path traversal is the security core here. Every ``path`` we accept is
validated against the resolved VAULT_DIR (belt: pre-resolution reject on
absolute paths / ``..`` components; braces: post-resolution
``relative_to`` check + suffix check). Escape attempts and missing files
share the SAME 404 response — no oracle.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from loguru import logger

from scripts.compile_wiki import VAULT_DIR, compile_wiki


router = APIRouter(prefix="/api/wiki", tags=["wiki"])


@router.post("/compile")
async def compile_wiki_endpoint(request: Request) -> dict[str, Any]:
    """Recompile the Morgoth vault and return the count summary."""
    try:
        counts = await compile_wiki(
            request.app.state.persistent_memory,
            request.app.state.llm_client,
        )
    except Exception as exc:
        logger.exception("POST /api/wiki/compile failed")
        raise HTTPException(
            status_code=500, detail=f"wiki compile failed: {type(exc).__name__}"
        )
    return counts


# ---------- read-only serve --------------------------------------------


def _section_for(rel_path: Path) -> str:
    """Group by top-level parent under VAULT_DIR: root / entities / system.

    Files directly under the vault (``_index.md``, ``log.md``,
    ``contradictions.md``) land in ``root``; anything under
    ``entities/`` or ``system/`` (any depth) picks up that section
    name. New top-level dirs fall back to their own name so the UI
    still groups them coherently.
    """
    parts = rel_path.parts
    if len(parts) == 1:
        return "root"
    return parts[0]


def _title_for(md_path: Path, rel_path: Path) -> str:
    """First ``# `` heading if present; else the filename stem."""
    try:
        with md_path.open("r", encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if stripped.startswith("# "):
                    return stripped[2:].strip()
                # Only scan the first ~20 non-empty lines — a title
                # buried deep is not a title.
                if line.startswith("---"):
                    continue
        return rel_path.stem
    except OSError:
        return rel_path.stem


def _compiled_at() -> str | None:
    """The compile timestamp from log.md if parseable, else its mtime."""
    log = VAULT_DIR / "log.md"
    if not log.exists():
        return None
    try:
        text = log.read_text(encoding="utf-8")
    except OSError:
        text = ""
    # compile_wiki writes ``- last run: `<iso>```` on the first
    # informational line. Grep that; on any failure, fall back to
    # the file's mtime — the operator still gets a fresh signal.
    for line in text.splitlines():
        if line.startswith("- last run: `") and line.rstrip().endswith("`"):
            return line[len("- last run: `") : -1]
    try:
        mtime = log.stat().st_mtime
    except OSError:
        return None
    return datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()


@router.get("/manifest")
async def wiki_manifest() -> dict[str, Any]:
    """Return every ``*.md`` page under VAULT_DIR + a compiled_at stamp.

    Response:
        {
          "compiled_at": "<iso-8601 or null>",
          "pages": [
            {"path": "entities/btc-short-term-price.md",
             "title": "BTC short-term price",
             "section": "entities"},
            ...
          ]
        }
    """
    vault_root = VAULT_DIR.resolve()
    if not vault_root.exists():
        return {"compiled_at": None, "pages": []}

    pages: list[dict[str, str]] = []
    for md_path in sorted(vault_root.rglob("*.md")):
        try:
            rel = md_path.resolve().relative_to(vault_root)
        except ValueError:
            # A symlink pointing outside the vault — skip silently.
            continue
        rel_posix = rel.as_posix()
        pages.append({
            "path": rel_posix,
            "title": _title_for(md_path, rel),
            "section": _section_for(rel),
        })
    return {"compiled_at": _compiled_at(), "pages": pages}


def _reject_path_shape(path: str) -> None:
    """Belt: reject absolute paths and ``..`` components BEFORE resolution.

    Escape attempts share the missing-file 404 response — a clean
    ``HTTPException(404)`` here is caught by the caller and re-raised
    with the same detail.
    """
    if not path:
        raise HTTPException(status_code=404, detail="page not found")
    if path.startswith("/") or path.startswith("\\"):
        raise HTTPException(status_code=404, detail="page not found")
    # Parts split reveals ``..`` even when hidden inside deeper paths.
    parts = Path(path).parts
    if any(p in ("..", "") for p in parts):
        raise HTTPException(status_code=404, detail="page not found")


@router.get("/page")
async def wiki_page(
    path: str = Query(..., description="Vault-relative path, e.g. 'entities/x.md'"),
) -> dict[str, Any]:
    """Return one markdown page by vault-relative path.

    Path traversal guard:
    1. Reject absolute paths and any ``..`` component up front.
    2. Resolve under VAULT_DIR and confirm ``relative_to(VAULT_DIR)``.
    3. Confirm ``.md`` suffix and existence.

    Escape attempts and missing files return the SAME 404 detail —
    no oracle for probing the filesystem.
    """
    _reject_path_shape(path)

    vault_root = VAULT_DIR.resolve()
    try:
        candidate = (vault_root / path).resolve()
    except (OSError, RuntimeError):
        raise HTTPException(status_code=404, detail="page not found")
    try:
        rel = candidate.relative_to(vault_root)
    except ValueError:
        raise HTTPException(status_code=404, detail="page not found")
    if candidate.suffix != ".md" or not candidate.is_file():
        raise HTTPException(status_code=404, detail="page not found")

    try:
        content = candidate.read_text(encoding="utf-8")
        mtime = candidate.stat().st_mtime
    except OSError:
        raise HTTPException(status_code=404, detail="page not found")
    return {
        "path": rel.as_posix(),
        "content": content,
        "mtime": datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat(),
    }
