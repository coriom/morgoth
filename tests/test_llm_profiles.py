"""Project profile control plane; no real provider, database, or production state."""
from contextlib import asynccontextmanager
from dataclasses import FrozenInstanceError
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.project import Project
from core.llm import profiles as P, registry, tasks, codex_cli, environment as E


@pytest.fixture
def projects(tmp_path, monkeypatch):
    for key in list(os.environ):
        if key.startswith('MORGOTH_LLM_') or key in {'THESIS_GENERATOR','REFLECT_PROVIDER','SHADOW_PROVIDER'}:
            monkeypatch.delenv(key)
    def make(name):
        return Project(id=name, name=name, domain='crypto', postgres_schema=name,
                       chroma_prefix=name+'_', vault_dir=tmp_path/name/'vault', runtime_dir=tmp_path/name/'state')
    a,b=make('project_a'),make('project_b')
    monkeypatch.setattr('core.project.current_project', lambda:a)
    return a,b


@pytest.fixture
def ready(monkeypatch):
    cap=lambda status: E.Capability(status,'synthetic')
    env=E.Environment('test',cap('ok'),cap('ok'),cap('ok'),cap('unavailable'),cap('ok'))
    monkeypatch.setattr(E,'detect_environment',AsyncMock(return_value=env))
    return env


@pytest.fixture
def no_campaign(monkeypatch):
    @asynccontextmanager
    async def guard(project):
        yield
    monkeypatch.setattr(P,'campaign_guard',guard)


def test_registry_data_immutable(projects):
    for name, provider in [('claude','claude-cli'),('codex','codex-cli')]:
        profile=P.PROFILES[name]
        assert set(profile.routes)==set(tasks.all_tasks())
        assert profile.routes['chat']=='ollama:default'
        assert all(route==provider+':default' for task,route in profile.routes.items() if task!='chat')
        with pytest.raises(TypeError): profile.routes['chat']='api:default'
        with pytest.raises(FrozenInstanceError): profile.id='changed'
    with pytest.raises(TypeError): P.PROFILES['new']=P.PROFILES['claude']


def test_absent_state_exact_legacy(projects,monkeypatch):
    a,_=projects
    assert P.current_profile(a).id=='legacy'
    assert not a.runtime_dir.exists()
    for task,spec in tasks.DEFAULTS.items():
        assert P.resolve_task_route(a,task)==registry._parse_spec(spec)
    monkeypatch.setenv('THESIS_GENERATOR','claude-cli')
    assert registry.resolve('thesis')==('claude-cli','default')
    monkeypatch.setenv('MORGOTH_LLM_THESIS','ollama:custom')
    assert registry.resolve('thesis')==('ollama','custom')
    monkeypatch.setenv('MORGOTH_LLM_THESIS','bad')
    assert registry.resolve('thesis')==registry._parse_spec(tasks.DEFAULTS['thesis'])


@pytest.mark.asyncio
async def test_atomic_all_tasks_reload_and_legacy(projects,ready,no_campaign,monkeypatch):
    a,_=projects
    for task in tasks.all_tasks(): monkeypatch.setenv('MORGOTH_LLM_'+task.upper(),'codex-cli:default')
    monkeypatch.setenv('REFLECT_PROVIDER','codex-cli')
    await P.switch_profile(a,'claude')
    assert json.loads(P.state_path(a).read_text())=={'schema_version':1,'profile':'claude'}
    assert P.state_path(a).stat().st_mode & 0o777 == 0o600
    assert P.state_path(a).parent.stat().st_mode & 0o777 == 0o700
    for task in tasks.all_tasks():
        assert registry.resolve(task)==('ollama' if task=='chat' else 'claude-cli','default')
    assert 'REFLECT_PROVIDER' in P.shadowed_overrides(a)
    assert all(row['source']=='managed profile' for row in registry.routing_table())
    await P.switch_profile(a,'legacy')
    assert registry.resolve('thesis')==('codex-cli','default')
    assert P.shadowed_overrides(a)==()


@pytest.mark.asyncio
async def test_two_projects_same_domain_independent(projects,ready,no_campaign):
    a,b=projects
    await P.switch_profile(a,'claude')
    await P.switch_profile(b,'legacy')
    assert a.domain==b.domain=='crypto'
    assert P.resolve_task_route(a,'thesis')==('claude-cli','default')
    assert P.resolve_task_route(b,'thesis')==('codex-cli','default')
    assert not P.state_path(b).exists()


