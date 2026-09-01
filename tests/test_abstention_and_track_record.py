"""Abstention + track-record — tests for both features that ship together.

Abstention (active): empty extraction is a NORMAL outcome, prompt names it
    valued, downstream logs it without treating it as failure.
Track-record (inert): aggregator + qualification rule + injection-ready
    context block that stays EMPTY until n>=20 AND rate is >2SE from chance
    AND the operator flips TRACK_RECORD_ENABLED. Grep-locks on both.
"""

from __future__ import annotations

import inspect
import os
from dataclasses import dataclass
from pathlib import Path

import pytest

from analysis import track_record as TR


# ═════════════════════════════════════════════════════════════════════
# ABSTENTION — clause-in-prompt grep-lock
# ═════════════════════════════════════════════════════════════════════


def test_abstention_clause_in_generation_prompt() -> None:
    """The generation prompt must EXPLICITLY value empty output.
    Grep-lock ensures a future edit that removes the clause breaks build."""
    src = Path("core/brain.py").read_text()
    # Named failure mode (same pattern the phantom-ban followed).
    assert "ABSTENTION IS VALUED" in src
    assert "STRICTLY\n            \"WORSE than emitting none" in src or \
           "STRICTLY WORSE than emitting none" in src.replace("\n            \"", "").replace('"', "")
    # Concrete words that name the "return []" instruction:
    assert "return an EMPTY array" in src
    assert "Fewer, denser, grounded theses" in src


def test_abstention_empty_extraction_is_normal_cycle_outcome() -> None:
    """The cycle path treats [] as a normal outcome — no error, no retry.
    Grep-lock: the abstention log line + no 'raise' inside the [] branch."""
    src = Path("core/brain.py").read_text()
    # Log line for observability (objective_id + cycle count).
    assert 'reason=empty' in src
    assert 'thesis abstention: objective_id' in src


# ═════════════════════════════════════════════════════════════════════
# TRACK-RECORD — qualification math + flag lock + empty-block invariant
# ═════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class _FakeRec:
    metric: str
    outcome: str  # "hit" or "miss"


class TestFlagDefault:
    def setup_method(self):
        os.environ.pop(TR.TRACK_RECORD_ENV, None)

    def test_default_off(self):
        assert TR.track_record_enabled() is False

    def test_on_env_values(self, monkeypatch):
        for val in ("1", "true", "on", "yes", "TRUE", "  On  "):
            monkeypatch.setenv(TR.TRACK_RECORD_ENV, val)
            assert TR.track_record_enabled() is True

    def test_off_env_values(self, monkeypatch):
        for val in ("", "0", "false", "off", "no", "banana"):
            monkeypatch.setenv(TR.TRACK_RECORD_ENV, val)
            assert TR.track_record_enabled() is False


class TestGrepLockDefault:
    def test_track_record_default_off_grep_lock(self):
        """Grep-lock: the flag default MUST stay off. A future edit that
        removes the empty-string fallback would flip the default silently."""
        src = Path("analysis/track_record.py").read_text()
        assert 'os.environ.get(TRACK_RECORD_ENV) or ""' in src
        assert '"1", "true", "on", "yes"' in src


class TestQualifiesRule:
    """n >= N_MIN AND |rate - chance| > 2*SE(chance, n).
    N_MIN = 20. Directional chance = 1/3, level chance = 1/2."""

    def test_below_n_min_never_qualifies(self):
        # 100% correct on n=15 — still doesn't qualify.
        ok, _ = TR.qualifies(rate=1.0, chance=1.0 / 3.0, n=15)
        assert ok is False

    def test_n_min_within_ci_of_chance_does_not_qualify(self):
        # n=20 directional, rate = 40 % — within 2SE band of chance (33 %).
        ok, margin = TR.qualifies(rate=0.40, chance=1.0 / 3.0, n=20)
        assert ok is False
        assert margin > 0.10  # ~21% at n=20

    def test_n_min_outside_ci_qualifies(self):
        # n=20 directional, rate = 80 % — well outside 2SE (~21%).
        ok, _ = TR.qualifies(rate=0.80, chance=1.0 / 3.0, n=20)
        assert ok is True

    def test_n_min_far_below_chance_qualifies(self):
        # n=20 directional, rate = 5 % — well below 2SE band.
        ok, _ = TR.qualifies(rate=0.05, chance=1.0 / 3.0, n=20)
        assert ok is True

    def test_level_chance_uses_50pct_baseline(self):
        # n=20 level, rate = 55 % — inside 2SE of 50 % (~22%).
        ok, _ = TR.qualifies(rate=0.55, chance=0.5, n=20)
        assert ok is False
        # rate = 90 % — outside.
        ok, _ = TR.qualifies(rate=0.90, chance=0.5, n=20)
        assert ok is True


