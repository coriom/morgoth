"""Chained-creation grep-locks: when the empty-queue cycle successfully
creates an objective, the loop must `continue` past the sleep so the
next iteration works on it in the same cadence slot. cycle_count still
increments EXACTLY ONCE per objective — inside the `if objectives:`
branch, on the WORK pass."""

from __future__ import annotations

import inspect


class TestChainedCreationWiring:
    def test_chain_flag_set_only_when_create_objective_succeeded(self):
        from core import brain
        src = inspect.getsource(brain.Brain.run_autonomous_cycle)
        # Detects a successful create_objective in tool_results.
        assert '_chain_creation = any(' in src
        assert 'tr.get("tool") == "create_objective"' in src
        assert 'tr["result"].get("success")' in src

    def test_work_branch_disables_chaining(self):
        from core import brain
        src = inspect.getsource(brain.Brain.run_autonomous_cycle)
        # Work branch explicitly turns off chaining so a work cycle
        # never chains into another; that would burst cadence.
        assert "_chain_creation = False" in src

    def test_continue_skips_sleep_only_when_chaining(self):
        from core import brain
        src = inspect.getsource(brain.Brain.run_autonomous_cycle)
        # The continue is inside `if _chain_creation:` — verify by
        # position (chain flag defined before, continue after).
        flag_pos = src.find("_chain_creation = any(")
        cont_pos = src.find("if _chain_creation:")
        sleep_pos = src.find("await asyncio.sleep(self._config.autonomous_cycle_minutes")
        assert 0 < flag_pos < cont_pos < sleep_pos

    def test_increment_cycle_count_still_called_only_in_work_branch(self):
        # cycle_count must increment exactly ONCE per work cycle; chained
        # creation does not add a second call site. Structural check:
        # there's still exactly one call to increment_cycle_count in the
        # cycle loop.
        from core import brain
        src = inspect.getsource(brain.Brain.run_autonomous_cycle)
        assert src.count("increment_cycle_count") == 1