@pytest.mark.asyncio
async def test_codex_rejected_no_change_even_binary_ready(projects,ready,no_campaign):
    a,_=projects
    await P.switch_profile(a,'claude')
    before=P.state_path(a).read_bytes()
    with pytest.raises(P.ProfileError,match='NOT_QUALIFIED'):
        await P.switch_profile(a,'codex')
    assert P.state_path(a).read_bytes()==before
    assert codex_cli.SAFE_FOR_WORKLOADS is False
    assert P.provider_status('codex-cli',ready)[0]=='BLOCKED'
    assert P.profile_status(a,P.PROFILES['codex'],ready)[0]=='BLOCKED'
    assert P.recommended_profile(a,ready)=='claude'
    assert all(r.provider!='codex-cli' for r in E.suggest_routing(ready))


@pytest.mark.parametrize('raw', ['{}','null','not json','{"schema_version":2,"profile":"claude"}',
    '{"schema_version":true,"profile":"claude"}', '{"schema_version":1,"profile":"unknown"}',
    '{"schema_version":1,"profile":"claude","secret":"synthetic"}'])
def test_malformed_fail_closed(projects,raw):
    a,_=projects
    a.runtime_dir.mkdir(parents=True)
    P.state_path(a).write_text(raw)
    with pytest.raises(P.ProfileError): registry.resolve('thesis')


@pytest.mark.asyncio
async def test_unknown_never_persists(projects):
    a,_=projects
    with pytest.raises(P.ProfileError,match='unknown'): await P.switch_profile(a,'invalid')
    assert not a.runtime_dir.exists()


@pytest.mark.asyncio
async def test_publish_failure_preserves_whole_profile(projects,ready,no_campaign,monkeypatch):
    a,_=projects
    await P.switch_profile(a,'claude')
    before=P.state_path(a).read_bytes()
    def fail(*args): raise OSError('synthetic')
    monkeypatch.setattr(P.os,'replace',fail)
    with pytest.raises(P.ProfileError): await P.switch_profile(a,'legacy')
    assert P.state_path(a).read_bytes()==before
    assert not list(a.runtime_dir.glob('.llm-profile-*'))


@pytest.mark.asyncio
@pytest.mark.parametrize('live',[True,False])
async def test_campaign_guard_canonical_sql_and_publication(projects,ready,monkeypatch,live):
    import asyncpg
    from core.campaign_lifecycle import CAMPAIGN_LIVE_SQL
    a,_=projects
    monkeypatch.setenv('POSTGRES_URL','postgresql://synthetic/morgoth_test')
    held=[];queries=[]
    @asynccontextmanager
    async def transaction():
        held.append(True)
        try: yield
        finally: held.pop()
    conn=SimpleNamespace(transaction=transaction,execute=AsyncMock(),close=AsyncMock())
    async def fetch(query):
        queries.append(query)
        assert CAMPAIGN_LIVE_SQL in query
        assert "ends_at > CURRENT_TIMESTAMP" in query
        assert "ended_at IS NULL" in query
        return live
    conn.fetchval=fetch
    connect=AsyncMock(return_value=conn)
    monkeypatch.setattr(asyncpg,'connect',connect)
    publish=P._publish
    def checked(*args):
        assert held
        conn.execute.assert_awaited_once_with('LOCK TABLE campaigns IN SHARE MODE')
        publish(*args)
    monkeypatch.setattr(P,'_publish',checked)
    if live:
        with pytest.raises(P.ProfileError,match='research-live'): await P.switch_profile(a,'claude')
        assert not P.state_path(a).exists()
    else:
        await P.switch_profile(a,'claude')
        assert P.current_profile(a).id=='claude'
    assert connect.call_args.kwargs['server_settings']['search_path']==a.postgres_schema
    assert not held and queries
    conn.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_guard_unavailable_refuses(projects,ready,monkeypatch):
    a,_=projects
    monkeypatch.delenv('POSTGRES_URL',raising=False)
    with pytest.raises(P.ProfileError,match='guard unavailable'): await P.switch_profile(a,'claude')
    assert not P.state_path(a).exists()


@pytest.mark.asyncio
async def test_same_profile_noop_during_campaign(projects,ready,no_campaign,monkeypatch):
    a,_=projects
    await P.switch_profile(a,'claude')
    guard=MagicMock(side_effect=AssertionError('must not check'))
    monkeypatch.setattr(P,'campaign_guard',guard)
    before=P.state_path(a).stat().st_mtime_ns
    await P.switch_profile(a,'claude')
    assert P.state_path(a).stat().st_mtime_ns==before
    guard.assert_not_called()


