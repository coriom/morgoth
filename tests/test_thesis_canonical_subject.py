"""Thesis canonical_subject column: populated at write time via the
existing contradiction-grouping helper, backfill idempotent, genuinely
different subjects stay distinct."""

from __future__ import annotations

import inspect

from core.contradictions import canonicalize_subject_for_grouping as _canon
from memory.persistent import PersistentMemory


class TestCanonicalisationCollapses:
    def test_bitcoin_dominance_variants_collapse(self):
        # The three real variants from the corpus should all yield the
        # same canonical form.
        assert _canon("BTC dominance") == "btc dominance"
        assert _canon("Bitcoin dominance") == "bitcoin dominance"
        assert _canon("bitcoin dominance") == "bitcoin dominance"
        # Global-prefix variant folds too.
        assert _canon("Global bitcoin dominance") == "bitcoin dominance"

    def test_case_and_whitespace_collapse(self):
        assert _canon("  Market   Cap  ") == "market cap"
        assert _canon("MARKET CAP") == "market cap"


class TestDistinctness:
    def test_market_cap_vs_trading_volume_stay_distinct(self):
        # Different root tokens after prefix strip → distinct canonicals.
        assert _canon("Global crypto market cap") != _canon("Global crypto trading volume")

    def test_btc_vs_eth_asset_qualifier_preserved(self):
        # "BTC" / "Bitcoin" / "ETH" / "Ethereum" are NOT stopwords —
        # asset qualifier survives canonicalisation.
        assert "btc" in _canon("BTC 24h price change")
        assert "ethereum" in _canon("Ethereum gas price")
        assert _canon("BTC 24h price change") != _canon("ETH 24h price change")


class TestIdempotent:
    def test_canonicalising_a_canonical_form_is_a_no_op(self):
        for s in ["btc dominance", "market cap 24h change", "fear & greed index"]:
            assert _canon(s) == s
        # Composition: canon(canon(x)) == canon(x).
        for s in ["Global crypto Market Cap", "BTC dominance vs price"]:
            once = _canon(s)
            twice = _canon(once)
            assert once == twice


class TestAddThesisPopulatesCanonical:
    """Grep-lock: add_thesis persists canonical_subject alongside subject."""

    def test_add_thesis_source_computes_canonical(self):
        src = inspect.getsource(PersistentMemory.add_thesis)
        assert "canonicalize_subject_for_grouping" in src
        assert "canonical_subject" in src

    def test_add_thesis_insert_includes_canonical_column(self):
        src = inspect.getsource(PersistentMemory.add_thesis)
        assert "INSERT INTO theses" in src
        # Column list in the INSERT statement mentions canonical_subject.
        assert "canonical_subject" in src
        # And it's bound to $7 (7th param) — canonical is the last VALUE.
        assert "$7" in src


class TestSchemaMigrationDeclared:
    """The ALTER TABLE ADD COLUMN IF NOT EXISTS is idempotent and
    lives in initialize() so restarts are safe."""

    def test_initialize_declares_canonical_column(self):
        src = inspect.getsource(PersistentMemory.initialize)
        assert "canonical_subject" in src
        # Idempotent DDL — safe to re-run on every startup.
        assert "ADD COLUMN IF NOT EXISTS canonical_subject" in src
        # Index exists.
        assert "theses_canonical_subject_idx" in src
