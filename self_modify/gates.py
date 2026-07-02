"""Gates — the runtime pipeline for self-modify proposals.

Two gates, in order:

1. ``gate_zone``:  pure classification via ``zones.classify_proposal``.
   Red → ``zone_rejected`` (terminal). Green → advance.

2. ``gate_tests``: copy the working tree to a sandbox, write the proposal's
   new_file content into the sandbox at ``target_path``, run the FULL
   pytest suite from the sandbox using the main venv's Python. Non-zero
   exit → ``tests_failed`` (terminal, with the tail of pytest output as
   reason). Zero exit → ``pending_approval``.

``run_pipeline`` is the only entry point.

Notes
-----
- The sandbox NEVER writes into the live tree. Sandbox copy excludes
  ``.venv``, ``.git``, ``data``, ``__pycache__``, ``vault``, ``backups``
  to keep the copy cheap. Sandbox is cleaned in ``finally``.
- APPLY DOES NOT EXIST in this module. Reaching ``pending_approval`` /
  ``approved_pending_apply`` is a status-only transition; the file the
  proposal describes is never merged into the live tree by this code.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path
from typing import Any

from loguru import logger

from self_modify import proposals as P
from self_modify import zones


# Directory names to skip when copying the working tree into the sandbox.
_SANDBOX_IGNORE = shutil.ignore_patterns(
    ".venv",
    ".git",
    "data",
    "__pycache__",
    "vault",
    "backups",
    "*.egg-info",
    ".pytest_cache",
    "node_modules",
)

# Path to the venv interpreter used to drive pytest inside the sandbox.
# Kept absolute so it works regardless of caller cwd or sandbox path.
_VENV_PYTHON = "/home/corio/Morgoth/morgoth/.venv/bin/python"

# Hard timeout for pytest under the sandbox — a proposal that hangs the
# suite is a failure, not an outage.
_PYTEST_TIMEOUT_SECS = 180


async def gate_zone(
    store: P.ProposalStore,
    proposal: dict[str, Any],
) -> str:
    """Classify + persist. Return the resulting status."""
    zone = zones.classify_proposal(
        target_path=proposal["target_path"],
        change_type=proposal["change_type"],
    )
    logger.info(
        "gate_zone: proposal_id={} target={!r} change_type={!r} → zone={}",
        proposal["proposal_id"],
        proposal["target_path"],
        proposal["change_type"],
        zone,
    )
    if zone != "green":
        reason = (
            f"zone={zone}: {proposal['change_type']} on {proposal['target_path']!r} "
            f"is not in the additive-only green zone"
        )
        await store.update_status(
            str(proposal["proposal_id"]),
            P.STATUS_ZONE_REJECTED,
            reason,
        )
        return P.STATUS_ZONE_REJECTED
    # green — leave status untouched; caller advances to gate_tests
    return "green"


def _run_pytest_in_sandbox(sandbox: Path) -> subprocess.CompletedProcess[str]:
    """Blocking call — run pytest -q from ``sandbox``. Used via to_thread."""
    return subprocess.run(
        [_VENV_PYTHON, "-m", "pytest", "-q"],
        cwd=str(sandbox),
        capture_output=True,
        text=True,
        timeout=_PYTEST_TIMEOUT_SECS,
    )


async def gate_tests(
    store: P.ProposalStore,
    proposal: dict[str, Any],
    repo_root: Path = Path("/home/corio/Morgoth/morgoth"),
) -> str:
    """Sandbox pytest gate. Returns the resulting status."""
    proposal_id = str(proposal["proposal_id"])
    change_type = proposal["change_type"]

    # Only new_file is supported for green today (edits are all red).
    # Defensive: if we ever expand to edits, this branch will need to
    # apply the diff instead of writing a whole file.
    if change_type != "new_file":
        reason = f"gate_tests: unsupported change_type={change_type!r}"
        await store.update_status(proposal_id, P.STATUS_TESTS_FAILED, reason)
        return P.STATUS_TESTS_FAILED

    sandbox_root = Path("/tmp/morgoth_sandbox")
    sandbox_root.mkdir(parents=True, exist_ok=True)
    sandbox = sandbox_root / f"proposal_{proposal_id}"
    if sandbox.exists():
        shutil.rmtree(sandbox)
    try:
        # 1. Copy live tree to sandbox (excludes venv/git/data/etc).
        logger.info("gate_tests: copying tree to sandbox {}", sandbox)
        await asyncio.to_thread(
            shutil.copytree, str(repo_root), str(sandbox), ignore=_SANDBOX_IGNORE
        )
        # 2. Write proposal's content at target_path inside sandbox.
        target = sandbox / proposal["target_path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(proposal["content"], encoding="utf-8")
        # 3. Run pytest.
        logger.info("gate_tests: running pytest in {}", sandbox)
        try:
            completed = await asyncio.to_thread(_run_pytest_in_sandbox, sandbox)
        except subprocess.TimeoutExpired as exc:
            reason = f"gate_tests: pytest timed out after {_PYTEST_TIMEOUT_SECS}s"
            logger.warning("{}: {}", reason, exc)
            await store.update_status(proposal_id, P.STATUS_TESTS_FAILED, reason)
            return P.STATUS_TESTS_FAILED

        if completed.returncode != 0:
            # Capture the tail of stdout+stderr for the operator (kept
            # bounded — full pytest output can be huge).
            tail = (completed.stdout + completed.stderr)[-2000:]
            reason = f"gate_tests: pytest exit={completed.returncode}\n---tail---\n{tail}"
            logger.warning(
                "gate_tests: FAIL proposal_id={} exit={}",
                proposal_id,
                completed.returncode,
            )
            await store.update_status(proposal_id, P.STATUS_TESTS_FAILED, reason)
            return P.STATUS_TESTS_FAILED

        # PASS
        logger.info("gate_tests: PASS proposal_id={}", proposal_id)
        await store.update_status(
            proposal_id,
            P.STATUS_PENDING_APPROVAL,
            "gate_tests: pytest passed in sandbox",
        )
        return P.STATUS_PENDING_APPROVAL
    finally:
        # Always clean the sandbox — success or failure.
        if sandbox.exists():
            shutil.rmtree(sandbox, ignore_errors=True)


async def run_pipeline(
    store: P.ProposalStore,
    proposal: dict[str, Any],
    repo_root: Path = Path("/home/corio/Morgoth/morgoth"),
) -> str:
    """Zone gate then (if green) tests gate. Returns the final status."""
    zone_result = await gate_zone(store, proposal)
    if zone_result != "green":
        return zone_result
    return await gate_tests(store, proposal, repo_root=repo_root)
