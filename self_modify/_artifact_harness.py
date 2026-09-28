"""Sandbox harness for the artifact check.

Runs INSIDE the same bwrap wrapper as gate_tests (see
``gates.wrap_command_in_sandbox``). Given a rendered proposal file
and a recorded HTTP response body, this harness:

  1. Imports the tool module dynamically from the given file path.
  2. Locates the ``BaseTool`` subclass with ``is_data_source=True``.
  3. Constructs it with a fake ``httpx.AsyncClient`` that returns the
     recorded body on every ``.get()`` call.
  4. Awaits ``execute()``.
  5. Prints a single JSON object to stdout with the outcome:
       {"ok": true,  "digest": {...}, "meta": {...}}
       {"ok": false, "kind": "module_exec"|"no_data_source"|
                              "instantiate"|"execute"|"execute_returned_failure"|
                              "all_null",
        "error": "<type>: <msg>", "traceback_tail": "<last 500 chars>"}

Never raises out; every failure mode is a well-shaped JSON payload.
The host reads ONLY this JSON — the harness's stderr, exit code,
and any file it writes are irrelevant. The rendered proposal file
never touches the host process.

WHY A SEPARATE PROCESS. The rendered file is LLM-authored code
carrying LLM-supplied strings interpolated via ``repr()``. Even the
repr()-based renderer + snake_case-restricted names cannot rule out
a subtle escaping bug in the template: the safe assumption is that
any rendered file could be adversarial. Running it inside the bwrap
sandbox — where /tmp is a fresh tmpfs, ~/Morgoth/morgoth/.env is
invisible, ~/.claude is invisible, the DB socket is unreachable —
turns "one interpolation flaw = full host RCE" into "the harness
prints a JSON failure".

USAGE
-----
  python -m self_modify._artifact_harness <tool_file> <body_json_file>

Both paths are relative to the sandbox cwd.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import traceback
from pathlib import Path
from types import SimpleNamespace
from typing import Any


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, default=str), flush=True)


def _tail(exc: BaseException) -> str:
    return "".join(
        traceback.format_exception(type(exc), exc, exc.__traceback__)
    )[-500:]


def _load_module(source_path: Path):
    spec = importlib.util.spec_from_file_location("_artifact_tool", str(source_path))
    if spec is None or spec.loader is None:
        raise ImportError(f"spec_from_file_location returned None for {source_path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _find_tool_class(mod: Any):
    for val in vars(mod).values():
        if isinstance(val, type) and getattr(val, "is_data_source", False):
            return val
    return None


async def _run(cls: type, body: Any) -> dict[str, Any]:
    from unittest.mock import AsyncMock, MagicMock

    fake_resp = SimpleNamespace(
        status_code=200, json=lambda: body,
        raise_for_status=lambda: None,
    )
    fake_client = MagicMock()
    fake_client.get = AsyncMock(return_value=fake_resp)
    fake_client.aclose = AsyncMock()
    cfg = SimpleNamespace(
        permissions=SimpleNamespace(
            permissions=SimpleNamespace(can_access_internet=True),
        )
    )
    try:
        tool = cls(cfg, client=fake_client)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "kind": "instantiate",
                "error": f"{type(exc).__name__}: {exc}",
                "traceback_tail": _tail(exc)}
    try:
        out = await tool.execute()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "kind": "execute",
                "error": f"{type(exc).__name__}: {exc}",
                "traceback_tail": _tail(exc)}
    if not (isinstance(out, dict) and out.get("success")):
        return {"ok": False, "kind": "execute_returned_failure",
                "error": (out or {}).get("error") if isinstance(out, dict) else str(out),
                "out": out}
    values = out.get("result", {}) or {}
    if not values or all(v is None for v in values.values()):
        return {"ok": False, "kind": "all_null",
                "error": "digest all-null on recorded body",
                "digest": values}
    return {"ok": True, "digest": values, "meta": out.get("metadata") or {}}


def main() -> int:
    if len(sys.argv) < 3:
        _emit({"ok": False, "kind": "usage",
               "error": "usage: python -m self_modify._artifact_harness <tool_file> <body_json>"})
        return 2
    tool_file = Path(sys.argv[1])
    body_file = Path(sys.argv[2])
    try:
        body = json.loads(body_file.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        _emit({"ok": False, "kind": "body_read",
               "error": f"{type(exc).__name__}: {exc}"})
        return 0
    try:
        mod = _load_module(tool_file)
    except Exception as exc:  # noqa: BLE001
        _emit({"ok": False, "kind": "module_exec",
               "error": f"{type(exc).__name__}: {exc}",
               "traceback_tail": _tail(exc)})
        return 0
    cls = _find_tool_class(mod)
    if cls is None:
        _emit({"ok": False, "kind": "no_data_source",
               "error": "no BaseTool subclass with is_data_source=True in the rendered module"})
        return 0
    result = asyncio.run(_run(cls, body))
    _emit(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
