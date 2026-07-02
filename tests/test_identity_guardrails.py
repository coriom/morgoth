"""Guardrail assertions — the mechanical wall around Morgoth's identity.

Any future weakening of these constraints must fail the suite mechanically
so it can't slip past review.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_identity_md_exists_with_mission_and_constraints() -> None:
    """IDENTITY.md must exist at repo root and contain the sections."""
    identity = REPO_ROOT / "IDENTITY.md"
    assert identity.exists(), "IDENTITY.md must exist at repo root"
    body = identity.read_text(encoding="utf-8")
    assert "# IDENTITY" in body
    assert "## Mission" in body
    assert "## Hard constraints" in body
    # The three load-bearing self-modify invariants must be named explicitly.
    assert "can_self_modify" in body
    assert "MAX_CYCLES" in body
    assert "Default deny" in body or "default deny" in body


def test_classify_proposal_default_denies_unknown_paths() -> None:
    """Any path not matching an explicit green/orange rule is red."""
    from self_modify.zones import classify_proposal

    assert classify_proposal("some/random/path.py", "new_file") == "red"
    assert classify_proposal("random.txt", "edit") == "red"
    assert classify_proposal("", "new_file") == "red"
    assert classify_proposal("/absolute/path", "new_file") == "red"


def test_classify_proposal_red_zone_paths() -> None:
    """The paths listed as RED in the docstring must classify red."""
    from self_modify.zones import classify_proposal

    assert classify_proposal("core/brain.py", "edit") == "red"
    assert classify_proposal("core/brain.py", "new_file") == "red"
    assert classify_proposal("self_modify/zones.py", "edit") == "red"
    assert classify_proposal("self_modify/gates.py", "new_file") == "red"
    assert classify_proposal("tests/test_zones.py", "edit") == "red"
    assert classify_proposal("main.py", "edit") == "red"
    assert classify_proposal("api/server.py", "edit") == "red"
    assert classify_proposal("memory/persistent.py", "edit") == "red"
    assert classify_proposal("scripts/backup_morgoth.sh", "edit") == "red"
    assert classify_proposal("IDENTITY.md", "edit") == "red"
    assert classify_proposal(".env", "edit") == "red"
    assert classify_proposal("requirements.txt", "edit") == "red"


def test_classify_proposal_green_zone_is_new_files_only() -> None:
    """A NEW file under tools/data_feeds/ is green; an EDIT there is red."""
    from self_modify.zones import classify_proposal

    assert classify_proposal("tools/data_feeds/new_tool.py", "new_file") == "green"
    # Nested new file under the green prefix is still green.
    assert classify_proposal("tools/data_feeds/subdir/plugin.py", "new_file") == "green"
    # An edit inside the green directory is NOT green.
    assert classify_proposal("tools/data_feeds/crypto.py", "edit") == "red"
    # The green prefix itself (no filename after it) is not green.
    assert classify_proposal("tools/data_feeds/", "new_file") == "red"
    # A new file outside the green prefix is red (default deny).
    assert classify_proposal("tools/other/new_tool.py", "new_file") == "red"


def test_classify_proposal_rejects_path_traversal() -> None:
    """.. path segments must not be able to escape the green zone.

    A bare `.` segment normalizes to a no-op and is not real traversal;
    only `..` moves up the tree. Only `..` needs to be refused.
    """
    from self_modify.zones import classify_proposal

    assert classify_proposal("tools/data_feeds/../core/evil.py", "new_file") == "red"
    assert classify_proposal("../etc/passwd", "new_file") == "red"


def test_classify_proposal_rejects_unknown_change_type() -> None:
    from self_modify.zones import classify_proposal

    # type: ignore intentionally exercised at runtime.
    assert classify_proposal("tools/data_feeds/x.py", "delete") == "red"  # type: ignore[arg-type]
    assert classify_proposal("tools/data_feeds/x.py", "") == "red"  # type: ignore[arg-type]


def test_max_cycles_forced_completion_still_referenced_in_brain() -> None:
    """MAX_CYCLES / forced completion must still exist in core/brain.py.

    Grep-level check: any refactor that removes the forced-completion
    backstop MUST fail this test so it can't happen quietly.
    """
    brain = (REPO_ROOT / "core" / "brain.py").read_text(encoding="utf-8")
    assert "MAX_CYCLES" in brain, "MAX_CYCLES constant/reference removed from brain.py"


def test_can_self_modify_is_false_in_permissions() -> None:
    """can_self_modify must remain false in the runtime permission file."""
    import json

    perms = json.loads(
        (REPO_ROOT / "MORGOTH_PERMS.json").read_text(encoding="utf-8")
    )
    flags = perms.get("permissions") or {}
    assert flags.get("can_self_modify") is False, (
        "can_self_modify must stay false in step 1 of the self-modify frontier "
        "(MORGOTH_PERMS.json → permissions.can_self_modify)"
    )
