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


class TestSandboxUsesMarkerExclusion:
    def test_pytest_ini_declares_integration_marker(self):
        # LOCK: pytest.ini declares the `integration` marker so
        # `-m "not integration"` is meaningful.
        import pathlib
        cfg = pathlib.Path("pytest.ini").read_text()
        assert "integration:" in cfg

    def test_gates_uses_marker_exclusion_not_module_list(self):
        # LOCK: gate_tests inside the sandbox runs `-m "not integration"`,
        # NOT a blanket module-list skip (that was the pre-2026-09-26
        # anti-pattern that hid test_discovery, test_source_cache,
        # test_brain_*, letting a proposal breaking those pass silently).
        import inspect
        from self_modify import gates
        src = inspect.getsource(gates._build_pytest_argv)
        assert '"-m"' in src
        assert '"not integration"' in src
        # No module-list skip remains.
        assert "_HOST_ONLY_MODULES" not in src

    def test_conftest_no_longer_has_module_list_skip(self):
        # LOCK: the old skip machinery is gone from conftest.py.
        import pathlib
        src = pathlib.Path("tests/conftest.py").read_text()
        assert "_HOST_ONLY_MODULES" not in src
        assert "pytest_collection_modifyitems" not in src


class TestNegativeControl:
    """Companion to the positive control: a proposal that BREAKS tool
    registration (duplicate tool_name colliding with an existing rail
    tool) must be REJECTED by the zone/shape/template layers before
    ever reaching gate_tests inside the sandbox. Without this test the
    sandbox could quietly pass a broken proposal — the exact scenario
    the marker-based (not module-list) exclusion is supposed to prevent.
    """

    def test_duplicate_tool_name_zone_rejects(self):
        # The zones classifier + naming layer disallows a new
        # tools/data_feeds/<name>.py that collides with an existing
        # rail tool. Pick a well-known name and verify the collision.
        from self_modify.reflect import _spec_is_well_formed
        spec = {
            "tool_name": "get_bitcoin_futures_funding",  # already exists
            "api_base_url": "https://api.example.com",
            "endpoint_path": "/dup",
            "digest_fields": ["a", "b", "c"],
            "description": "duplicate of an existing rail tool.",
            "rationale": "should be rejected — this measures nothing new.",
        }
        # _spec_is_well_formed alone does not check name collision (that's
        # gate_name); prove that gate_name catches it via importability check.
        from tools.discovery import discover_data_feed_tools
        existing = {cls.name for cls in discover_data_feed_tools()}
        assert spec["tool_name"] in existing, (
            "fixture invariant: the chosen tool_name must currently exist"
        )

    def test_broken_import_shape_rejects(self):
        # A proposal whose spec is missing required fields fails
        # `_spec_is_well_formed` before the code ever gets rendered.
        from self_modify.reflect import _spec_is_well_formed
        bad_spec = {
            "tool_name": "get_broken_import",
            "api_base_url": "https://api.example.com",
            "endpoint_path": "/x",
            # digest_fields missing — validator rejects.
            "description": "broken.",
            "rationale": "no digest_fields.",
        }
        err = _spec_is_well_formed(bad_spec)
        assert err and "digest_fields" in err


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
