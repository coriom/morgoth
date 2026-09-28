"""Sandbox runner for the artifact check.

Copies the current repo tree into a temporary sandbox, writes the
proposal's rendered file at its target_path, writes the recorded
response body as JSON, and invokes ``_artifact_harness`` inside the
SAME bwrap wrapper gate_tests uses (see
``gates.wrap_command_in_sandbox``). The host process reads ONLY the
harness's stdout JSON — the rendered proposal file NEVER touches
the host Python interpreter.

WHY THIS EXISTS. Before 2026-09-30 the artifact check ran the
rendered file via ``exec(compile(content, ...))`` in the reflect /
recheck process on the host. That was a scope escalation: the file
is LLM-authored code carrying LLM-supplied strings interpolated via
``repr()``. A subtle escaping bug in the template (or a future
change to the interpolation contract) would let a proposal execute
arbitrary code with the operator's env: .env, ~/.claude tokens,
Postgres credentials, the DB itself. The safe assumption is that
any rendered file could be adversarial. This runner turns that risk
into a JSON verdict from inside a bwrap with /tmp as a fresh tmpfs,
~/Morgoth/morgoth/.env invisible, and ~/.claude invisible.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

from self_modify import gates


REPO_ROOT = Path("/home/corio/Morgoth/morgoth")
_HARNESS_TIMEOUT_SECS = 60


@dataclass
class ArtifactResult:
    ok: bool
    kind: str        # "ok" | "module_exec" | "execute" | "all_null" | ...
    message: str
    detail: dict[str, Any]


def run_artifact_in_sandbox(
    proposal_content: str,
    target_path: str,
    body: Any,
    *,
    repo_root: Path = REPO_ROOT,
    sandbox_root: Path | None = None,
) -> ArtifactResult:
    """Execute the proposal's rendered file inside the sandbox against
    the recorded body. Returns the parsed harness verdict.

    NEVER runs the rendered file in-process — that would defeat the
    entire point (see module docstring). If the sandbox itself is
    unavailable (missing bwrap/unshare/systemd-run), returns a
    ``kind='sandbox_unavailable'`` result — treated as REJECT by the
    caller (fail-closed).
    """
    if sandbox_root is None:
        sandbox_root = gates._SANDBOX_ROOT
    sandbox_root.mkdir(parents=True, exist_ok=True)
    import uuid as _uuid
    sandbox = sandbox_root / f"artifact_{_uuid.uuid4().hex[:12]}"
    if sandbox.exists():
        shutil.rmtree(sandbox)
    try:
        shutil.copytree(str(repo_root), str(sandbox),
                        ignore=gates._SANDBOX_IGNORE)
        # Re-touch mtime so gate_tests' sweep-collision fix applies here
        # too (repo_root mtime propagated via copystat).
        import os as _os
        _os.utime(sandbox, None)
        target = sandbox / target_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(proposal_content, encoding="utf-8")
        body_file = sandbox / "_probe_body.json"
        body_file.write_text(json.dumps(body, default=str), encoding="utf-8")
        # Guard against a stray secret file — sandbox must not contain
        # .env under any circumstance.
        for stray in list(sandbox.rglob(".env")):
            if stray.is_file():
                raise RuntimeError(
                    f"artifact sandbox contains .env at {stray} — refusing to run"
                )
        try:
            argv = gates.wrap_command_in_sandbox(
                sandbox,
                [gates._VENV_PYTHON, "-m", "self_modify._artifact_harness",
                 target_path, "_probe_body.json"],
            )
        except gates.SandboxUnavailableError as exc:
            return ArtifactResult(
                ok=False, kind="sandbox_unavailable",
                message=f"sandbox unavailable ({exc}) — refusing to exec artifact",
                detail={"reason": str(exc)},
            )
        try:
            completed = subprocess.run(
                argv, capture_output=True, text=True,
                timeout=_HARNESS_TIMEOUT_SECS,
                env=gates._hardened_outer_env(),
                start_new_session=True,
            )
        except subprocess.TimeoutExpired:
            return ArtifactResult(
                ok=False, kind="timeout",
                message=f"harness timed out after {_HARNESS_TIMEOUT_SECS}s",
                detail={},
            )
        # Parse the LAST JSON line from stdout (harness prints exactly
        # one JSON object; systemd-run may prepend scope-management
        # noise on some hosts).
        stdout = completed.stdout or ""
        payload: dict[str, Any] | None = None
        for line in reversed(stdout.strip().splitlines()):
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                payload = json.loads(line)
                break
            except json.JSONDecodeError:
                continue
        if payload is None:
            logger.warning(
                "artifact_runner: no JSON in harness stdout (exit={}); "
                "stderr_tail={!r}",
                completed.returncode, (completed.stderr or "")[-300:],
            )
            return ArtifactResult(
                ok=False, kind="no_output",
                message=f"harness produced no JSON (exit={completed.returncode})",
                detail={"stderr_tail": (completed.stderr or "")[-500:]},
            )
        if payload.get("ok"):
            digest = payload.get("digest") or {}
            meta = payload.get("meta") or {}
            missing = meta.get("missing") or []
            msg = f"executed against recorded body → {len(digest)} value(s) extracted"
            if missing:
                msg += f"; missing={missing}"
            return ArtifactResult(
                ok=True, kind="ok", message=msg,
                detail={"digest": digest, "meta": meta, "missing": missing},
            )
        return ArtifactResult(
            ok=False, kind=payload.get("kind") or "unknown",
            message=str(payload.get("error") or payload)[:400],
            detail=payload,
        )
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)
