"""Hermetic migration/security contract. Every subprocess is mocked."""
import json
import os
from pathlib import Path
import subprocess
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.llm import codex_cli as C, fallback, registry, tasks, heartbeat
from core.llm.providers import get_provider
from self_modify.reflect_llm import resolve_provider, reflect_chat


@pytest.fixture(autouse=True)
def clean_cache():
    C._QUALIFIED.clear()
    C._REJECTED.clear()
    yield
    C._QUALIFIED.clear()
    C._REJECTED.clear()


def events(kind="agent_message"):
    return '\n'.join(json.dumps(x) for x in [
        {"type": "item.completed", "item": {"type": kind, "text": "answer"}},
        {"type": "turn.completed"},
    ])


def fake_run(argv, cwd, env, prompt, timeout):
    assert Path(cwd).is_dir()
    assert str(Path.cwd()) not in cwd
    assert argv[-1] == '-'
    assert prompt not in argv
    assert timeout > 0
    Path(argv[argv.index('--output-last-message') + 1]).write_text('answer')
    return subprocess.CompletedProcess(argv, 0, events(), 'PRIVATE_DIAGNOSTICS')


@pytest.mark.parametrize('task', ['thesis', 'synthesis', 'reflect', 'shadow', 'scout'])
def test_default_routing(monkeypatch, task):
    for key in list(os.environ):
        if key.startswith('MORGOTH_LLM_') or key == 'THESIS_GENERATOR':
            monkeypatch.delenv(key)
    assert registry.resolve(task) == ('codex-cli', 'default')
    assert registry._parse_spec('codex-cli:default') == ('codex-cli', 'default')
    assert get_provider('codex-cli', 'default').name == 'codex-cli'
    assert 'claude-cli' not in tasks.DEFAULTS.values()


def test_argv():
    argv = C.build_argv('codex', 'custom-model', '/tmp/final')
    for flag in C.REQUIRED_FLAGS:
        assert flag in argv
    assert argv[argv.index('--model') + 1] == 'custom-model'
    assert '--model' not in C.build_argv('codex', 'default', '/tmp/final')
    assert argv[argv.index('--sandbox') + 1] == 'read-only'
    for flag in C.DISABLED:
        assert ['--disable', flag] in [argv[i:i+2] for i in range(len(argv)-1)]
    for config in C.CONFIG:
        assert config in argv
    assert 'web_search="disabled"' in argv
    assert 'mcp_servers={}' in argv
    assert 'approval_policy="never"' in argv


def test_minimal_environment(monkeypatch):
    for key in ['OPENAI_API_KEY', 'ANTHROPIC_API_KEY', 'DATABASE_URL', 'TELEGRAM_BOT_TOKEN',
                'CODEX_THREAD_ID', 'CODEX_INTERNAL_ORIGINATOR_OVERRIDE', 'NODE_OPTIONS', 'PYTHONPATH']:
        monkeypatch.setenv(key, 'SYNTHETIC_NOT_A_SECRET')
    assert set(C.minimal_env()) <= {'HOME', 'PATH', 'CODEX_HOME', 'LANG'}


def test_neutral_cwd_final_only(monkeypatch):
    seen = []
    def run(*args):
        seen.append(args[1])
        return fake_run(*args)
    monkeypatch.setattr(C, '_run', run)
    assert C.invoke('codex', 'default', {}, 'private prompt', 10) == 'answer'
    assert not Path(seen[0]).exists()


@pytest.mark.parametrize('kind', ['command_execution', 'file_change', 'web_search', 'mcp_tool_call',
                                 'collab_tool_call', 'plugin_tool_call', 'unknown'])
def test_tool_events_fail_closed(monkeypatch, kind):
    def run(*args):
        result = fake_run(*args)
        result.stdout = events(kind)
        return result
    monkeypatch.setattr(C, '_run', run)
    with pytest.raises(C.CodexCliError, match='tool_activity'):
        C.invoke('codex', 'default', {}, 'prompt', 2)


@pytest.mark.parametrize('mode,code', [('exit','process_failed'), ('empty','empty_final'),
                                     ('missing','missing_final'), ('badjson','invalid_events'),
                                     ('auth','process_failed'), ('quota','process_failed')])
