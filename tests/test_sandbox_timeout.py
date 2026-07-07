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
    silent shrink to the old 180s cannot land unnoticed."""
    assert gates_mod._SANDBOX_HEADROOM >= 2.0
    assert gates_mod._SANDBOX_MEASURED_SECS >= 1000  # not the old 180
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
