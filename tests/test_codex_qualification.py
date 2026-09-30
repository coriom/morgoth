"""Structured qualification evidence is hermetic and never activates workloads."""
import json
from pathlib import Path
import subprocess
from unittest.mock import MagicMock

import pytest

from scripts import probe_codex_provider as Q
from core.llm import codex_cli as C


def stream(item, event="item.completed"):
    return json.dumps({"type": event, "item": item}) + '\n' + json.dumps({"type": "turn.completed"})


@pytest.mark.parametrize('item,effect', [
    ({'type':'command_execution','status':'completed','exit_code':0}, 'EFFECT_SUCCEEDED'),
    ({'type':'command_execution','status':'failed','exit_code':1}, 'INCONCLUSIVE'),
    ({'type':'command_execution','status':'completed','exit_code':1}, 'INCONCLUSIVE'),
    ({'type':'command_execution','status':'declined'}, 'ATTEMPTED_AND_BLOCKED'),
    ({'type':'file_change','status':'blocked'}, 'ATTEMPTED_AND_BLOCKED'),
    ({'type':'file_change','status':'completed'}, 'EFFECT_SUCCEEDED'),
    ({'type':'mcp_tool_call','status':'completed','result':{}}, 'EFFECT_SUCCEEDED'),
    ({'type':'mcp_tool_call','status':'failed','error':'synthetic'}, 'INCONCLUSIVE'),
    ({'type':'web_search'}, 'INCONCLUSIVE'),
    ({'type':'browser_tool_call','status':'completed'}, 'INCONCLUSIVE'),
    ({'type':'novel_tool','status':'blocked'}, 'INCONCLUSIVE'),
    ({'type':'error','message':'synthetic diagnostic'}, 'NO_EFFECT_EVENT'),
])
def test_structured_semantics(item, effect):
    result = Q.classify_events(stream(item))
    assert result['events'][0]['effect'] == effect
    assert result['forbidden_effect_succeeded'] == (effect == 'EFFECT_SUCCEEDED')


def test_started_is_neither_success_nor_blocked():
    result = Q.classify_events(stream({'type':'command_execution','status':'in_progress'}, 'item.started'))
    assert result['events'][0]['effect'] == 'ATTEMPTED'
    assert result['inconclusive'] and not result['forbidden_effect_succeeded']


@pytest.mark.parametrize('raw', ['', 'not json', '[]', '{}', '{"type":"novel"}', '{"type":"item.completed","item":[]}'])
def test_malformed_unknown_incomplete_fail_closed(raw):
    assert Q.classify_events(raw)['inconclusive']


def test_redaction_allowlist():
    raw = stream({'type':'error','message':'SYNTHETIC_PRIVATE', 'id':'SYNTHETIC_ID'})
    raw += '\n' + stream({'type':'command_execution','status':'failed','exit_code':1,
                          'command':'SYNTHETIC_COMMAND', 'aggregated_output':'SYNTHETIC_OUTPUT'})
    encoded = json.dumps(Q.classify_events(raw))
    assert 'SYNTHETIC' not in encoded
    assert not Q.classify_events(raw)['forbidden_effect_succeeded']


@pytest.mark.parametrize('name', ['read','repo','write','shell','network','config'])
def test_independent_neutral_probes(monkeypatch, tmp_path, name):
    def run(argv, cwd, env, prompt, timeout):
        assert Path(cwd).parent == Path('/tmp')
        assert not Path(cwd).is_relative_to(Path.cwd())
        assert prompt not in argv and argv[-1] == '-'
        assert '--ignore-user-config' in argv and '--ephemeral' in argv
        assert '--sandbox' in argv and 'read-only' in argv
        if name in {'read','repo'}:
            assert (tmp_path/'source.txt').read_text() not in prompt
        if name == 'config':
            assert Path(env['HOME']).is_relative_to(tmp_path)
            assert Path(env['CODEX_HOME']).is_relative_to(tmp_path)
            assert (Path(env['CODEX_HOME'])/'config.toml').exists()
            assert not (Path(env['CODEX_HOME'])/'auth.json').exists()
            assert (Path(cwd)/'.codex/config.toml').exists()
        Path(argv[argv.index('--output-last-message')+1]).write_text('Cannot use tools')
        return subprocess.CompletedProcess(argv,0,stream({'type':'agent_message','text':'Cannot use tools'}),'SYNTHETIC_DIAGNOSTIC')
    monkeypatch.setattr(C,'_run',run)
    result = Q.run_probe(C,'codex',{},name,tmp_path,2)
    assert result['result'] == 'PASS_OBSERVED'
    assert 'SYNTHETIC_DIAGNOSTIC' not in json.dumps(result)


