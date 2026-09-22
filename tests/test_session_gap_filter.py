"""Session-gap report filter: a gap counts when it ENDS in the window,
not just when it STARTED in it. Reproduces the "1h window shows 0 gaps
right after an 11h gap ended" complaint."""

from __future__ import annotations

import inspect


class TestFilterFix:
    def test_report_filters_gaps_by_ended_at(self):
        # Structural: the SESSION GAPS aggregation now filters on
        # ended_at (or an overlap with the window), not started_at.
        from analysis import session_report as m
        src = inspect.getsource(m.collect)
        assert "FROM session_gaps WHERE ended_at >= $1" in src
        # Old filter must be gone.
        assert "FROM session_gaps WHERE started_at >= $1" not in src


class TestSchemaMatchesTimestamptz:
    """B — the columns ARE timestamp with time zone; observed
    "mixed offset" is a display artefact of the client session TZ,
    not a storage defect. This test asserts the schema so any
    accidental TEXT-column migration would fail loudly."""

    def test_startup_declares_timestamptz(self):
        from memory.persistent import PersistentMemory
        src = inspect.getsource(PersistentMemory.initialize)
        # Both columns declared as TIMESTAMPTZ.
        assert "started_at TIMESTAMPTZ" in src
        assert "ended_at TIMESTAMPTZ" in src