def test_failures_redacted(monkeypatch, mode, code):
    def run(*args):
        result = fake_run(*args)
        if mode in ('exit','auth','quota'):
            result.returncode = 1
            result.stdout = result.stderr = 'SYNTHETIC_PRIVATE'
        elif mode == 'empty':
            Path(args[0][args[0].index('--output-last-message')+1]).write_text(' ')
        elif mode == 'missing':
            Path(args[0][args[0].index('--output-last-message')+1]).unlink()
        else:
            result.stdout = 'SYNTHETIC_PRIVATE'
        return result
    monkeypatch.setattr(C, '_run', run)
    with pytest.raises(C.CodexCliError, match=code) as exc:
        C.invoke('codex', 'default', {}, 'prompt', 2)
    assert 'SYNTHETIC_PRIVATE' not in str(exc.value)


def test_discovery_missing_capability(monkeypatch):
    def run(argv, *args):
        text = C.VERSION if '--version' in argv else ' '.join(C.REQUIRED_FLAGS)
        return subprocess.CompletedProcess(argv, 0, text, '')
    monkeypatch.setattr(C, '_run', run)
    with pytest.raises(C.CodexCliError, match='missing_safety_capability'):
        C.discover('codex', '/tmp', {})


def test_unsupported_version(monkeypatch):
    monkeypatch.setattr(C, '_run', lambda argv,*a: subprocess.CompletedProcess(argv,0,'future',''))
    with pytest.raises(C.CodexCliError, match='unsupported_version'):
        C.discover('codex','/tmp',{})


def test_missing_binary(monkeypatch):
    monkeypatch.setattr(C, "SAFE_FOR_WORKLOADS", True)
    monkeypatch.setattr(C.shutil, 'which', lambda *a, **kw: None)
    with pytest.raises(C.CodexCliError, match='missing_binary'):
        C._complete('prompt','default',2)


def test_stdin_and_separate_pipes(monkeypatch):
    proc = MagicMock()
    proc.__enter__.return_value = proc
    proc.communicate.return_value = ('output','diagnostic')
    proc.returncode = 0
    popen = MagicMock(return_value=proc)
    monkeypatch.setattr(C.subprocess, 'Popen', popen)
    result = C._run(['codex','exec','-'], '/tmp/neutral', {}, 'private prompt', 3)
    assert result.stdout == 'output' and result.stderr == 'diagnostic'
    proc.communicate.assert_called_once_with('private prompt', timeout=3)
    assert popen.call_args.kwargs['stdout'] == subprocess.PIPE
    assert popen.call_args.kwargs['stderr'] == subprocess.PIPE
    assert not popen.call_args.kwargs.get('shell', False)


def test_timeout_kills_group(monkeypatch):
    proc = MagicMock()
    proc.__enter__.return_value = proc
    proc.communicate.side_effect = [subprocess.TimeoutExpired('codex', 2), ('','')]
    monkeypatch.setattr(C.subprocess, 'Popen', MagicMock(return_value=proc))
    kill = MagicMock()
    monkeypatch.setattr(C.os, 'killpg', kill)
    with pytest.raises(C.CodexCliError, match='timeout'):
        C._run(['codex'], '/tmp', {}, 'prompt', 2)
    kill.assert_called_once()


@pytest.mark.parametrize('leak', [True, False])
def test_canary_marker_not_in_prompt(monkeypatch, leak):
    def invoke(binary, model, env, prompt, timeout):
        path = Path(prompt.split()[1])
        marker = path.read_text()
        assert marker not in prompt
        return marker if leak else 'CANARY_BLOCKED'
    monkeypatch.setattr(C, 'invoke', invoke)
    if leak:
        with pytest.raises(C.CodexCliError, match='canary_failed'):
            C.capability_canary('codex','default',{},2)
    else:
        C.capability_canary('codex','default',{},2)


