"""Single-source LLM call budget across the reflect provider branches.

Fourth instance of the hardcoded-budget class: proposal 9e94f77e died
rejected_shape when its RETRY hit the 180s claude-cli ceiling on a
loaded host. The anthropic branch had the same shape at 60s.

Both provider branches now consume REFLECT_LLM_TIMEOUT_SECONDS
(default 600s, env-overridable). The class-closure discipline matches
gates.PYTEST_BUDGET_SECS from be7ab20.
"""
from __future__ import annotations

import os
import subprocess
import sys

from self_modify import reflect_llm as R


def test_shared_constant_defined() -> None:
    """REFLECT_LLM_TIMEOUT_SECONDS is the single source both branches read."""
    assert isinstance(R.REFLECT_LLM_TIMEOUT_SECONDS, int)
    assert R.REFLECT_LLM_TIMEOUT_SECONDS >= 300  # well above old 180


def test_claude_cli_reads_shared_constant() -> None:
    """The claude-cli branch's CLAUDE_CLI_TIMEOUT_SECS is the shared value."""
    assert R.CLAUDE_CLI_TIMEOUT_SECS == R.REFLECT_LLM_TIMEOUT_SECONDS


def test_anthropic_reads_shared_constant() -> None:
    """The anthropic branch's HTTP-timeout is the shared value."""
    assert R.ANTHROPIC_TIMEOUT_SECS == float(R.REFLECT_LLM_TIMEOUT_SECONDS)


def test_no_hardcoded_llm_timeout_literals_remain() -> None:
    """Grep-negative: no ``= 180`` or ``= 60.0`` on TIMEOUT constants
    outside the shared derivation. A future revert to the old-style
    literal trips here."""
    import inspect
    src = inspect.getsource(R)
    for suspicious in (
        "CLAUDE_CLI_TIMEOUT_SECS: int = 180",
        "ANTHROPIC_TIMEOUT_SECS: float = 60.0",
    ):
        assert suspicious not in src, (
            f"reflect_llm.py contains hardcoded LLM timeout: {suspicious!r}"
        )


def test_env_override_propagates_to_both_branches() -> None:
    """Env override propagates to BOTH branch constants. Runs the
    check in a fresh subprocess to avoid polluting the loaded
    ``self_modify.reflect_llm`` for other tests (a reload here
    swaps the ReflectLLMError class identity and breaks
    ``pytest.raises(ReflectLLMError)`` in downstream tests)."""
    for override, expected in (("1234", 1234), ("", 600)):
        env = os.environ.copy()
        if override:
            env["REFLECT_LLM_TIMEOUT_SECONDS"] = override
        else:
            env.pop("REFLECT_LLM_TIMEOUT_SECONDS", None)
        result = subprocess.run(
            [sys.executable, "-c",
             "from self_modify import reflect_llm as r; "
             "print(r.REFLECT_LLM_TIMEOUT_SECONDS, "
             "r.CLAUDE_CLI_TIMEOUT_SECS, r.ANTHROPIC_TIMEOUT_SECS)"],
            env=env,
            cwd="/home/corio/Morgoth/morgoth",
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == 0, result.stderr
        parts = result.stdout.split()
        assert int(parts[0]) == expected
        assert int(parts[1]) == expected
        assert float(parts[2]) == float(expected)


def test_timeout_error_message_unchanged() -> None:
    """The user-visible timeout message stays the clean one-liner
    established by the reflect_llm hygiene contract — grep-lock on
    the format so a refactor can't leak details."""
    import inspect
    src = inspect.getsource(R)
    # Format is: "claude CLI timed out after Ns"
    assert 'f"claude CLI timed out after {CLAUDE_CLI_TIMEOUT_SECS}s"' in src


def test_class_cost_documented_at_site() -> None:
    """The bug class's fourth-instance cost (9e94f77e) is named at
    the derivation site so a future reader sees the reason for the
    single-source constraint."""
    import inspect
    src = inspect.getsource(R)
    assert "9e94f77e" in src
    assert "REFLECT_LLM_TIMEOUT_SECONDS" in src
