"""Path grammar for digest_fields: parser + resolver + shape-gate wiring."""

from __future__ import annotations

import inspect

import pytest

from self_modify import digest_path as dp


class TestParser:
    def test_bare_word_is_top_level_key(self):
        p = dp.parse("symbol")
        assert p.aggregate is None
        assert p.segments == ["symbol"]

    def test_dotted_keys(self):
        p = dp.parse("data.timestamp")
        assert [s for s in p.segments if isinstance(s, str)] == ["data", "timestamp"]

    def test_list_index(self):
        p = dp.parse("items[0].name")
        assert p.segments[0] == "items"
        assert p.segments[1].kind == "index"
        assert p.segments[1].index == 0
        assert p.segments[2] == "name"

    def test_key_value_selector(self):
        p = dp.parse("peggedAssets[symbol=USDT].circulating.peggedUSD")
        sel = p.segments[1]
        assert sel.kind == "eq"
        assert sel.key == "symbol"
        assert sel.value == "USDT"

    def test_sum_aggregate(self):
        p = dp.parse("sum(peggedAssets[*].circulating.peggedUSD)")
        assert p.aggregate == "sum"
        assert p.has_star()

    def test_count_without_star_is_ok(self):
        # count(x) with no [*] → 1 if x resolves, 0 else
        p = dp.parse("count(top_level_key)")
        assert p.aggregate == "count"

    def test_sum_without_star_rejected(self):
        with pytest.raises(dp.DigestPathError):
            dp.parse("sum(top_level_key)")

    def test_never_evals_injection_shape(self):
        # Injection SAFETY: no matter what the string, the parser must
        # never execute code. Either raises a DigestPathError, OR
        # returns a data structure whose resolution against a body
        # produces at most a data lookup — never a call.
        # Every hostile string below must either raise, or return a
        # ParsedPath (no side effect); NEVER call eval/exec.
        for hostile in (
            "__import__('os').system('rm -rf /')",
            "eval('1+1')",
            "peggedAssets[symbol=' + os.system( + '].x",
            "]", "..", "[",
        ):
            try:
                parsed = dp.parse(hostile)
                # If it parsed, resolving against an empty body must
                # only raise a DigestPathError, never any other type
                # (which would indicate accidental execution).
                try:
                    dp.resolve(parsed, {})
                except dp.DigestPathError:
                    pass
            except dp.DigestPathError:
                pass

    def test_star_outside_aggregate_rejected(self):
        with pytest.raises(dp.DigestPathError):
            # [*] must be inside sum()/count()/min()/max()
            path = dp.parse("items[*].name")
            dp.resolve(path, {"items": [{"name": "x"}]})


class TestResolver:
    def test_top_level_scalar(self):
        assert dp.resolve(dp.parse("symbol"), {"symbol": "BTCUSDT"}) == "BTCUSDT"

    def test_dotted_dict(self):
        assert dp.resolve(
            dp.parse("a.b.c"), {"a": {"b": {"c": 42}}},
        ) == 42

    def test_missing_key_names_segment(self):
        with pytest.raises(dp.DigestPathError) as exc:
            dp.resolve(dp.parse("a.z"), {"a": {"b": 1}})
        assert exc.value.segment == "z"

    def test_index_out_of_range(self):
        with pytest.raises(dp.DigestPathError):
            dp.resolve(dp.parse("xs[5]"), {"xs": [1, 2]})

    def test_key_value_selector(self):
        body = {"peggedAssets": [
            {"symbol": "USDT", "circulating": {"peggedUSD": 100}},
            {"symbol": "USDC", "circulating": {"peggedUSD": 50}},
        ]}
        v = dp.resolve(
            dp.parse("peggedAssets[symbol=USDT].circulating.peggedUSD"),
            body,
        )
        assert v == 100

    def test_sum_star_over_list(self):
        body = {"peggedAssets": [
            {"circulating": {"peggedUSD": 100}},
            {"circulating": {"peggedUSD": 50}},
            {"circulating": {"peggedUSD": 25}},
        ]}
        assert dp.resolve(
            dp.parse("sum(peggedAssets[*].circulating.peggedUSD)"),
            body,
        ) == 175

    def test_count_star(self):
        body = {"peggedAssets": [{"x": 1}, {"x": 2}, {"y": 3}]}
        assert dp.resolve(dp.parse("count(peggedAssets[*])"), body) == 3

    def test_star_skips_missing(self):
        body = {"peggedAssets": [
            {"circulating": {"peggedUSD": 100}},
            {},  # missing — skipped
            {"circulating": {"peggedUSD": 25}},
        ]}
        assert dp.resolve(
            dp.parse("sum(peggedAssets[*].circulating.peggedUSD)"),
            body,
        ) == 125

    def test_min_max(self):
        body = {"xs": [{"v": 5}, {"v": 1}, {"v": 3}]}
        assert dp.resolve(dp.parse("min(xs[*].v)"), body) == 1
        assert dp.resolve(dp.parse("max(xs[*].v)"), body) == 5

    def test_scalar_landing_required(self):
        # A path that lands on a dict is an error — enforces the
        # "one scalar per digest field" contract.
        with pytest.raises(dp.DigestPathError):
            dp.resolve(dp.parse("a.b"), {"a": {"b": {"nested": 1}}})


