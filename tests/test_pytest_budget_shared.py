"""Single-source pytest budget across self_modify.

Three dead proposals (742e7d5e, acdba238, 1182ee96) trace to
hardcoded time budgets that diverged from the measured suite:
  - 180s sandbox limit  → 742e7d5e (tests_failed on infra)
  - 1380s sandbox limit → acdba238 (tests_failed on infra)
  - 300s apply limit    → 1182ee96 (apply_failed_rolled_back on infra)

Both pytest-invoking subprocesses in self_modify/ (gate_tests +
apply's live pytest) now consume ONE derived constant, so the class
cannot recur silently.
"""
from __future__ import annotations

import importlib
import os
from unittest.mock import patch

from self_modify import apply as apply_mod
from self_modify import gates as gates_mod


def test_apply_reads_shared_pytest_budget() -> None:
    """apply._PYTEST_TIMEOUT_SECS is not a literal — it comes from
    gates.PYTEST_BUDGET_SECS."""
    assert apply_mod._PYTEST_TIMEOUT_SECS == gates_mod.PYTEST_BUDGET_SECS
    # The value must be well above the old 300s ceiling that killed
    # 1182ee96 (below the sandbox budget of ~6720s).
    assert apply_mod._PYTEST_TIMEOUT_SECS >= 3000


def test_gates_pytest_budget_is_the_shared_alias() -> None:
    """PYTEST_BUDGET_SECS aliases SANDBOX_TIMEOUT_SECONDS — the
    sandbox constant remains readable for existing env docs, the new
    name is the canonical import point for other modules."""
    assert gates_mod.PYTEST_BUDGET_SECS == gates_mod.SANDBOX_TIMEOUT_SECONDS


def test_no_hardcoded_pytest_timeout_literals_in_self_modify() -> None:
    """Grep-negative: no literal pytest timeout remains outside
    gates.py's derivation site. A future refactor that reintroduces a
    hardcoded 300/180/etc. constant trips here."""
    import inspect
    for mod, name in ((apply_mod, "apply"), (gates_mod, "gates")):
        src = inspect.getsource(mod)
        # In gates.py the literals appear in the derivation formula
        # itself. We only reject SIGNS of a hardcoded pytest timeout —
        # a literal like ``= 300`` or ``= 180`` on a
        # _PYTEST_TIMEOUT_SECS or similar name.
        for suspicious in (
            "_PYTEST_TIMEOUT_SECS = 300",
            "_PYTEST_TIMEOUT_SECS = 180",
            "_PYTEST_TIMEOUT_SECS = 1380",
        ):
            assert suspicious not in src, (
                f"{name}.py contains hardcoded pytest timeout: "
                f"{suspicious!r}"
            )


def test_env_override_propagates_to_both_sites() -> None:
    """SANDBOX_TIMEOUT_SECONDS override → both gate_tests + apply see
    the new value after module reload. This is the operator-triage
    escape hatch."""
    with patch.dict(os.environ, {"SANDBOX_TIMEOUT_SECONDS": "9999"}):
        importlib.reload(gates_mod)
        importlib.reload(apply_mod)
        assert gates_mod.SANDBOX_TIMEOUT_SECONDS == 9999
        assert gates_mod.PYTEST_BUDGET_SECS == 9999
        assert apply_mod._PYTEST_TIMEOUT_SECS == 9999
    # Restore default.
    with patch.dict(os.environ, {"SANDBOX_TIMEOUT_SECONDS": ""}):
        importlib.reload(gates_mod)
        importlib.reload(apply_mod)
        assert gates_mod.PYTEST_BUDGET_SECS == gates_mod._SANDBOX_DEFAULT_TIMEOUT_SECS


def test_apply_and_gate_tests_use_same_argv_shape() -> None:
    """apply's live pytest MUST use xdist too — a live serial run at
    ~2700s wall time would routinely exceed even the sandbox xdist
    budget. Grep-level check that ``-n`` reaches the live invocation."""
    import inspect
    src = inspect.getsource(apply_mod._run_live_pytest)
    assert "-n" in src and "auto" in src, (
        "apply._run_live_pytest missing '-n auto' — divergent budget "
        "envelope from the sandbox"
    )


def test_class_cost_named_in_docstring() -> None:
    """The bug class's cost — three dead proposals — is named at the
    derivation site so a future reader sees the reason for the
    single-source constraint."""
    import inspect
    src = inspect.getsource(gates_mod)
    assert "1182ee96" in src
    assert "PYTEST_BUDGET_SECS" in src
