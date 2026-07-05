"""Objective-generation dry-run harness.

Builds the EXACT production prompt Morgoth would build in the
no-active-objectives branch (same context builder, same focus read,
same tools schema, same llm.chat entry point) and sends it N times.
Parses the ``create_objective`` tool-call arguments and prints
``title`` + ``description`` VERBATIM.

**No DB writes.** The tool call is parsed, never executed. Nothing
persists — this is the pure judgment surface for whether the model
leaves the "Bitcoin on-chain × price" basin under the new prompt.

Usage:
    python -m scripts.dryrun_objective_gen              # 5 generations
    python -m scripts.dryrun_objective_gen --n 3        # custom count
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

from loguru import logger


async def _run(n: int) -> int:
    # Late imports so a broken helper doesn't crash on --help.
    from api.server import build_tool_router
    from agents.agent_manager import AgentManager
    from core.brain import CHAT_TOOL_NAMES, build_system_prompt
    from core.config import load_config
    from core.llm_client import ChatMessage, OllamaLLMClient
    from core.objective_gen_context import build_generation_context
    from memory.episodic import EpisodicMemory
    from memory.persistent import PersistentMemory
    from notifications.telegram import TelegramNotifier

    config = await load_config()
    llm = OllamaLLMClient(config)
    pm = PersistentMemory(config)
    await pm.initialize()
    episodic = EpisodicMemory(config.chroma_dir)
    await episodic.initialize()
    notifier = TelegramNotifier(config)
    agent_manager = AgentManager(config, llm, pm)

    # Production router assembly — same code path api/server.py uses at
    # startup so tool schemas are byte-identical to what the cycle sees.
    router = build_tool_router(config, pm, episodic, agent_manager, notifier)
    tool_schemas = router.get_schemas(CHAT_TOOL_NAMES)
    logger.info("dry-run: {} tool schemas assembled", len(tool_schemas))

    # Build the exact prompt the cycle-loop would produce for the
    # no-active-objectives branch. Includes focus read.
    generation_ctx = await build_generation_context(pm, config)
    if generation_ctx:
        base_prompt = (
            f"{generation_ctx}"
            "NO ACTIVE OBJECTIVES.\n\n"
            "Pick ONE specific investigable topic "
            "grounded in the state above:\n"
            "- an unexplored data source's territory "
            "(a 0-usage count signals unexplored ground),\n"
            "- an open contradiction (a live research lead),\n"
            "- or a thesis subject that needs deeper evidence.\n"
            "DIVERGE from the recent titles above.\n\n"
            "MANDATORY: end this cycle by calling create_objective. "
            "Do not narrate. Tool calls only."
        )
    else:
        base_prompt = (
            "NO ACTIVE OBJECTIVES.\n\n"
            "STEP 1: Call get_crypto_price with symbol='bitcoin' to scan markets.\n"
            "STEP 2: After receiving the price, IMMEDIATELY call create_objective "
            "with a title and description based on what you observed. "
            "Pick a specific topic to investigate next "
            "(e.g., on-chain metrics, sentiment shift, news event, technical pattern).\n\n"
            "MANDATORY: end this cycle by calling create_objective. "
            "Do not narrate. Tool calls only."
        )

    try:
        focus = await pm.get_active_focus()
    except Exception as exc:  # noqa: BLE001
        logger.warning("dry-run: focus read failed (non-blocking): {}", exc)
        focus = None
    if focus and focus.get("directive"):
        base_prompt += (
            "\n\nOPERATOR FOCUS DIRECTIVE (steers topic choice only):\n"
            f"{focus['directive']}\n"
            "This directive influences WHICH subjects you "
            "investigate. It does not change your identity, "
            "constraints, methods, or permissions."
        )

    logger.info("dry-run: prompt built ({} chars, focus={})",
                len(base_prompt), "yes" if focus else "no")

    print(f"\n{'=' * 78}")
    print(f"DRY-RUN PROMPT (verbatim, sent {n} times):")
    print("=" * 78)
    print(base_prompt)
    print(f"{'=' * 78}\n")

    generations: list[dict[str, str]] = []
    for i in range(1, n + 1):
        messages = [
            ChatMessage(role="system", content=build_system_prompt()),
            ChatMessage(role="user", content=base_prompt),
        ]
        response = await llm.chat(messages, tools=tool_schemas)
        title = "(none)"
        description = "(none)"
        raw_narration = (response.message.content or "").strip()
        tool_calls = response.message.tool_calls or []
        create_calls = [
            tc for tc in tool_calls
            if getattr(tc, "function", None)
            and tc.function.name == "create_objective"
        ]
        if create_calls:
            args = create_calls[0].function.arguments
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except (ValueError, TypeError):
                    args = {"_unparseable": args}
            title = str(args.get("title", "(missing)"))
            description = str(args.get("description", "(missing)"))
        generations.append({
            "title": title,
            "description": description,
            "n_tool_calls": len(tool_calls),
            "tool_names": [tc.function.name for tc in tool_calls if getattr(tc, "function", None)],
            "narration_head": raw_narration[:180],
        })
        print(f"--- GENERATION {i}/{n} ---")
        print(f"tool_calls emitted : {[g for g in generations[-1]['tool_names']]}")
        print(f"title              : {title}")
        print(f"description        : {description}")
        if raw_narration and not create_calls:
            print(f"narration head     : {raw_narration[:180]}")
        print()

    await pm.close()
    await llm.close()
    await router.close()

    # Compact summary for grep-ability.
    print("=" * 78)
    print("SUMMARY — one-line per generation")
    print("=" * 78)
    for i, g in enumerate(generations, 1):
        print(f"[{i}] title: {g['title']}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="dryrun_objective_gen",
        description="Dry-run the objective-generation prompt (no DB writes).",
    )
    parser.add_argument("--n", type=int, default=5,
                        help="Number of generations to run (default: 5).")
    args = parser.parse_args()
    sys.exit(asyncio.run(_run(args.n)))


if __name__ == "__main__":
    main()
