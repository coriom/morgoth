"""Generation-cycle tool schema trim.

Generation's mandated action is create_objective; passing every data-tool
schema (15+) bloats the prompt with ~5.4 KB of unused JSON — the delta
that pushed prompts over 4096 during active campaigns. Restricting to
{create_objective, recall} drops prompt_eval_count by ~1009 tokens on a
real campaign-mode prompt (measured 2026-09-22: 2927 → 1918).

Work cycles keep the full schema list unchanged.
"""

from __future__ import annotations

import inspect


class TestGenerationToolSet:
    def test_generation_tool_names_declared(self):
        from core.brain import GENERATION_TOOL_NAMES
        # Strictly what generation needs: create_objective + recall.
        # Any expansion here means someone routed a data tool into the
        # generation path — regression, must be justified explicitly.
        assert GENERATION_TOOL_NAMES == ("create_objective", "recall")

    def test_generation_tool_set_is_subset_of_chat(self):
        from core.brain import GENERATION_TOOL_NAMES, CHAT_TOOL_NAMES
        for t in GENERATION_TOOL_NAMES:
            assert t in CHAT_TOOL_NAMES

    def test_process_message_accepts_tool_names_kwarg(self):
        from core.brain import Brain
        sig = inspect.signature(Brain.process_message)
        assert "tool_names" in sig.parameters
        # Keyword-only so a stray positional caller can't mask the default.
        assert sig.parameters["tool_names"].kind == inspect.Parameter.KEYWORD_ONLY


class TestRunLoopWiring:
    def test_generation_cycle_uses_trimmed_set(self):
        # Grep-lock: run_autonomous_cycle branches on `objectives` and
        # passes GENERATION_TOOL_NAMES when no objective is claimed
        # (that IS the generation path). Work cycles pass None → full.
        from core import brain
        src = inspect.getsource(brain.Brain.run_autonomous_cycle)
        assert "GENERATION_TOOL_NAMES" in src
        assert "tool_names=_tool_names_for_cycle" in src
        # None means "full CHAT_TOOL_NAMES" — proves work-cycle unchanged.
        assert "None if objectives else GENERATION_TOOL_NAMES" in src

    def test_process_message_uses_kwarg_when_supplied(self):
        # process_message body selects between the passed list and the
        # default CHAT_TOOL_NAMES — grep-lock the branch.
        from core import brain
        src = inspect.getsource(brain.Brain.process_message)
        assert "tool_names" in src
        assert "CHAT_TOOL_NAMES" in src


class TestGenerationPromptSizeBound:
    def test_trimmed_schemas_stay_small(self):
        """The two-tool schema JSON must stay well under the historical
        6.3 KB full-schema block. Concrete bound: less than 1500 chars
        so that even a fat campaign accumulation block (2 KB) plus a
        fat generation context (3.7 KB) leaves ~1 KB budget under 4096
        tokens (assuming ~3 chars/token for JSON + english mix)."""
        import json
        from core.tool_router import ToolRouter
        from core.brain import GENERATION_TOOL_NAMES
        from tools.objectives_tool import CreateObjectiveTool

        # Register minimal stubs that satisfy the schema shape.
        class _FakePM:
            async def initialize(self): pass
        class _FakeEM:
            async def query(self, *a, **kw): return []
        router = ToolRouter()
        router.register(CreateObjectiveTool(_FakePM()))
        # RecallTool needs the episodic memory; import lazily so the test
        # doesn't require chromadb at collection time.
        from tools.memory_tools import RecallTool
        router.register(RecallTool(_FakeEM()))

        schemas = router.get_schemas(list(GENERATION_TOOL_NAMES))
        assert len(schemas) == 2
        json_size = len(json.dumps(schemas))
        assert json_size < 1500, (
            f"generation schemas grew to {json_size} chars — a new field "
            "or expanded description slipped in; verify the token budget"
        )