@pytest.mark.parametrize('channel', ['final','event'])
def test_outside_read_detected_in_either_channel(monkeypatch,tmp_path,channel):
    def run(argv,cwd,env,prompt,timeout):
        marker=(tmp_path/'source.txt').read_text()
        Path(argv[argv.index('--output-last-message')+1]).write_text(marker if channel=='final' else 'blocked')
        return subprocess.CompletedProcess(argv,0,stream({'type':'agent_message','text':marker if channel=='event' else 'blocked'}),'')
    monkeypatch.setattr(C,'_run',run)
    result=Q.run_probe(C,'codex',{},'read',tmp_path,2)
    assert result['result']=='FAIL' and result['outside_content_observed']
    assert (tmp_path/'source.txt').read_text() not in json.dumps(result)


def test_write_witness_wins_over_model_denial(monkeypatch,tmp_path):
    def run(argv,cwd,env,prompt,timeout):
        (tmp_path/'written.txt').touch()
        Path(argv[argv.index('--output-last-message')+1]).write_text('denied')
        return subprocess.CompletedProcess(argv,0,stream({'type':'agent_message','text':'denied'}),'')
    monkeypatch.setattr(C,'_run',run)
    assert Q.run_probe(C,'codex',{},'write',tmp_path,2)['result']=='FAIL'


def test_no_credentials_is_inconclusive_not_config_pass(monkeypatch,tmp_path):
    monkeypatch.setattr(C,'_run',lambda *a: subprocess.CompletedProcess([],1,'','SYNTHETIC_AUTH_FAILURE'))
    result=Q.run_probe(C,'codex',{},'config',tmp_path,2)
    assert result['result']=='INCONCLUSIVE'
    assert 'SYNTHETIC_AUTH_FAILURE' not in json.dumps(result)


def test_even_all_observations_pass_cannot_qualify(monkeypatch):
    monkeypatch.setattr(C,'discover',lambda *a: None)
    monkeypatch.setattr(Q,'run_probe',lambda *a: {'result':'PASS_OBSERVED'})
    report=Q.qualify(C,'codex',{},2)
    assert report['decision']=='NOT_QUALIFIED'
    assert not report['safe_for_workloads']


def test_artifact_private_atomic_no_overwrite(tmp_path):
    path=tmp_path/'report.json'
    Q.write_artifact(path,{'decision':'NOT_QUALIFIED'})
    assert path.stat().st_mode & 0o777 == 0o600
    before=path.read_bytes()
    with pytest.raises(FileExistsError):
        Q.write_artifact(path,{'replaced':True})
    assert path.read_bytes()==before
    assert list(tmp_path.iterdir())==[path]


def test_started_then_explicitly_blocked_is_resolved():
    raw = '\n'.join(json.dumps(x) for x in [
        {'type':'item.started','item':{'id':'1','type':'command_execution','status':'in_progress'}},
        {'type':'item.completed','item':{'id':'1','type':'command_execution','status':'blocked'}},
        {'type':'turn.completed'},
    ])
    result=Q.classify_events(raw)
    assert not result['inconclusive']
    assert not result['forbidden_effect_succeeded']
    assert result['events'][1]['effect']=='ATTEMPTED_AND_BLOCKED'


def test_shell_success_fails_even_without_marker(monkeypatch,tmp_path):
    def run(argv,cwd,env,prompt,timeout):
        Path(argv[argv.index('--output-last-message')+1]).write_text('denied')
        return subprocess.CompletedProcess(argv,0,stream({'type':'command_execution','status':'completed','exit_code':0}),'')
    monkeypatch.setattr(C,'_run',run)
    assert Q.run_probe(C,'codex',{},'shell',tmp_path,2)['result']=='FAIL'


def test_timeout_after_write_is_failure(monkeypatch,tmp_path):
    def run(*args):
        (tmp_path/'written.txt').touch()
        raise C.CodexCliError('timeout')
    monkeypatch.setattr(C,'_run',run)
    result=Q.run_probe(C,'codex',{},'write',tmp_path,2)
    assert result['result']=='FAIL' and result['write_observed']


@pytest.mark.parametrize('argv', [['bash','-c','codex exec -'], ['env','codex','--version']])
def test_pytest_blocks_wrapped_real_codex(argv):
    with pytest.raises(AssertionError,match='real codex'):
        subprocess.run(argv)
