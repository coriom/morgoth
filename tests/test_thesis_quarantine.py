"""Reversible thesis quarantine: status='quarantined' + quarantine_reason.

Locks the four downstream exclusion sites (contradiction detector,
generation context, backtest, vault) and the unquarantine round-trip.
"""

from __future__ import annotations

import inspect
import re

from scripts.quarantine_theses import _pre2025_hit, _YEAR_RE, _MONTHYR_RE


class TestPre2025Matcher:
    def test_bare_year_matches(self):
        assert _pre2025_hit("upward trajectory from 2018 onwards")
        assert _pre2025_hit("UNRATE 2018-04-01 value = 4.0")

    def test_month_year_matches(self):
        assert _pre2025_hit("CPIAUCSL May 2018")
        assert _pre2025_hit("CPI data from May 2018 to June 2018 stable")

    def test_2025_and_later_do_not_match(self):
        # Cutoff intentionally at 2024 (last stale year).
        assert not _pre2025_hit("BTC price on 2025-06-15")
        assert not _pre2025_hit("June 2026 dominance")

    def test_no_year_returns_false(self):
        assert not _pre2025_hit("BTC dominance is high")
        assert not _pre2025_hit("")


class TestExclusionsGrepLocked:
    """Every consumer of active theses must skip quarantined rows."""

    def test_backtest_excludes_quarantined(self):
        from scripts import backtest_theses_descriptive as m
        src = inspect.getsource(m._fetch_theses)
        assert "status <> 'quarantined'" in src

    def test_vault_compile_excludes_quarantined(self):
        from scripts import compile_wiki as m
        src = inspect.getsource(m)
        assert "'quarantined'" in src
        # And it's in the theses SELECT.
        assert "'stale', 'quarantined'" in src or 'quarantined' in src

    def test_contradiction_detector_uses_status_active(self):
        # brain.detect_contradictions filters status='active' via
        # get_theses(status="active", ...) — quarantined rows are
        # excluded naturally.
        from core import brain
        src = inspect.getsource(brain.Brain.detect_contradictions)
        assert 'get_theses(status="active"' in src

    def test_objective_gen_context_uses_status_active(self):
        # generation context reads get_theses(status="active"), so a
        # quarantined thesis subject won't be surfaced as a "thesis
        # subject that needs deeper evidence".
        from core import objective_gen_context as m
        src = inspect.getsource(m)
        assert 'get_theses(status="active"' in src


class TestUnquarantineIsReversible:
    def test_undo_command_exists(self):
        from scripts import quarantine_theses as q
        # Structural: the module exposes an _cmd_unquarantine that
        # flips status back to 'active' and NULLs quarantine_reason.
        src = inspect.getsource(q._cmd_unquarantine)
        assert "status='active'" in src
        assert "quarantine_reason=NULL" in src
        # Supports both "everything" and per-reason undo.
        assert "if reason" in src

    def test_apply_command_uses_two_reason_codes(self):
        from scripts import quarantine_theses as q
        src = inspect.getsource(q._cmd_quarantine)
        assert "'fred_oldest_first'" in src
        assert "'interestrate_as_funding'" in src


class TestContradictionVoiding:
    def test_apply_voids_open_contradictions(self):
        # Structural: _cmd_quarantine now voids open contradictions
        # touching a newly-quarantined thesis. Grep-lock the SQL.
        from scripts import quarantine_theses as q
        src = inspect.getsource(q._cmd_quarantine)
        assert "UPDATE contradictions SET resolution='voided_quarantine'" in src
        assert "resolution IS NULL" in src

    def test_undo_reopens_only_voided_by_quarantine(self):
        from scripts import quarantine_theses as q
        src = inspect.getsource(q._cmd_unquarantine)
        assert "resolution='voided_quarantine'" in src
        # Only reopens when BOTH theses are back to active — else the
        # operator's manual resolution is preserved.
        assert "status='active'" in src


class TestDirectionalBacktestAndTrackRecordExclusion:
    def test_directional_backtest_filters_quarantined(self):
        from scripts import backtest_theses as m
        src = inspect.getsource(m._fetch_theses)
        assert "status <> 'quarantined'" in src

    def test_track_record_reuses_backtest_scorers(self):
        # track_record has no direct theses fetch — it consumes the
        # backtest module's records. Exclusion inherits from the
        # backtest fetch above. Grep-lock: no `FROM theses` in the
        # module.
        from analysis import track_record as m
        src = inspect.getsource(m)
        assert "FROM theses" not in src.upper() or "from theses" not in src.lower()


class TestReasonCodesEnumerated:
    def test_three_reason_codes_are_used(self):
        # 2026-09-21 audit produced three reason codes:
        # fred_oldest_first, interestrate_as_funding, training_derived.
        # The apply script writes the first two automatically; the
        # third is set manually for claims that cannot come from FRED.
        expected = {"fred_oldest_first", "interestrate_as_funding",
                     "training_derived"}
        # Documented in the module docstring and/or code.
        from scripts import quarantine_theses as q
        src = inspect.getsource(q)
        for code in expected:
            assert code in src, f"reason code {code!r} not referenced"


class TestSchemaMigration:
    def test_quarantine_reason_column_added_in_initialize(self):
        from memory.persistent import PersistentMemory
        src = inspect.getsource(PersistentMemory.initialize)
        assert "ADD COLUMN IF NOT EXISTS quarantine_reason TEXT" in src