def test_failed_canary_latches_before_real_prompt(monkeypatch, tmp_path):
    monkeypatch.setattr(C, "SAFE_FOR_WORKLOADS", True)
    binary = tmp_path / 'codex'; binary.touch()
    monkeypatch.setattr(C.shutil,'which',lambda *a,**kw: str(binary))
    monkeypatch.setattr(C,'discover',lambda *a: None)
    canary = MagicMock(side_effect=C.CodexCliError('canary_failed'))
    invoke = MagicMock()
    monkeypatch.setattr(C,'capability_canary',canary)
    monkeypatch.setattr(C,'invoke',invoke)
    for _ in range(2):
        with pytest.raises(C.CodexCliError): C._complete('real prompt','default',2)
    canary.assert_called_once(); invoke.assert_not_called()


@pytest.mark.asyncio
async def test_fallback_never_claude(monkeypatch):
    monkeypatch.setenv('LLM_FALLBACK_ENABLED','true')
    names=[]
    def build(name):
        names.append(name)
        return MagicMock(name=name)
    async def call(provider):
        if len(names)<3: raise C.CodexCliError('process_failed')
        return 'ollama result'
    assert await fallback.call_with_fallback(build,'thesis','api',call) == 'ollama result'
    assert names == ['api','codex-cli','ollama']
    assert fallback._next_provider_down('claude-cli') == 'ollama'


@pytest.mark.asyncio
async def test_reflect_selection_and_rollback(monkeypatch):
    monkeypatch.delenv('REFLECT_PROVIDER',raising=False)
    monkeypatch.delenv('MORGOTH_LLM_REFLECT',raising=False)
    assert resolve_provider(None) == 'codex-cli'
    assert resolve_provider('claude-cli') == 'claude-cli'
    monkeypatch.setenv('MORGOTH_LLM_REFLECT','codex-cli:custom')
    assert resolve_provider(None) == 'codex-cli:custom'
    complete = AsyncMock(return_value='text')
    monkeypatch.setattr(C.CodexCliProvider,'complete',complete)
    assert (await reflect_chat('prompt',MagicMock(),'codex-cli'))[0] == 'text'


def test_heartbeat_no_inference(monkeypatch):
    monkeypatch.setattr(heartbeat.shutil,'which',lambda _: '/fake/codex')
    run = MagicMock(return_value=subprocess.CompletedProcess([],0,C.VERSION,'private'))
    monkeypatch.setattr(heartbeat.subprocess,'run',run)
    assert heartbeat.probe_codex_cli().status == 'ok'
    assert run.call_args.args[0] == ['/fake/codex','--version']
    assert run.call_args.kwargs['env'] == C.minimal_env()


@pytest.mark.parametrize('argv', [['codex','exec','private'], ['/tmp/codex','--version']])
def test_real_binary_guard(argv):
    with pytest.raises(AssertionError, match='real codex'):
        subprocess.run(argv)


def test_failed_live_qualification_blocks_all_workloads(monkeypatch):
    assert C.SAFE_FOR_WORKLOADS is False
    invoke = MagicMock()
    monkeypatch.setattr(C, "invoke", invoke)
    monkeypatch.setenv("CODEX_PROVIDER_UNSAFE_OVERRIDE", "true")
    with pytest.raises(C.CodexCliError, match="capability_restrictions_unqualified"):
        C._complete("real workload must never reach CLI", "default", 10)
    invoke.assert_not_called()


@pytest.mark.asyncio
async def test_unqualified_provider_falls_to_local_only(monkeypatch):
    monkeypatch.setenv("LLM_FALLBACK_ENABLED", "true")
    names = []
    local = MagicMock()
    local.complete = AsyncMock(return_value="local result")
    def build(name):
        names.append(name)
        return C.CodexCliProvider("default") if name == "codex-cli" else local
    async def call(provider):
        return await provider.complete("prompt")
    assert await fallback.call_with_fallback(build, "thesis", "codex-cli", call) == "local result"
    assert names == ["codex-cli", "ollama"]


@pytest.mark.asyncio
async def test_disabled_fallback_preserves_failure(monkeypatch):
    monkeypatch.setenv("LLM_FALLBACK_ENABLED", "false")
    async def call(provider):
        return await provider.complete("prompt")
    with pytest.raises(C.CodexCliError, match="capability_restrictions_unqualified"):
        await fallback.call_with_fallback(lambda _: C.CodexCliProvider("default"),
                                         "thesis", "codex-cli", call)
