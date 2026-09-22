"""Ollama num_ctx wiring — the default is 4096 and cycle prompts hit it.

2026-09-22: journal showed 20 truncation events over 30 days, all with
`limit=4096 prompt=~4960 keep=5 new=4096`. The client now sends an
explicit num_ctx on every /api/chat, sourced from AppConfig, and warns
+ persists an event when prompt_eval_count sits at the ceiling.
"""

from __future__ import annotations

import inspect


class TestConfig:
    def test_num_ctx_field_default(self):
        from core.config import AppConfig
        # Field is declared with default=8192 (env alias OLLAMA_NUM_CTX).
        src = inspect.getsource(AppConfig)
        assert 'ollama_num_ctx' in src
        assert 'OLLAMA_NUM_CTX' in src
        assert 'default=8192' in src


class TestClientInjection:
    def test_chat_injects_num_ctx_option(self):
        from core.llm_client import OllamaLLMClient
        src = inspect.getsource(OllamaLLMClient.chat)
        # The chat method builds an options dict with num_ctx from config
        # regardless of whether the caller passed options.
        assert 'num_ctx' in src
        assert 'ollama_num_ctx' in src

    def test_saturation_check_wired(self):
        from core.llm_client import OllamaLLMClient
        # Post-response saturation guard exists and is invoked.
        assert hasattr(OllamaLLMClient, '_check_ctx_saturation')
        chat_src = inspect.getsource(OllamaLLMClient.chat)
        assert '_check_ctx_saturation' in chat_src

    def test_attach_persistent_memory_helper(self):
        from core.llm_client import OllamaLLMClient
        # brain.py attaches PM post-init so events can persist.
        assert hasattr(OllamaLLMClient, 'attach_persistent_memory')

    def test_saturation_threshold_at_95pct(self):
        # Guard fires at prompt_eval_count >= 0.95 * num_ctx — catches the
        # front-cut truncation ceiling with a small safety margin.
        from core.llm_client import OllamaLLMClient
        src = inspect.getsource(OllamaLLMClient._check_ctx_saturation)
        assert '0.95' in src


class TestPersistentMemoryTable:
    def test_ctx_saturation_events_ddl_and_recorder(self):
        import inspect
        from memory.persistent import PersistentMemory
        init_src = inspect.getsource(PersistentMemory.initialize)
        assert 'ctx_saturation_events' in init_src
        assert 'prompt_tokens' in init_src
        assert 'num_ctx' in init_src
        # Recorder method exists.
        assert hasattr(PersistentMemory, 'record_ctx_saturation_event')


class TestSynthesisRouting:
    def test_synthesis_goes_through_registry(self):
        import inspect
        from core import brain
        src = inspect.getsource(brain.Brain._synthesize_objective)
        # Registry-mediated: MORGOTH_LLM_SYNTHESIS becomes a real switch.
        assert 'registry' in src
        assert '_tasks.SYNTHESIS' in src
        assert 'call_with_fallback' in src or '_fallback' in src

    def test_synthesis_task_declared_default_ollama(self):
        # Regression: default MUST still be ollama:default so unset env
        # reproduces pre-refactor behavior byte-identically.
        from core.llm import tasks as T
        assert T.SYNTHESIS == 'synthesis'
        assert T.DEFAULTS[T.SYNTHESIS] == 'ollama:default'


class TestBrainWiresPmToClient:
    def test_brain_calls_attach_pm(self):
        import inspect
        from core import brain
        src = inspect.getsource(brain.Brain.__init__)
        assert 'attach_persistent_memory' in src


class TestSessionReportRendersCtxSaturation:
    def test_report_renders_ctx_saturations_line(self):
        from analysis.session_report import SessionReport
        from datetime import datetime, timedelta, timezone
        r = SessionReport(
            since=datetime.now(timezone.utc) - timedelta(hours=1),
            now=datetime.now(timezone.utc),
        )
        r.ctx_saturations = 3
        out = r.render()
        assert 'CTX SATURATION' in out
        assert '3 prompt' in out
