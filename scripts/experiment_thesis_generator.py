"""One-off experiment: run thesis extraction with THESIS_GENERATOR=claude-cli
on RECENT objective syntheses, tag the produced theses, and score them.

READ-ONLY on gates/apply/shadow. Writes to `theses` with objective_id
prefix 'experiment-claude-cli-' so backtest analysis can isolate.

Usage:
    THESIS_GENERATOR=claude-cli python -m scripts.experiment_thesis_generator --n 20

The point is to answer "is the 33.3 % hit-rate ceiling the MODEL or the
TASK?" — running the SAME grounding prompt on the SAME synthesis with
a stronger model and comparing hit rates.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.brain import Brain  # noqa: E402
from core.config import load_config  # noqa: E402
from memory.persistent import PersistentMemory  # noqa: E402

EXPERIMENT_TAG = "experiment-claude-cli"


def _extract_synthesis(evidence_str: str | None) -> str | None:
    """Pull the 'summary' field out of an objective's evidence JSON."""
    if not evidence_str:
        return None
    try:
        data = json.loads(evidence_str) if isinstance(evidence_str, str) else evidence_str
    except json.JSONDecodeError:
        return None
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return data[0].get("summary")
    if isinstance(data, dict):
        return data.get("summary") or data.get("content")
    return None


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n", type=int, default=20, help="max objectives to sample")
    parser.add_argument("--dry-run", action="store_true", help="parse but do not insert")
    args = parser.parse_args()

    if os.environ.get("THESIS_GENERATOR", "").lower() != "claude-cli":
        print("WARN: THESIS_GENERATOR is not 'claude-cli' — set it or the experiment is meaningless")
    config = await load_config()
    pm = PersistentMemory(config)
    await pm.initialize()

    # Minimal Brain shell — only the extractor is needed.
    from unittest.mock import MagicMock
    brain = Brain(
        config=config, llm_client=MagicMock(), persistent_memory=pm,
        episodic_memory=MagicMock(), scheduler=MagicMock(),
        tool_router=MagicMock(), agent_manager=MagicMock(),
        notifier=MagicMock(), websocket_manager=None,
    )

    pool = pm._require_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT objective_id, title, evidence, created_at
            FROM objectives
            WHERE evidence::text ILIKE '%summary%'
              AND created_at >= '2026-08-07'
            ORDER BY created_at DESC LIMIT $1
            """, args.n,
        )

    all_theses = []
    total_syntheses = 0
    for r in rows:
        synth = _extract_synthesis(r["evidence"])
        if not synth or len(synth.strip()) < 100:
            continue
        total_syntheses += 1
        obj = {"objective_id": str(r["objective_id"]), "title": r["title"]}
        try:
            theses = await brain._extract_theses(obj, synth, sources=["multiple"])
        except Exception as exc:
            print(f"  ERR objective {str(r['objective_id'])[:8]}: {type(exc).__name__}: {exc}")
            continue
        print(f"  obj {str(r['objective_id'])[:8]} synth_len={len(synth)}: {len(theses)} thesis/theses")
        for t in theses:
            all_theses.append((str(r["objective_id"]), t))
    print(f"\nTotal: {total_syntheses} syntheses → {len(all_theses)} theses")

    if args.dry_run:
        for oid, t in all_theses[:5]:
            print(f"  DRY [{oid[:8]}]  {t.get('subject')!r} :: {t.get('claim')!r}")
        await pm.close()
        return 0

    # Insert with tagged objective_id so the backtest can isolate.
    async with pool.acquire() as conn:
        for oid, t in all_theses:
            await conn.execute(
                "INSERT INTO theses (subject, claim, confidence, evidence, objective_id) "
                "VALUES ($1, $2, $3, $4::jsonb, $5)",
                str(t.get("subject", ""))[:200],
                str(t.get("claim", ""))[:400],
                str(t.get("confidence", "medium")),
                json.dumps(t.get("evidence") or []),
                f"{EXPERIMENT_TAG}-{oid[:8]}",
            )
    print(f"Inserted {len(all_theses)} experiment theses tagged '{EXPERIMENT_TAG}-*'")
    await pm.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