class TestSilentZeroFailClosed:
    """2026-09-24: an aggregate over ZERO resolved elements now FAILS
    (was silently returning 0). Silent zero produced a plausible-
    looking number that the numeric-fidelity gate would then validate
    — masking upstream schema drift (renamed key skipping every
    element)."""

    def test_sum_over_all_skipped_fails(self):
        # Every element misses the tail key → 0 resolved → must raise.
        body = {"peggedAssets": [{"noKey": 1}, {"noKey": 2}]}
        with pytest.raises(dp.DigestPathError) as exc:
            dp.resolve(
                dp.parse("sum(peggedAssets[*].circulating.peggedUSD)"),
                body,
            )
        assert "ZERO resolved" in str(exc.value)
        # Silent zero MUST NOT be returned.
        assert "0" not in str(exc.value).split("ZERO")[0][-4:]

    def test_min_over_empty_fails(self):
        body = {"xs": []}
        with pytest.raises(dp.DigestPathError):
            dp.resolve(dp.parse("min(xs[*].v)"), body)

    def test_max_over_all_skipped_fails(self):
        body = {"xs": [{"noKey": 1}]}
        with pytest.raises(dp.DigestPathError):
            dp.resolve(dp.parse("max(xs[*].v)"), body)

    def test_count_over_all_skipped_fails(self):
        # count with tail: all elements missing the tail → fail.
        body = {"xs": [{"noKey": 1}, {"noKey": 2}]}
        with pytest.raises(dp.DigestPathError):
            dp.resolve(dp.parse("count(xs[*].v)"), body)

    def test_resolved_and_skipped_metadata_exposed(self):
        # Partial skip: 2 of 3 elements resolve; metadata reports 2, 1.
        body = {"xs": [{"v": 1}, {"noKey": 2}, {"v": 3}]}
        v, err, meta = dp.resolve_digest_fields(
            [{"name": "total", "path": "sum(xs[*].v)"}], body,
        )
        assert err == []
        assert v["total"] == 4
        assert meta["total"] == {"resolved": 2, "skipped": 1}

    def test_shape_gate_reports_silent_zero_as_error(self):
        # End-to-end: shape gate returns a reason mentioning ZERO.
        from self_modify.reflect import _shape_check
        body = {"peggedAssets": [{"noKey": 1}, {"noKey": 2}]}
        err = _shape_check(body, [
            {"name": "total", "path": "sum(peggedAssets[*].circulating.peggedUSD)"},
        ])
        assert err is not None
        assert "ZERO" in err or "zero" in err


class TestDigestFieldsCap:
    def test_cap_enforced(self):
        many = [{"name": f"f{i}", "path": "top"} for i in range(dp.MAX_DIGEST_FIELDS + 1)]
        _v, err, _m = dp.resolve_digest_fields(many, {"top": 1})
        assert err and "cap exceeded" in err[0][1]


class TestBackCompatStringEntry:
    def test_string_entry_still_supported(self):
        v, err, _m = dp.resolve_digest_fields(
            ["symbol", "price"],
            {"symbol": "BTC", "price": 100},
        )
        assert err == []
        assert v == {"symbol": "BTC", "price": 100}


