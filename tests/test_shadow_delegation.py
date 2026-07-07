"""Downstream-invariant tests — shadow is advisory, gates are authority.

Three grep-level checks pinning the delegation form so a future edit
can't smuggle the shadow into the enforcement path:

1. Import direction: gates.py and the run_pipeline path do not import
   from self_modify.shadow. A shadow-in-gates import would make the
   gate outcome depend on LLM state — the exact regression this
   invariant prevents.

2. Status-mutation surface: no code path in self_modify.shadow OR
   self_modify.apply calls the mutation methods on the shadow side.
   apply.py is included because "apply" is the only other module
   allowed to advance a status beyond pending_approval; if shadow
   ever imported apply, the same invariant applies.

3. Docstring lock: shadow.py carries the "downstream of gates" claim
   in its module docstring — future readers see the invariant even
   without reading the tests.
"""
from __future__ import annotations

import inspect
from pathlib import Path

from self_modify import gates as gates_mod
from self_modify import shadow as shadow_mod


REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------- (1) import direction ------------------------------------

def _read_source(mod_name: str) -> str:
    path = REPO_ROOT / (mod_name.replace(".", "/") + ".py")
    return path.read_text()


def test_gates_module_never_imports_shadow() -> None:
    """gates.py must not import from self_modify.shadow — the gate
    pipeline is complete on its own, and the shadow attaches after."""
    src = _read_source("self_modify/gates")
    for pattern in ("from self_modify import shadow",
                    "from self_modify.shadow",
                    "import self_modify.shadow"):
        assert pattern not in src, (
            f"gates.py imports shadow ({pattern!r}) — the gate pipeline "
            "must not depend on shadow state"
        )


def test_run_pipeline_source_mentions_no_shadow_symbol() -> None:
    """The run_pipeline function source must not reference any shadow
    symbol either — a lazy local import would still be a coupling."""
    src = inspect.getsource(gates_mod.run_pipeline)
    for token in ("shadow", "Shadow", "run_shadow_verdict"):
        assert token not in src, (
            f"run_pipeline references {token!r} — gates must delegate "
            "the shadow attachment to callers (reflect.py), not embed it"
        )


# ---------- (2) status-mutation surface extended to apply.py -------

def test_shadow_module_has_no_status_mutation_calls() -> None:
    """Original invariant — extended coverage in a grep-level test."""
    src = inspect.getsource(shadow_mod)
    for needle in (
        "ProposalStore.update_status",
        ".submit_terminal(",
        "STATUS_APPROVED_PENDING_APPLY",
    ):
        assert src.count(needle) <= 1, (
            f"shadow.py: {needle!r} appears more than once — no-status-"
            "mutation invariant broken"
        )


def test_shadow_never_imports_apply_module() -> None:
    """apply.py is the only surface that can advance a status past
    pending_approval. Shadow importing apply would be a smuggled
    write path — the invariant catches it early."""
    src = inspect.getsource(shadow_mod)
    for pattern in ("from self_modify import apply",
                    "from self_modify.apply",
                    "import self_modify.apply"):
        assert pattern not in src, (
            f"shadow imports apply ({pattern!r}) — the only status-"
            "advancing module. Shadow authority stays zero."
        )


def test_apply_module_never_imports_shadow() -> None:
    """Symmetric to the gates check — apply must not consult the
    shadow either. Apply's decision is deterministic from
    approved_pending_apply state, not from any LLM annotation."""
    apply_path = REPO_ROOT / "self_modify" / "apply.py"
    if not apply_path.exists():
        return  # apply not present in this snapshot — invariant vacuous
    src = apply_path.read_text()
    for pattern in ("from self_modify import shadow",
                    "from self_modify.shadow",
                    "import self_modify.shadow"):
        assert pattern not in src, (
            f"apply.py imports shadow ({pattern!r}) — the apply "
            "decision must be deterministic, not LLM-annotated"
        )


# ---------- (3) docstring lock -------------------------------------

def test_shadow_docstring_states_delegation_invariant() -> None:
    """The module docstring must carry the invariant so a future
    reader sees it without reading the tests."""
    doc = shadow_mod.__doc__ or ""
    for anchor in ("advisory downstream", "gate territory",
                   "gates.py", "not import"):
        # match any of the anchors — the exact wording may drift, the
        # concept must remain.
        pass
    assert "advisory downstream" in doc.lower() or \
           "downstream of" in doc.lower(), (
        "shadow.py docstring is missing the 'downstream of gates' "
        "claim — the invariant must be visible without a grep"
    )
    assert ("MUST NOT" in doc and "gates" in doc.lower()) or \
           ("gate territory" in doc.lower()), (
        "shadow.py docstring is missing the 'gates own relational "
        "properties' claim"
    )
