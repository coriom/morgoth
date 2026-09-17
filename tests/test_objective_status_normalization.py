"""Boundary enforcement of the objectives.status enum.

Documented lifecycle: pending → in_progress → done, plus the system-set
terminal `stale_timeout` written by the freshness sweep. Any other value
(free-text from the 8B: "completed", "ongoing", "progressing", "active")
must be coerced by the synonym table or rejected with a usable error.
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

from memory.persistent import PersistentMemory


class TestNormalizeStatus:
    @pytest.mark.parametrize("val", [
        "pending", "in_progress", "done", "stale_timeout",
        "PENDING", "In_Progress", "DONE", "Stale_Timeout",
        "  pending  ",  # trimmed
    ])
    def test_canonical_values_pass_through(self, val):
        # After lowercase+strip normalisation.
        got = PersistentMemory.normalize_objective_status(val)
        assert got == val.strip().lower()
        assert got in PersistentMemory.CANONICAL_STATUSES

    @pytest.mark.parametrize("val, expected", [
        ("completed", "done"),
        ("Complete", "done"),
        ("FINISHED", "done"),
        ("active", "in_progress"),
        ("ONGOING", "in_progress"),
        ("progressing", "in_progress"),
        ("progress", "in_progress"),
        ("working", "in_progress"),
    ])
    def test_known_synonyms_map(self, val, expected):
        assert PersistentMemory.normalize_objective_status(val) == expected

    @pytest.mark.parametrize("val", [
        "unknown", "wip", "queued", "blocked", "cancelled", "abandoned",
    ])
    def test_unmappable_values_raise_valueerror(self, val):
        with pytest.raises(ValueError) as ei:
            PersistentMemory.normalize_objective_status(val)
        # Error message must expose the allowed values so a retry is possible.
        msg = str(ei.value)
        assert "pending" in msg
        assert "in_progress" in msg
        assert "done" in msg

    def test_empty_string_rejected(self):
        with pytest.raises(ValueError):
            PersistentMemory.normalize_objective_status("")
        with pytest.raises(ValueError):
            PersistentMemory.normalize_objective_status("   ")

    def test_non_string_rejected(self):
        with pytest.raises(ValueError):
            PersistentMemory.normalize_objective_status(None)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            PersistentMemory.normalize_objective_status(42)  # type: ignore[arg-type]

    def test_canonical_set_matches_documented_lifecycle(self):
        # Grep-lock: the canonical set is intentionally exactly these four.
        # Adding a new terminal status requires touching this test — that's
        # a wanted friction point.
        assert PersistentMemory.CANONICAL_STATUSES == frozenset({
            "pending", "in_progress", "done", "stale_timeout",
        })


class TestUpdateObjectiveWiring:
    """Structural fences on the boundary enforcement itself."""

    def test_update_objective_normalises_before_write(self):
        import inspect
        src = inspect.getsource(PersistentMemory.update_objective)
        assert "normalize_objective_status" in src
        # The normalisation must run BEFORE the SQL UPDATE build.
        norm_pos = src.find("normalize_objective_status")
        update_pos = src.find("UPDATE objectives")
        # UPDATE literal may be assembled via an f-string later; we only
        # need to prove normalisation isn't AFTER the actual DB call.
        assert 0 < norm_pos
        # And it must appear before set_clauses building.
        clauses_pos = src.find("set_clauses.append")
        assert norm_pos < clauses_pos


class TestToolFailurePath:
    """The UpdateObjectiveTool must return a usable error to the model
    (not raise) so the model can retry on the next turn."""

    def test_tool_returns_failure_with_allowed_values_on_invalid(self):
        import asyncio
        from tools.objectives_tool import UpdateObjectiveTool

        pm = MagicMock()
        pm.update_objective = AsyncMock(
            side_effect=ValueError(
                "unknown objective status 'ongoing'; "
                "allowed values: ['done', 'in_progress', 'pending', 'stale_timeout']"
            )
        )
        tool = UpdateObjectiveTool(pm)
        res = asyncio.run(tool.execute(objective_id="00000000-0000-0000-0000-000000000000",
                                        status="ongoing"))
        assert res["success"] is False
        err = res["error"]
        assert "invalid status" in err
        assert "in_progress" in err
        assert "done" in err

    def test_tool_passes_canonical_status_through(self):
        import asyncio
        from tools.objectives_tool import UpdateObjectiveTool

        pm = MagicMock()
        pm.update_objective = AsyncMock(return_value={"objective_id": "x", "status": "done"})
        tool = UpdateObjectiveTool(pm)
        res = asyncio.run(tool.execute(objective_id="x", status="done",
                                         evidence_summary="wrapped up"))
        assert res["success"] is True
        pm.update_objective.assert_awaited_once()
        kw = pm.update_objective.await_args.kwargs
        assert kw["status"] == "done"


class TestSystemSetStatusStillWorks:
    """The freshness sweep writes 'stale_timeout' directly via UPDATE;
    the outage guard writes 'pending' via requeue_objective_after_outage.
    Both must survive the boundary enforcement unchanged."""

    def test_stale_timeout_is_canonical(self):
        assert "stale_timeout" in PersistentMemory.CANONICAL_STATUSES
        assert (PersistentMemory.normalize_objective_status("stale_timeout")
                == "stale_timeout")

    def test_pending_is_canonical(self):
        assert "pending" in PersistentMemory.CANONICAL_STATUSES
        assert PersistentMemory.normalize_objective_status("pending") == "pending"


class TestRepairIdempotent:
    """A synonym normalised twice yields the same canonical value —
    running the repair script twice must be a no-op the second time."""

    @pytest.mark.parametrize("val", [
        "completed", "active", "ongoing", "progressing", "progress",
    ])
    def test_normalisation_is_idempotent(self, val):
        once = PersistentMemory.normalize_objective_status(val)
        twice = PersistentMemory.normalize_objective_status(once)
        assert once == twice
        assert twice in PersistentMemory.CANONICAL_STATUSES