class TestDictEntry:
    def test_defillama_stablecoins_shape(self):
        # Live-shape fixture: DefiLlama /stablecoins response.
        body = {"peggedAssets": [
            {"id": "1", "symbol": "USDT", "name": "Tether",
             "circulating": {"peggedUSD": 120_000_000_000}},
            {"id": "2", "symbol": "USDC", "name": "USD Coin",
             "circulating": {"peggedUSD": 40_000_000_000}},
            {"id": "3", "symbol": "DAI", "name": "Dai",
             "circulating": {"peggedUSD": 5_000_000_000}},
        ]}
        spec_fields = [
            {"name": "total_supply", "path": "sum(peggedAssets[*].circulating.peggedUSD)"},
            {"name": "usdt_supply",  "path": "peggedAssets[symbol=USDT].circulating.peggedUSD"},
            {"name": "usdc_supply",  "path": "peggedAssets[symbol=USDC].circulating.peggedUSD"},
            {"name": "asset_count",  "path": "count(peggedAssets[*])"},
        ]
        v, err, meta = dp.resolve_digest_fields(spec_fields, body)
        assert err == []
        assert v["total_supply"] == 165_000_000_000
        assert v["usdt_supply"]  == 120_000_000_000
        assert v["usdc_supply"]  ==  40_000_000_000
        assert v["asset_count"]  == 3
        # Aggregate metadata surfaces resolved/skipped counts.
        assert meta["total_supply"]["resolved"] == 3
        assert meta["total_supply"]["skipped"] == 0
        assert meta["asset_count"]["resolved"] == 3


class TestShapeGateWiring:
    def test_shape_check_accepts_path_entries(self):
        from self_modify.reflect import _shape_check
        body = {"peggedAssets": [
            {"symbol": "USDT", "circulating": {"peggedUSD": 100}},
        ]}
        err = _shape_check(body, [
            {"name": "usdt", "path": "peggedAssets[symbol=USDT].circulating.peggedUSD"},
        ])
        assert err is None

    def test_shape_check_names_failing_segment(self):
        from self_modify.reflect import _shape_check
        body = {"peggedAssets": [{"symbol": "USDT"}]}  # missing circulating
        err = _shape_check(body, [
            {"name": "usdt", "path": "peggedAssets[symbol=USDT].circulating.peggedUSD"},
        ])
        assert err is not None
        assert "circulating" in err


class TestCorrectivePromptLocksTarget:
    def test_shape_reject_adds_target_lock_line(self):
        from self_modify.reflect import _corrective_prompt
        out = _corrective_prompt("BASE", {"tool_name": "x"}, "shape err",
                                  reject_status="rejected_shape")
        assert "TARGET LOCK" in out
        assert "SAME api_base_url" in out
        assert "abstain" in out.lower() or "NONE" in out

    def test_other_rejects_do_not_lock_target(self):
        from self_modify.reflect import _corrective_prompt
        out = _corrective_prompt("BASE", {"tool_name": "x"}, "shape err",
                                  reject_status="rejected_smoke")
        assert "TARGET LOCK" not in out


class TestTargetChangedNoteInShow:
    def test_helper_flags_host_and_tool_diff(self):
        from self_modify.cli import _target_change_note
        orig = {"content": '{"api_base_url":"https://api.llama.fi","tool_name":"get_defillama_stablecoins"}'}
        retry = {"content": '{"api_base_url":"https://api.gemini.com","tool_name":"get_gemini_usdt_peg"}'}
        note = _target_change_note(orig, retry)
        assert "host" in note and "llama.fi" in note and "gemini" in note
        assert "tool_name" in note

    def test_helper_silent_when_target_unchanged(self):
        from self_modify.cli import _target_change_note
        orig = {"content": '{"api_base_url":"https://api.llama.fi","tool_name":"get_x"}'}
        retry = {"content": '{"api_base_url":"https://api.llama.fi","tool_name":"get_x"}'}
        assert _target_change_note(orig, retry) == ""

    def test_cmd_show_uses_target_change_note(self):
        from self_modify import cli as _cli
        src = inspect.getsource(_cli._cmd_show)
        assert "_target_change_note" in src
        assert "TARGET CHANGED" in src
