"""Cycle-loop sleep ordering: sleep at BOTTOM, not top.

Prior behavior: the loop called `await asyncio.sleep(N*60)` BEFORE the
first cycle's body — so a restart cost N minutes of pure idle before
any work. Post-fix: sleep at bottom, first cycle runs immediately.
Inter-cycle cadence unchanged.

Grep-lock: a future edit that puts the sleep back at the top (which
would look identical in a diff review but silently reintroduce the
idle wait) breaks the build.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


BRAIN_SRC = Path("core/brain.py").read_text()


def test_run_autonomous_cycle_docstring_documents_bottom_placement():
    """The docstring must state the sleep-at-bottom decision. This is the
    'why' anchor — a future maintainer reading the loop sees the intent."""
    assert "Sleep placement: at the BOTTOM" in BRAIN_SRC
    assert "post-restart the first" in BRAIN_SRC


def test_sleep_is_after_the_body_not_before():
    """The `while True:` block in run_autonomous_cycle must have its
    shared cadence call after the body and exception handlers. Forced
    completion also calls that same cancellable helper before continuing."""
    # Grab the body of run_autonomous_cycle.
    match = re.search(
        r"async def run_autonomous_cycle\(self\) -> None:(.+?)async def ",
        BRAIN_SRC, re.DOTALL,
    )
    assert match, "run_autonomous_cycle not found in brain.py"
    body = match.group(1)
    sleep_line = "await self._sleep_between_cycles()"
    assert body.count(sleep_line) == 2
    helper = BRAIN_SRC.split("async def _sleep_between_cycles", 1)[1].split("async def ", 1)[0]
    assert helper.count("await asyncio.sleep(self._config.autonomous_cycle_minutes * 60)") == 1
    assert "except asyncio.CancelledError:" in helper and "raise" in helper
    # The shared bottom call must appear after the exception handlers.
    sleep_pos = body.rindex(sleep_line)
    cycle_start_pos = body.index('"Autonomous cycle starting"')
    assert sleep_pos > body.index("except Exception as e:") > cycle_start_pos, (
        "sleep must be at bottom of loop (after the cycle body), not top"
    )
    forced = body.index("await self._finalize_objective(obj, new_count, forced=True)")
    assert forced < body.index(sleep_line, forced) < body.index("continue", forced)


def test_no_top_of_loop_sleep_regression():
    """Grep-lock the specific anti-pattern: `while True:\\n            try:\\n
    await asyncio.sleep`. This is what the pre-fix loop looked like — a
    future refactor that reintroduces it fails here."""
    anti_pattern = re.compile(
        r"while True:\s*\n\s*try:\s*\n\s*await asyncio\.sleep\(self\._config\.autonomous_cycle_minutes",
    )
    assert not anti_pattern.search(BRAIN_SRC), (
        "regression: sleep is back at the TOP of the cycle loop"
    )
