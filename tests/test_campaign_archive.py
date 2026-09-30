"""Owned evidence only, no production state, network, Chroma or inference."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from analysis import campaign_archive as A

CID = '11111111-1111-1111-1111-111111111111'
OTHER = '22222222-2222-2222-2222-222222222222'
OID = '33333333-3333-3333-3333-333333333333'
TID = '44444444-4444-4444-4444-444444444444'
NOW = datetime(2026, 9, 30, tzinfo=timezone.utc)


def dataset():
    return {
        'campaigns': [{'campaign_id': CID, 'status': 'active', 'subject': 'synthetic research', 'ends_at': '2026-10-01T00:00:00+00:00', 'ended_at': None}, {'campaign_id': OTHER}],
        'objectives': [{'objective_id': OID, 'campaign_id': CID, 'status': 'in_progress', 'cycle_count': 1, 'evidence': [{'type': 'cycle_payload', 'cycle': 1, 'tool_results': [{'tool': 'synthetic_tool', 'success': True, 'result': {'value': 42}}]}]}, {'objective_id': OTHER, 'campaign_id': OTHER, 'evidence': [{'secret': 'synthetic-credential-do-not-export'}]}],
        'theses': [{'thesis_id': TID, 'objective_id': OID, 'claim': 'synthetic', 'code_version': 'abc1234'}, {'thesis_id': OTHER, 'objective_id': OTHER, 'claim': 'unrelated'}],
        'campaign_data_gaps': [{'campaign_id': CID, 'phrase': 'synthetic gap', 'count': 1}, {'campaign_id': OTHER, 'phrase': 'unrelated'}],
        'numeric_fidelity_events': [{'event_id': 'n1', 'objective_id': OID}, {'event_id': 'n2', 'objective_id': OTHER}],
        'field_confusion_events': [{'event_id': 'f1', 'thesis_id': TID}, {'event_id': 'f2', 'thesis_id': OTHER}],
        'contradictions': [{'contradiction_id': 'c1', 'thesis_id_a': TID, 'thesis_id_b': TID}, {'contradiction_id': 'cross-boundary', 'thesis_id_a': TID, 'thesis_id_b': OTHER}],
    }


class Reader:
    def __init__(self, data=None):
        self.data = deepcopy(data if data is not None else dataset())
        self.queries = []
        self.transactions = []
        self.close = AsyncMock()
    def transaction(self, **options):
        self.transactions.append(options)
        assert options == {'isolation': 'repeatable_read', 'readonly': True}
        return self
    async def __aenter__(self): return self
    async def __aexit__(self, *args): return False
    async def execute(self, *args): raise AssertionError('archive must not issue writes/DDL')
    async def fetch(self, sql, *args):
        assert sql.startswith('SELECT ')
        self.queries.append((sql, args))
        if 'information_schema.columns' in sql:
            return [{'table_name': t, 'column_name': k} for t, rows in self.data.items() for k in sorted({k for r in rows for k in r})]
        table = sql.split('"."')[1].split('"')[0]
        key = args[0]
        if table in ('campaigns', 'objectives', 'campaign_data_gaps'):
            rows = [r for r in self.data[table] if r['campaign_id'] == key]
        elif table in ('theses', 'numeric_fidelity_events'):
            rows = [r for r in self.data[table] if r['objective_id'] in key]
        elif table == 'field_confusion_events':
            rows = [r for r in self.data[table] if r['thesis_id'] in key]
        else:
            rows = [r for r in self.data[table] if r['thesis_id_a'] in key and r['thesis_id_b'] in key]
        return [{'record': json.dumps(r)} for r in rows]


async def archive(reader=None, **kwargs):
    return await A.read_campaign_archive(reader or Reader(), CID, schema='project_alpha', project_id='alpha', exporter_version='exporter-sha', exported_at=NOW, **kwargs)


@pytest.mark.asyncio
async def test_owned_records_exact_and_read_only():
    reader = Reader(); result = await archive(reader)
    assert result['campaign'] == dataset()['campaigns'][0]
    assert reader.transactions == [{'isolation': 'repeatable_read', 'readonly': True}]
    for key in dataset():
        if key != 'campaigns': assert len(result['records'][key]) == 1
    assert result['records']['cycle_payloads'][0]['objective_id'] == OID
    assert result['code_versions_in_records'] == ['abc1234']
    assert result['record_counts']['cycle_payloads'] == 1
    assert result['validity_metadata']['recorded_tool_successes'] == {'synthetic_tool': 1}
    assert 'unrelated' not in json.dumps(result)
    assert 'synthetic-credential-do-not-export' not in json.dumps(result)
    assert all('"project_alpha".' in sql for sql, _ in reader.queries[1:])


@pytest.mark.asyncio
async def test_deterministic_content_and_full_artifact():
    data = dataset()
    data['objectives'].append({'objective_id': 'z', 'campaign_id': CID, 'cycle_count': 0})
    first = await archive(Reader(data))
    for rows in data.values(): rows.reverse()
    second = await archive(Reader(data))
    assert A._canonical(first) == A._canonical(second)
    second['exported_at'] = 'a later export timestamp'
    assert {k:v for k,v in first.items() if k!='exported_at'} == {k:v for k,v in second.items() if k!='exported_at'}


@pytest.mark.asyncio
async def test_missing_optional_classes_explicit_and_null_versions_unknown():
    data = dataset(); del data['numeric_fidelity_events']; del data['field_confusion_events']
    data['theses'][0]['code_version'] = None
    result = await archive(Reader(data))
    missing = {x['category'] for x in result['unreconstructed_categories']}
    assert {'numeric_fidelity_events', 'field_confusion_events', 'complete_code_provenance', 'source_snapshots', 'chroma_findings', 'complete_cycle_timeline', 'raw_logs'} <= missing
    assert result['code_versions_in_records'] == []


@pytest.mark.asyncio
async def test_unknown_campaign_and_missing_project_fail_cleanly():
    data = dataset(); data['campaigns'] = [data['campaigns'][1]]
    with pytest.raises(A.ArchiveError, match='not found'): await archive(Reader(data))
    with pytest.raises(A.ArchiveError, match='required'): await archive(Reader({}))


@pytest.mark.parametrize('secret', [{'api_key': 'synthetic-not-a-real-key'}, 'password=synthetic-only', 'Bearer SYNTHETIC_ONLY', 'postgresql://user:synthetic@host/db', '{"api_key": "synthetic-encoded-only"}'])
@pytest.mark.asyncio
async def test_sensitive_owned_content_refuses_without_value_in_error(secret):
    data = dataset(); data['objectives'][0]['description'] = secret
    with pytest.raises(A.ArchiveError, match='suspected sensitive content') as error:
        await archive(Reader(data))
    assert 'synthetic' not in str(error.value)


@pytest.mark.asyncio
async def test_partial_annotation_never_changes_persisted_campaign():
    result = await archive(validity='PARTIAL')
    assert result['campaign']['status'] == 'active'
    assert result['validity_metadata']['classification_origin'] == 'operator_annotation'
    assert result['validity_metadata']['forensic_only']
    assert not result['validity_metadata']['continuity_proven']


@pytest.mark.asyncio
async def test_atomic_permissions_hash_and_no_overwrite(tmp_path):
    result = await archive(); dest = tmp_path/'campaign.json'
    digest = A.write_campaign_archive(result, dest)
    assert digest == hashlib.sha256(dest.read_bytes()).hexdigest()
    assert Path(str(dest)+'.sha256').read_text() == f'{digest}  campaign.json\n'
    assert dest.stat().st_mode & 0o777 == 0o600
    assert Path(str(dest)+'.sha256').stat().st_mode & 0o777 == 0o600
    assert not list(tmp_path.glob('.campaign-archive-*'))
    before = dest.read_bytes()
    with pytest.raises(A.ArchiveError, match='already exists'): A.write_campaign_archive(result, dest)
    assert dest.read_bytes() == before


@pytest.mark.asyncio
async def test_dangling_symlink_and_existing_sidecar_refuse(tmp_path):
    result = await archive(); dest = tmp_path/'campaign.json'
    dest.symlink_to(tmp_path/'missing')
    with pytest.raises(A.ArchiveError): A.write_campaign_archive(result, dest)
    dest.unlink(); Path(str(dest)+'.sha256').write_text('existing')
    with pytest.raises(A.ArchiveError): A.write_campaign_archive(result, dest)
    assert not dest.exists()


@pytest.mark.asyncio
async def test_publish_failure_is_closed_and_temporary_files_cleaned(tmp_path, monkeypatch):
    result = await archive(); dest = tmp_path/'campaign.json'
    real = A._rename_new
    def interrupted(source, target):
        if target == dest: raise OSError('synthetic interruption')
        real(source, target)
    monkeypatch.setattr(A, '_rename_new', interrupted)
    with pytest.raises(OSError): A.write_campaign_archive(result, dest)
    assert not dest.exists() and Path(str(dest)+'.sha256').exists()
    assert not list(tmp_path.glob('.campaign-archive-*'))
    with pytest.raises(A.ArchiveError): A.write_campaign_archive(result, dest)


@pytest.mark.asyncio
async def test_cli_dispatch_never_initializes_runtime(monkeypatch, tmp_path, capsys):
    from scripts import campaign_cli as cli
    monkeypatch.setattr(cli, 'load_config', AsyncMock(side_effect=AssertionError('no runtime config')))
    monkeypatch.setattr(cli.PersistentMemory, 'initialize', AsyncMock(side_effect=AssertionError('no DDL')))
    command = AsyncMock(return_value='a'*64); monkeypatch.setattr(A, 'archive_command', command)
    assert await cli._main(['archive', CID, '--output', str(tmp_path/'out.json'), '--validity', 'PARTIAL']) == 0
    assert command.await_args.kwargs['validity'] == 'PARTIAL'
    command.side_effect = RuntimeError('synthetic-credential-do-not-print')
    assert await cli._main(['archive', CID, '--output', str(tmp_path/'other.json')]) == 1
    assert 'synthetic-credential' not in capsys.readouterr().err


@pytest.mark.asyncio
async def test_archive_connection_defaults_readonly_and_never_loads_chroma(monkeypatch, tmp_path):
    import core.project as project
    reader = Reader()
    connect = AsyncMock(return_value=reader); monkeypatch.setattr(A.asyncpg, 'connect', connect)
    monkeypatch.setattr(project, 'current_namespace', lambda: SimpleNamespace(is_legacy=False, postgres_schema='project_alpha', id='alpha'))
    monkeypatch.setenv('POSTGRES_URL', 'postgresql://synthetic/synthetic_test')
    await A.archive_command(CID, tmp_path/'archive.json')
    settings = connect.await_args.kwargs['server_settings']
    assert settings['default_transaction_read_only'] == 'on'
    assert settings['search_path'] == 'project_alpha'
    reader.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_concurrent_writers_cannot_mix_or_overwrite_artifacts(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    result = await archive(); dest = tmp_path/'race.json'
    def publish(index):
        item = deepcopy(result); item['exported_at'] = str(index)
        try: return A.write_campaign_archive(item, dest)
        except (OSError, A.ArchiveError): return None
    with ThreadPoolExecutor(max_workers=4) as workers:
        outcomes = list(workers.map(publish, range(8)))
    successful = [x for x in outcomes if x]
    assert len(successful) == 1
    assert hashlib.sha256(dest.read_bytes()).hexdigest() == successful[0]
    assert Path(str(dest)+'.sha256').read_text().startswith(successful[0]+'  ')


@pytest.mark.asyncio
async def test_fsync_failure_never_publishes_unflushed_artifact(tmp_path, monkeypatch):
    result = await archive(); dest = tmp_path/'out.json'
    def failed(_): raise OSError('synthetic fsync failure')
    monkeypatch.setattr(A.os, 'fsync', failed)
    with pytest.raises(OSError): A.write_campaign_archive(result, dest)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_unsafe_namespace_rejected_before_query():
    reader = Reader()
    with pytest.raises(ValueError):
        await A.read_campaign_archive(reader, CID, schema='bad";drop', project_id='bad', exporter_version='synthetic')
    assert reader.queries == []