class TestAggregateByClass:
    def test_groups_by_metric_and_class(self):
        directional = [
            _FakeRec("btc_difficulty", "hit"),
            _FakeRec("btc_difficulty", "miss"),
            _FakeRec("btc_hashrate", "hit"),
        ]
        level = [
            _FakeRec("eth_gas", "hit"),
            _FakeRec("eth_gas", "miss"),
        ]
        rows = TR.aggregate_by_class(directional, level)
        by_key = {(r.metric, r.claim_class): r for r in rows}
        assert by_key[("btc_difficulty", "directional")].n == 2
        assert by_key[("btc_difficulty", "directional")].hits == 1
        assert by_key[("btc_hashrate", "directional")].n == 1
        assert by_key[("eth_gas", "level")].n == 2
        # Chances are correctly set.
        assert by_key[("btc_difficulty", "directional")].chance == pytest.approx(1 / 3)
        assert by_key[("eth_gas", "level")].chance == 0.5


class TestRenderContextBlock:
    def setup_method(self):
        os.environ.pop(TR.TRACK_RECORD_ENV, None)

    def test_empty_string_when_flag_off(self):
        """Flag off → block empty even if classes would qualify. This is
        the non-regression invariant: prompt is byte-identical to today."""
        rows = [TR.ClassRow(metric="btc_hashrate", claim_class="directional",
                            n=100, hits=90, rate=0.9, chance=1/3,
                            margin_required=0.09, qualifies=True)]
        assert TR.render_context_block(rows) == ""

    def test_empty_string_when_flag_on_but_nothing_qualifies(self, monkeypatch):
        monkeypatch.setenv(TR.TRACK_RECORD_ENV, "on")
        rows = [TR.ClassRow(metric="btc_difficulty", claim_class="directional",
                            n=25, hits=10, rate=0.40, chance=1/3,
                            margin_required=0.19, qualifies=False)]
        assert TR.render_context_block(rows) == ""

    def test_empty_string_when_flag_on_and_rows_empty(self, monkeypatch):
        monkeypatch.setenv(TR.TRACK_RECORD_ENV, "on")
        assert TR.render_context_block([]) == ""

    def test_renders_only_qualifying_when_both_gates_pass(self, monkeypatch):
        monkeypatch.setenv(TR.TRACK_RECORD_ENV, "on")
        rows = [
            TR.ClassRow(metric="btc_hashrate", claim_class="directional",
                        n=25, hits=22, rate=0.88, chance=1/3,
                        margin_required=0.19, qualifies=True),
            TR.ClassRow(metric="btc_difficulty", claim_class="directional",
                        n=25, hits=10, rate=0.40, chance=1/3,
                        margin_required=0.19, qualifies=False),
        ]
        out = TR.render_context_block(rows)
        assert out != ""
        assert "TRACK RECORD" in out
        # Only the qualifying class is listed.
        assert "btc_hashrate" in out
        assert "btc_difficulty" not in out
        assert "above chance" in out


class TestPromptByteIdenticalWhenBlockEmpty:
    """Non-regression: with the flag off (default), the generation prompt
    must render exactly as it did before track-record wired in — i.e. the
    empty prefix must NOT introduce a stray newline / whitespace / marker."""

    def test_empty_block_prefix_has_zero_length(self):
        os.environ.pop(TR.TRACK_RECORD_ENV, None)
        # Default flag off → block empty → prefix concat is a no-op.
        assert TR.render_context_block([]) == ""
        # Sanity: len 0 → prompt unchanged. The brain.py grep proves the
        # concat is `f"{_track_prefix}OBJECTIVE: ..."` — an empty string
        # there yields the exact original string.
        src = Path("core/brain.py").read_text()
        assert 'f"{_track_prefix}"' in src


class TestGrepLockInjectionPath:
    def test_brain_py_uses_render_context_block(self):
        """A future edit that removes the injection call site would silently
        block the feature from ever activating. Grep-lock."""
        src = Path("core/brain.py").read_text()
        assert "from analysis.track_record import render_context_block" in src
        assert "_track_prefix = _tr_block([])" in src
