"""bin/morgoth reachability contract.

The wrapper delegates to ``self_modify.cli`` for every proposal-side
subcommand. Silent drift here (a new self_modify.cli subcommand that
the wrapper doesn't dispatch to) means the operator has to remember
the raw ``python -m self_modify.cli <cmd>`` invocation. Both times
this has been observed (amend + recheck, chantier 4) the missing arm
went unnoticed until the operator hit it live.

These tests are a grep-lock on the dispatch table — every subparser
name in ``self_modify.cli`` MUST correspond to a case in the
wrapper's dispatch case statement AND to a usage line.
"""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
BIN_MORGOTH = REPO_ROOT / "bin" / "morgoth"


def _cli_subcommands() -> set[str]:
    """Return the set of subparser names exposed by self_modify.cli."""
    import self_modify.cli as _cli
    parser: argparse.ArgumentParser = _cli.build_parser() \
        if hasattr(_cli, "build_parser") else None
    if parser is None:
        # cli.py doesn't currently export a build_parser helper; walk
        # the AST of the module source instead. Kept AST-based so this
        # lock doesn't require a refactor of the module we're locking.
        import ast
        tree = ast.parse((REPO_ROOT / "self_modify" / "cli.py")
                          .read_text(encoding="utf-8"))
        names: set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            attr = getattr(fn, "attr", None)
            if attr != "add_parser":
                continue
            if not node.args:
                continue
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                names.add(first.value)
        return names
    subs_action = next(
        (a for a in parser._actions
         if isinstance(a, argparse._SubParsersAction)),
        None,
    )
    return set(subs_action.choices) if subs_action else set()


def test_bin_morgoth_exists_and_executable() -> None:
    assert BIN_MORGOTH.exists(), (
        "bin/morgoth is the operator-facing wrapper (symlink target "
        "for ~/.local/bin/morgoth). Missing = every symlinked shell "
        "invocation breaks."
    )
    # Must be executable (bit set) — a non-x file requires an explicit
    # `bash` prefix that the operator won't remember.
    assert BIN_MORGOTH.stat().st_mode & 0o111, (
        f"bin/morgoth exists but lacks the executable bit"
    )


def test_bin_morgoth_dispatches_every_self_modify_cli_subcommand() -> None:
    """Every subparser in self_modify.cli MUST have a matching arm in
    the wrapper's dispatch case + a delegating cmd_<name>() function.
    Prevents amend/recheck-class silent drift where the wrapper is
    behind the module surface."""
    text = BIN_MORGOTH.read_text(encoding="utf-8")
    cli_subs = _cli_subcommands()
    # `list` is exposed via the wrapper's `proposals` alias — not a
    # missing dispatch. Same idea for the reflect/scout/rail/session/
    # env/models/audit subparsers whose wrapper names are unchanged.
    aliased = {"list": "proposals",
               "rail": "rail-check",
               "session": "session-report"}
    missing_case: list[str] = []
    missing_fn: list[str] = []
    for name in sorted(cli_subs):
        wrapper_name = aliased.get(name, name)
        # Dispatch case arm.
        if f"{wrapper_name})" not in text:
            missing_case.append(wrapper_name)
        # Delegating function. Case-arm may hyphenate; the fn uses
        # underscore. `rail-check` → `cmd_rail_check`.
        fn_name = "cmd_" + wrapper_name.replace("-", "_")
        if f"{fn_name}()" not in text:
            missing_fn.append(fn_name)
    assert not missing_case, (
        f"bin/morgoth is missing dispatch case arms for "
        f"self_modify.cli subcommands: {missing_case}. "
        f"Add `{missing_case[0]}) shift; cmd_{missing_case[0].replace('-', '_')} \"$@\";;` "
        f"to the case statement."
    )
    assert not missing_fn, (
        f"bin/morgoth is missing delegate functions: {missing_fn}"
    )


def test_bin_morgoth_help_lists_amend_and_recheck() -> None:
    """Regression: amend + recheck must appear in the usage. The
    operator relies on `morgoth help` to discover which subcommands
    exist — omission here is the failure mode chantier 4 fixes."""
    out = subprocess.run(
        [str(BIN_MORGOTH), "help"],
        capture_output=True, text=True, timeout=10,
    )
    assert out.returncode == 0, out.stderr
    assert "amend ID" in out.stdout, out.stdout
    assert "recheck ID" in out.stdout, out.stdout


@pytest.mark.parametrize("cmd", ["amend", "recheck"])
def test_bin_morgoth_amend_recheck_missing_id_prints_usage(cmd) -> None:
    """Called with no arguments, amend + recheck must exit non-zero
    and surface the usage line — the same convention the other
    proposal-side subcommands (approve, apply, shadow, provision)
    already follow."""
    out = subprocess.run(
        [str(BIN_MORGOTH), cmd],
        capture_output=True, text=True, timeout=10,
    )
    assert out.returncode != 0
    assert "usage" in (out.stderr + out.stdout).lower()
