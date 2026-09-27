"""Discovery must not take the rail down on a single bad file.

Before 2026-09-29 (this commit): one tool with a syntax error broke
`discover_data_feed_tools()` for ALL tools — a production hazard
(one bad file at restart takes the whole rail down) AND why the
negative gate-selftest saw 200 test failures cascading.

After: broken modules are excluded, logged, and reported via
`discovery_load_errors()`. A "zero load errors" test locks that
in — a proposal that ships a broken file still FAILS the gate, but
via THIS test (concise signal), not via 200 cascading assertions."""

from __future__ import annotations

import pathlib
import textwrap

from tools.discovery import discover_data_feed_tools, discovery_load_errors


def test_current_tree_has_zero_load_errors():
    """The live tree must import cleanly. If this fails, a data_feed
    file is broken — fix the file (or delete it if unused). Never
    catch this by expanding the exclusion list."""
    tools = discover_data_feed_tools()
    errs = discovery_load_errors()
    assert errs == [], (
        f"discovery reported {len(errs)} load error(s); the rail is "
        f"running WITHOUT those tools. First error: {errs[0] if errs else ''}"
    )
    # And we actually found some tools — otherwise the test is vacuous.
    assert len(tools) >= 5, f"discovery found only {len(tools)} tools"


def test_broken_module_excluded_others_survive(tmp_path, monkeypatch):
    """Inject a broken module into a temporary tools/data_feeds/-shaped
    package and prove: (a) discovery returns the good tools, (b) the
    broken one is logged in discovery_load_errors, (c) no exception
    escapes.

    Verified against SyntaxError specifically because that's what a
    proposal-shape injection error looks like."""
    pkg = tmp_path / "data_feeds"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    # a good module
    (pkg / "get_good_probe.py").write_text(textwrap.dedent("""
        from tools.base_tool import BaseTool
        class GetGoodProbe(BaseTool):
            name = "get_good_probe"
            description = "probe"
            is_data_source = True
            async def run(self, **kw): return {}
    """).strip(), encoding="utf-8")
    # a broken module (SyntaxError at line 1)
    (pkg / "get_broken_probe.py").write_text(
        "1 = 2  # SyntaxError injected by the test\n", encoding="utf-8"
    )
    # Point tools.data_feeds at this temp package.
    import sys
    from tools import data_feeds
    monkeypatch.setattr(data_feeds, "__path__", [str(pkg)])
    # Purge any cached imports so the discovery pass re-imports fresh.
    for name in list(sys.modules):
        if name.startswith("tools.data_feeds."):
            del sys.modules[name]
    classes = discover_data_feed_tools()
    errs = discovery_load_errors()
    names = {c.name for c in classes}
    assert "get_good_probe" in names, "good tool must survive discovery"
    assert any("get_broken_probe" in mod for mod, _ in errs), (
        f"broken module must be reported; errs={errs}"
    )
