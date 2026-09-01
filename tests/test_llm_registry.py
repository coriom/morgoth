"""Task-router + provider layer — safety spine.

Locks:
  · with NO MORGOTH_LLM_* env set, EVERY task resolves to its
    pre-refactor default (grep-lockable + explicit-value asserted here);
  · env override takes effect and is parsed correctly;
  · bad env falls back to default without raising;
  · legacy THESIS_GENERATOR env is honored;
  · HttpApiProvider redacts the API key from error surfaces;
  · reachability probe never reads the key value.
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.llm import providers, registry, tasks
from core.llm.providers import (
    ClaudeCliProvider,
    HttpApiError,
    HttpApiKeyMissing,
    HttpApiProvider,
    OllamaProvider,
    get_provider,
    probe_reachability,
)


class TestRegistryDefaults:
    """The 'no behavior change' contract: unset env → the exact pre-refactor
    provider per task."""

    def setup_method(self):
        # Wipe every relevant env var so each test starts clean.
        for key in list(os.environ):
            if key.startswith("MORGOTH_LLM_") or key == "THESIS_GENERATOR":
                del os.environ[key]

    @pytest.mark.parametrize(
        "task, expected_provider, expected_model",
        [
            (tasks.THESIS, "ollama", "default"),
            (tasks.SYNTHESIS, "ollama", "default"),
            (tasks.CHAT, "ollama", "default"),
            (tasks.REFLECT, "claude-cli", "default"),
            (tasks.SHADOW, "claude-cli", "default"),
            (tasks.SCOUT, "claude-cli", "default"),
        ],
    )
    def test_defaults_unchanged(self, task, expected_provider, expected_model):
        provider, model = registry.resolve(task)
        assert provider == expected_provider
        assert model == expected_model

    def test_all_tasks_have_a_default(self):
        for task in tasks.all_tasks():
            provider, model = registry.resolve(task)
            assert provider in ("ollama", "claude-cli", "api")
            assert model != ""

    def test_unknown_task_raises_keyerror(self):
        with pytest.raises(KeyError):
            registry.resolve("does-not-exist")


class TestRegistryOverrides:
    def setup_method(self):
        for key in list(os.environ):
            if key.startswith("MORGOTH_LLM_") or key == "THESIS_GENERATOR":
                del os.environ[key]

    def test_env_override_takes_effect(self, monkeypatch):
        monkeypatch.setenv("MORGOTH_LLM_THESIS", "claude-cli:default")
        provider, model = registry.resolve(tasks.THESIS)
        assert provider == "claude-cli"

    def test_env_override_with_explicit_model(self, monkeypatch):
        monkeypatch.setenv("MORGOTH_LLM_THESIS", "api:claude-opus-4-7")
        provider, model = registry.resolve(tasks.THESIS)
        assert provider == "api"
        assert model == "claude-opus-4-7"

    def test_legacy_thesis_generator_env_honored(self, monkeypatch):
        monkeypatch.setenv("THESIS_GENERATOR", "claude-cli")
        provider, model = registry.resolve(tasks.THESIS)
        assert provider == "claude-cli"

    def test_new_env_wins_over_legacy(self, monkeypatch):
        monkeypatch.setenv("THESIS_GENERATOR", "claude-cli")
        monkeypatch.setenv("MORGOTH_LLM_THESIS", "ollama:default")
        provider, _ = registry.resolve(tasks.THESIS)
        assert provider == "ollama"

    def test_bad_env_falls_back_to_default_without_raising(self, monkeypatch, capsys):
        monkeypatch.setenv("MORGOTH_LLM_THESIS", "banana:garbage")
        provider, model = registry.resolve(tasks.THESIS)
        # Falls back to ollama:default; a warning is printed for visibility.
        assert provider == "ollama"
        captured = capsys.readouterr()
        assert "WARN" in captured.out


class TestRoutingTable:
    def test_routing_table_shape(self, monkeypatch):
        monkeypatch.setenv("MORGOTH_LLM_THESIS", "claude-cli:default")
        rows = registry.routing_table()
        by_task = {r["task"]: r for r in rows}
        assert by_task["thesis"]["source"] == "env"
        assert by_task["synthesis"]["source"] == "default"
        # Every task in DEFAULTS is present.
        for t in tasks.all_tasks():
            assert t in by_task


class TestOllamaProvider:
    @pytest.mark.asyncio
    async def test_wraps_shared_client(self):
        client = MagicMock()
        client.chat = AsyncMock(return_value=MagicMock(
            message=MagicMock(content="hello world"),
        ))
        p = OllamaProvider(client, "llama3.1:8b")
        out = await p.complete("hi", system="you are helpful")
        assert out == "hello world"
        client.chat.assert_awaited_once()
        # System message is prepended when provided.
        messages = client.chat.await_args.args[0]
        assert len(messages) == 2
        assert messages[0].role == "system"
        assert messages[1].role == "user"


class TestClaudeCliProvider:
    @pytest.mark.asyncio
    async def test_delegates_to_reflect_llm_subprocess(self, monkeypatch):
        async def fake_claude_cli(prompt, runner):
            return "cli-response", {"provider": "claude-cli"}
        monkeypatch.setattr("self_modify.reflect_llm._claude_cli_call", fake_claude_cli)
        p = ClaudeCliProvider("default")
        out = await p.complete("hi", system="ctx")
        assert out == "cli-response"


class TestHttpApiProvider:
    @pytest.mark.asyncio
    async def test_raises_key_missing_without_env(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        p = HttpApiProvider("default")
        with pytest.raises(HttpApiKeyMissing):
            await p.complete("hi")

    @pytest.mark.asyncio
    async def test_key_never_appears_in_error(self, monkeypatch):
        """Simulate a 401 upstream — the exception message must NOT contain
        the API key value."""
        secret = "sk-ant-SUPER-SECRET-KEY-DO-NOT-LEAK"
        monkeypatch.setenv("ANTHROPIC_API_KEY", secret)

        class FakeResp:
            status_code = 401
            reason_phrase = "Unauthorized"
            text = "invalid credentials"

        class FakeClient:
            def __init__(self, *a, **kw): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def post(self, *a, **kw):
                import httpx as _h
                raise _h.HTTPStatusError(
                    "401", request=MagicMock(), response=FakeResp(),
                )

        monkeypatch.setattr("core.llm.providers.httpx.AsyncClient", FakeClient)
        p = HttpApiProvider("default")
        with pytest.raises(HttpApiError) as ei:
            await p.complete("hi")
        # The critical assertion: the key is nowhere in the error surface.
        assert secret not in str(ei.value)
        assert secret not in repr(ei.value)

    @pytest.mark.asyncio
    async def test_parses_content_blocks(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")

        class FakeResp:
            def __init__(self): self.status_code = 200
            def raise_for_status(self): pass
            def json(self):
                return {"content": [{"type": "text", "text": "hello from api"}]}

        class FakeClient:
            def __init__(self, *a, **kw): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def post(self, *a, **kw): return FakeResp()

        monkeypatch.setattr("core.llm.providers.httpx.AsyncClient", FakeClient)
        p = HttpApiProvider("default")
        out = await p.complete("hi", system="ctx")
        assert out == "hello from api"


class TestReachabilityProbe:
    def test_probe_shape_and_key_not_read(self, monkeypatch):
        # Set a fake key; the probe must NOT include the value in its output.
        secret = "sk-ant-DO-NOT-LEAK"
        monkeypatch.setenv("ANTHROPIC_API_KEY", secret)
        result = probe_reachability()
        assert set(result.keys()) == {"ollama", "claude-cli", "api"}
        for name, (ok, note) in result.items():
            assert isinstance(ok, bool)
            assert secret not in note


class TestGetProviderFactory:
    def test_ollama_requires_client(self):
        with pytest.raises(ValueError, match="ollama provider requires"):
            get_provider("ollama", "default", ollama_client=None)

    def test_claude_cli_stateless(self):
        p = get_provider("claude-cli", "default")
        assert isinstance(p, ClaudeCliProvider)

    def test_api_stateless(self):
        p = get_provider("api", "default")
        assert isinstance(p, HttpApiProvider)

    def test_unknown_raises(self):
        with pytest.raises(ValueError, match="unknown provider"):
            get_provider("banana", "default")