@pytest.mark.asyncio
async def test_reflect_and_shadow_resolve_at_call(projects,ready,no_campaign,monkeypatch):
    from self_modify import reflect_llm
    from core.llm.providers import get_provider
    a,_=projects
    await P.switch_profile(a,'claude')
    assert reflect_llm.resolve_provider('codex-cli')=='claude-cli'
    complete=AsyncMock(return_value='synthetic')
    monkeypatch.setattr('core.llm.providers.get_provider',lambda *a,**kw:SimpleNamespace(complete=complete))
    for task in ('reflect','shadow'):
        result,_=await reflect_llm.reflect_chat('synthetic',MagicMock(),'codex-cli',task=task)
        assert result=='synthetic'
    assert complete.await_count==2


@pytest.mark.asyncio
async def test_models_cli_no_database_initialization(projects,ready,no_campaign,monkeypatch,capsys):
    from self_modify import cli
    initialize=AsyncMock(side_effect=AssertionError('must not initialize'))
    monkeypatch.setattr(cli.PersistentMemory,'initialize',initialize)
    assert await cli._main(['models','use','claude'])==0
    assert await cli._main(['models'])==0
    assert 'profile=claude' in capsys.readouterr().out
    assert await cli._main(['models','use','codex'])==2
    initialize.assert_not_called()


@pytest.mark.asyncio
async def test_new_profile_is_data_not_switch_branch(projects,ready,no_campaign,monkeypatch):
    from types import MappingProxyType
    a,_=projects
    custom=P.LLMProfile('synthetic_local',{task:'ollama:default' for task in tasks.all_tasks()})
    monkeypatch.setattr(P,'PROFILES',MappingProxyType({**P.PROFILES,custom.id:custom}))
    await P.switch_profile(a,custom.id)
    assert all(P.resolve_task_route(a,task)==('ollama','default') for task in tasks.all_tasks())


def test_unavailable_never_recommended(projects,ready):
    a,_=projects
    ready.claude_cli.status='unavailable'
    ready.ollama.status='unavailable'
    assert E.suggest_routing(ready)==[]
    assert P.recommended_profile(a,ready) is None


@pytest.mark.parametrize('kind',['duplicate','oversize','symlink','fifo'])
def test_unsafe_state_fail_closed(projects,kind,tmp_path):
    a,_=projects
    a.runtime_dir.mkdir(parents=True)
    path=P.state_path(a)
    if kind=='duplicate': path.write_text('{"schema_version":1,"profile":"claude","profile":"legacy"}')
    if kind=='oversize': path.write_text(' '*4097+'{}')
    if kind=='symlink': path.symlink_to(tmp_path/'absent')
    if kind=='fifo': os.mkfifo(path)
    with pytest.raises(P.ProfileError): P.current_profile(a)


@pytest.mark.asyncio
async def test_concurrent_switch_refuses_without_partial_state(projects,ready,no_campaign):
    import fcntl
    a,_=projects
    a.runtime_dir.mkdir(parents=True)
    with open(a.runtime_dir/'.llm-profile.lock','w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        with pytest.raises(P.ProfileError,match='in progress'):
            await P.switch_profile(a,'claude')
    assert P.current_profile(a).id=='legacy'


def test_project_override_preserved_in_legacy(projects):
    a,_=projects
    customized=Project(**{**a.model_dump(),'llm_overrides':{'thesis':'claude-cli:custom'}})
    assert P.resolve_task_route(customized,'thesis')==('claude-cli','custom')


@pytest.mark.asyncio
async def test_unavailable_switch_leaves_state_absent(projects,ready,no_campaign):
    a,_=projects
    ready.claude_cli.status='unavailable'
    with pytest.raises(P.ProfileError,match='UNAVAILABLE'): await P.switch_profile(a,'claude')
    assert not a.runtime_dir.exists()


@pytest.mark.asyncio
async def test_blocked_profile_noop_cannot_claim_readiness(projects):
    a,_=projects
    a.runtime_dir.mkdir(parents=True)
    P.state_path(a).write_text('{"schema_version":1,"profile":"codex"}')
    before=P.state_path(a).read_bytes()
    with pytest.raises(P.ProfileError,match='NOT_QUALIFIED'): await P.switch_profile(a,'codex')
    assert P.state_path(a).read_bytes()==before
