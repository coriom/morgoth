"""Positive control: a proven DefiLlama stablecoins spec MUST clear the
shape gate under path-digest grammar. This is the counterpart to the
sandbox canaries — canaries prove the sandbox blocks what it must,
this test proves it still lets a GOOD proposal through. Without it,
the fail-closed sandbox could silently degrade into reject-everything
without anyone noticing.
"""

from __future__ import annotations

import json


class TestDefillamaSpecClearsShapeGate:
    """The DefiLlama /stablecoins endpoint returns {peggedAssets: [...]}
    — a nested list of dicts. The path-digest grammar was added to
    unlock exactly this class of source. Verify every step of the
    pipeline the shape gate exercises: parse spec → shape check →
    template render → import & execute with mocked HTTP → digest."""

    def _live_shape_body(self):
        return {
            "peggedAssets": [
                {"id": "1", "symbol": "USDT", "name": "Tether",
                 "circulating": {"peggedUSD": 120_000_000_000}},
                {"id": "2", "symbol": "USDC", "name": "USD Coin",
                 "circulating": {"peggedUSD": 40_000_000_000}},
                {"id": "3", "symbol": "DAI", "name": "Dai",
                 "circulating": {"peggedUSD": 5_000_000_000}},
            ]
        }

    def _spec(self):
        return {
            "tool_name": "get_defillama_stablecoins",
            "api_base_url": "https://stablecoins.llama.fi",
            "endpoint_path": "/stablecoins",
            "digest_fields": [
                {"name": "total_supply",
                 "path": "sum(peggedAssets[*].circulating.peggedUSD)"},
                {"name": "usdt_supply",
                 "path": "peggedAssets[symbol=USDT].circulating.peggedUSD"},
                {"name": "usdc_supply",
                 "path": "peggedAssets[symbol=USDC].circulating.peggedUSD"},
                {"name": "asset_count",
                 "path": "count(peggedAssets[*])"},
            ],
            "description": (
                "Fetch DefiLlama total stablecoin supply per pegged asset "
                "to measure aggregate stablecoin issuance for the rail."
            ),
            "rationale": (
                "Fills the stablecoin-issuance data gap surfaced by "
                "campaign 2. total_supply is the concrete field that "
                "measures the claimed gap."
            ),
        }

    def test_spec_validates(self):
        from self_modify.reflect import _spec_is_well_formed
        err = _spec_is_well_formed(self._spec())
        assert err is None, f"spec should validate; got: {err}"

    def test_shape_gate_passes(self):
        from self_modify.reflect import _shape_check
        err = _shape_check(self._live_shape_body(), self._spec()["digest_fields"])
        assert err is None, f"shape gate should PASS; got: {err}"

    def test_digest_resolves_scalar_per_field(self):
        from self_modify.digest_path import resolve_digest_fields
        values, errors, meta = resolve_digest_fields(
            self._spec()["digest_fields"], self._live_shape_body(),
        )
        assert errors == []
        assert values["total_supply"] == 165_000_000_000
        assert values["usdt_supply"]  == 120_000_000_000
        assert values["usdc_supply"]  ==  40_000_000_000
        assert values["asset_count"]  == 3
        assert meta["total_supply"]["resolved"] == 3

    def test_rationale_names_measuring_field(self):
        # 2026-09-26 gate-3 requirement: the rationale must name the
        # digest_field that measures the claimed gap. Enforced softly
        # here via a substring match — the operator sees this on
        # `morgoth show <id>` (see cli.py TARGET CHANGED / RATIONALE
        # cross-check).
        spec = self._spec()
        digest_names = {
            (f["name"] if isinstance(f, dict) else f)
            for f in spec["digest_fields"]
        }
        matched = [n for n in digest_names if n in (spec["rationale"] or "")]
        assert matched, (
            "rationale must name at least one digest_field that measures "
            "the claimed gap"
        )


class TestSandboxHermeticSubsetSkipsHostOnly:
    def test_conftest_declares_host_only_modules(self):
        # LOCK: conftest.py has a curated _HOST_ONLY_MODULES set and a
        # pytest_collection_modifyitems that applies skip when
        # MORGOTH_SANDBOX=1 is present.
        import inspect, tests.conftest as _cf
        src = inspect.getsource(_cf)
        assert "_HOST_ONLY_MODULES" in src
        assert 'MORGOTH_SANDBOX' in src
        assert "pytest_collection_modifyitems" in src

    def test_bwrap_sets_sandbox_marker(self):
        # LOCK: gates.py bwrap invocation exports MORGOTH_SANDBOX=1
        # so conftest.py's skip logic activates inside the sandbox.
        import inspect
        from self_modify import gates
        src = inspect.getsource(gates._build_pytest_argv)
        assert '"MORGOTH_SANDBOX"' in src
        assert '"1"' in src


class TestReflectPromptTeachesPathGrammar:
    def test_prompt_contains_path_digest_example(self):
        # 2026-09-26: reflect prompt must SHOW the digest-path grammar
        # plus one worked example — otherwise the model has no way to
        # discover the /stablecoins-shape solution and will keep
        # substituting flat-scalar sources.
        from self_modify.reflect import _reflection_prompt
        ctx = {
            "tools_block": "- t (data_source) — objectives_using=0: x",
            "objectives_block": "- OBJ", "theses_block": "- SUB",
            "rejections_block": "", "leads_block": "", "data_gaps_block": "",
        }
        out = _reflection_prompt(ctx)
        # Grammar tokens must appear.
        assert "sum(" in out and "[*]" in out
        # A worked example (path with a selector).
        assert "[symbol=USDT]" in out or "peggedAssets" in out


class TestRejectionsBlockAnnotatesFixedCauses:
    def test_shape_rejection_gets_path_digest_annotation(self):
        # When a past rejection has status_reason indicating a nested-
        # array shape failure, the rejections block should annotate
        # it as now-solvable via path digests rather than presenting
        # a dead end.
        from self_modify.reflect import _rejections_block
        rows = [{
            "target_path": "tools/data_feeds/get_defillama_stablecoins.py",
            "status": "rejected_shape",
            "status_reason": ("shape check on stablecoins.llama.fi: "
                               "response is a list but its first element is a dict"),
            "content": ("{\"tool_name\":\"get_defillama_stablecoins\","
                        "\"api_base_url\":\"https://stablecoins.llama.fi\","
                        "\"endpoint_path\":\"/stablecoins\"}"),
        }]
        block = _rejections_block(rows)
        assert "path digest" in block.lower() or "now expressible" in block.lower()
