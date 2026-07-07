"""SANDBOX_TIMEOUT_SECONDS derivation + env-override contract.

The sandbox-pytest timeout must scale with the measured suite time,
not stay pinned at a value that will silently kill every valid
proposal once the suite grows past it. The env override lets an
operator triage a specific proposal without editing the constant.
"""
from __future__ import annotations

import importlib
import os
from unittest.mock import patch

from self_modify import gates as gates_mod


def test_default_derives_from_measured_with_headroom() -> None:
    """Default = ceil(measured × 2.5 / 60) × 60. Locked so a
    silent shrink to the old 180s cannot land unnoticed. Post-xdist
    the measured value dropped to ~547s (12-core parallel); the
    lower bound is set well above 180s but low enough to accept the
    xdist speedup."""
    assert gates_mod._SANDBOX_HEADROOM >= 2.0
    assert gates_mod._SANDBOX_MEASURED_SECS > 400  # not the old 180
    import math
    expected = int(math.ceil(
        gates_mod._SANDBOX_MEASURED_SECS * gates_mod._SANDBOX_HEADROOM / 60
    ) * 60)
    assert gates_mod._SANDBOX_DEFAULT_TIMEOUT_SECS == expected
    # ≥ 2× measured — the anti-regression contract.
    assert (gates_mod._SANDBOX_DEFAULT_TIMEOUT_SECS
            >= 2 * gates_mod._SANDBOX_MEASURED_SECS)


def test_env_override_respected() -> None:
    with patch.dict(os.environ, {"SANDBOX_TIMEOUT_SECONDS": "9999"}):
        importlib.reload(gates_mod)
        assert gates_mod.SANDBOX_TIMEOUT_SECONDS == 9999
        assert gates_mod._PYTEST_TIMEOUT_SECS == 9999
    with patch.dict(os.environ, {"SANDBOX_TIMEOUT_SECONDS": ""}):
        importlib.reload(gates_mod)
        assert gates_mod.SANDBOX_TIMEOUT_SECONDS == gates_mod._SANDBOX_DEFAULT_TIMEOUT_SECS


def test_env_override_ignores_invalid() -> None:
    """Bad env values fall back to the default rather than crashing
    the gate at import time."""
    for bad in ("abc", "-1", "0", "  "):
        with patch.dict(os.environ, {"SANDBOX_TIMEOUT_SECONDS": bad}):
            importlib.reload(gates_mod)
            assert gates_mod.SANDBOX_TIMEOUT_SECONDS == gates_mod._SANDBOX_DEFAULT_TIMEOUT_SECS


def test_subset_run_alternative_rejected_in_docstring() -> None:
    """The rationale comment names the rejected alternative so a
    future reader can't silently swap in a subset run to shrink the
    budget."""
    import inspect
    src = inspect.getsource(gates_mod)
    assert "Subset-run alternative is REJECTED" in src


def test_xdist_wired_into_isolated_argv() -> None:
    """``-n auto`` must appear in the isolated pytest argv — the
    xdist speedup is the reason the timeout could drop from 6780s to
    1380s. Removing it silently would push suite time back to ~2701s
    and risk timeouts."""
    from pathlib import Path
    argv = gates_mod._build_pytest_argv(Path("/tmp/sbx"), isolated=True)
    # The isolated form packs the pytest call into a shell string —
    # check the inline command carries -n auto.
    joined = " ".join(argv)
    assert "-n auto" in joined, (
        f"isolated argv missing '-n auto': {joined!r}"
    )


def test_xdist_wired_into_nonisolated_argv() -> None:
    """Same wiring on the fallback path — pytest-xdist is not
    conditional on isolation posture."""
    from pathlib import Path
    argv = gates_mod._build_pytest_argv(Path("/tmp/sbx"), isolated=False)
    assert "-n" in argv and "auto" in argv, (
        f"non-isolated argv missing xdist flag: {argv!r}"
    )
