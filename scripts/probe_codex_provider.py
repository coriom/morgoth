"""Exactly two synthetic CLI probes; no Morgoth config, DB or workload calls."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import shutil
import tempfile


def main() -> int:
    """Print only fixed verdicts; never child diagnostics or account metadata."""
    path = Path(__file__).resolve().parents[1] / "core/llm/codex_cli.py"
    spec = importlib.util.spec_from_file_location("codex_transport", path)
    assert spec and spec.loader
    transport = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(transport)
    env = transport.minimal_env()
    binary = shutil.which("codex", path=env.get("PATH", ""))
    if not binary:
        print("qualification: missing_binary")
        return 1
    try:
        with tempfile.TemporaryDirectory(prefix="codex-probe-check-", dir="/tmp") as cwd:
            transport.discover(binary, cwd, env)
    except transport.CodexCliError as exc:
        print(f"qualification: {exc.code}")
        return 1
    failed = False
    try:
        text = transport.invoke(binary, "default", env, "Reply exactly CODEX_PROVIDER_OK", 90)
        if text != "CODEX_PROVIDER_OK":
            raise transport.CodexCliError("unexpected_text")
        print("text: PASS")
    except transport.CodexCliError as exc:
        failed = True
        print(f"text: FAIL ({exc.code})")
    try:
        transport.capability_canary(binary, "default", env, 90)
        print("capability-canary: PASS (marker inaccessible; no tool events)")
    except transport.CodexCliError as exc:
        failed = True
        print(f"capability-canary: FAIL ({exc.code}); provider unavailable")
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
