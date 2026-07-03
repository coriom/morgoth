"""CLI backing for ``morgoth focus`` — set / show / clear the operator
focus directive.

Design choice: single script rather than a package. Focus is smaller
scope than self_modify (one table, three verbs, no lifecycle), so a
lightweight ``python scripts/focus_cli.py <verb>`` keeps the surface
area small and matches the repo's other one-shot scripts
(``scripts/compile_wiki.py``, ``scripts/remediate_contradictions.py``).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


from core.config import load_config  # noqa: E402
from memory.persistent import PersistentMemory  # noqa: E402


async def _cmd_set(pm: PersistentMemory, args: argparse.Namespace) -> int:
    directive = (args.text or "").strip()
    if not directive:
        print("morgoth focus: directive text cannot be empty", file=sys.stderr)
        return 2
    focus_id = await pm.set_focus_directive(directive)
    print(f"focus set: {focus_id}")
    print(f"directive: {directive}")
    print("effective: next objective generation (no restart required)")
    return 0


async def _cmd_show(pm: PersistentMemory, _args: argparse.Namespace) -> int:
    row = await pm.get_active_focus()
    if not row:
        print("focus: none")
        return 0
    print(f"focus_id:   {row['focus_id']}")
    print(f"since:      {row['created_at']}")
    print(f"directive:  {row['directive']}")
    return 0


async def _cmd_clear(pm: PersistentMemory, _args: argparse.Namespace) -> int:
    cleared = await pm.clear_focus_directive()
    if cleared:
        print("focus cleared")
    else:
        print("focus: none was active")
    return 0


async def _main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="focus", description="Set / show / clear the operator focus directive."
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_set = sub.add_parser("set", help="set (replaces any active directive)")
    p_set.add_argument("text", help="the directive text")
    p_set.set_defaults(_fn=_cmd_set)

    p_show = sub.add_parser("show", help="show the active directive or 'none'")
    p_show.set_defaults(_fn=_cmd_show)

    p_clear = sub.add_parser("clear", help="tombstone the active directive")
    p_clear.set_defaults(_fn=_cmd_clear)

    args = parser.parse_args(argv)

    config = await load_config()
    pm = PersistentMemory(config)
    await pm.initialize()
    try:
        return await args._fn(pm, args)
    finally:
        await pm.close()


def main() -> None:
    sys.exit(asyncio.run(_main(sys.argv[1:])))


if __name__ == "__main__":
    main()
