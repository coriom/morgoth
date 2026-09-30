"""Non-crypto Domain proof; all tools and storage are synthetic."""
from __future__ import annotations

import ast
import re
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml

from core import domain as domain_mod
from core import metric_recorder
from core.project import ProjectConfig
from analysis import scorer_registry
from analysis.scorer_registry import resolve_scorer


PACK = """\
name: neutral
rail: {tools: [synthetic_probe]}
entities:
  alpha: [alpha]
  beta: [beta]
semantic_classes:
  fast: [rapid]
  slow: [gradual]
semantic_windows_hours:
  default: 4
  fast: 1
  slow: 12
metric_names:
  synthetic_metric: synthetic_metric
metric_field_map:
  value: synthetic_metric
metric_collections:
  synthetic_probe:
    source: synthetic_source
    interval_secs: 120
    fields: [value]
    args: {}
"""


@pytest.fixture
def neutral_pack(tmp_path, monkeypatch):
    from core import tool_rail
    installed = tool_rail.installed_catalog()
    monkeypatch.setattr(tool_rail, "installed_catalog", lambda: {
        **installed, "synthetic_probe": tool_rail.ToolKind(False, True, True),
    })
    directory = tmp_path / "domains" / "neutral"
    directory.mkdir(parents=True)
    (directory / "domain.yaml").write_text(PACK, encoding="utf-8")
    monkeypatch.setattr(domain_mod, "_DOMAINS_ROOT", tmp_path / "domains")
    return domain_mod._load("neutral")


def test_project_and_neutral_semantics(neutral_pack, tmp_path):
    project = ProjectConfig(id="neutral", name="Neutral test", domain=neutral_pack.name,
                            postgres_schema="neutral_test", chroma_prefix="neutral_test_",
                            vault_dir=tmp_path / "vault", runtime_dir=tmp_path / "run")
    assert project.domain == "neutral"
    assert project.postgres_schema != "public" and project.chroma_prefix
    assert domain_mod.resolve_subject_entity("alpha rapid", neutral_pack) == "alpha"
    assert domain_mod.resolve_subject_entity("beta gradual", neutral_pack) == "beta"
    assert domain_mod.resolve_subject_entity("unlisted", neutral_pack) is None
    assert domain_mod.subject_semantic_class("alpha rapid", neutral_pack) == "fast"
    assert domain_mod.subject_semantic_class("beta gradual", neutral_pack) == "slow"
    assert domain_mod.semantic_window_hours("fast", neutral_pack) == 1
    assert domain_mod.semantic_window_hours("slow", neutral_pack) == 12
    assert domain_mod.semantic_window_hours("default", neutral_pack) == 4
    with pytest.raises(domain_mod.DomainPackError):
        domain_mod.semantic_window_hours("price", neutral_pack)
    assert resolve_scorer(neutral_pack, "descriptive") is None
    assert neutral_pack.prompt_bootstrap_snippet == ""
    assert neutral_pack.test_bootstrap_tool_defaults == {}
    assert neutral_pack.bootstrap_recurring_task == {}


def test_no_scorer_does_not_import_crypto(neutral_pack, monkeypatch):
    monkeypatch.setattr(scorer_registry, "import_module", lambda name: pytest.fail(name))
    assert resolve_scorer(neutral_pack, "directional") is None
    assert resolve_scorer(neutral_pack, "campaign_quality") is None


def test_noncrypto_project_process_uses_only_declared_semantics(tmp_path):
    """Fresh process selects a synthetic Project without inherited secrets."""
    root = Path(__file__).resolve().parents[1]
    packs = tmp_path / "packs"
    (packs / "neutral").mkdir(parents=True)
    (packs / "neutral" / "domain.yaml").write_text(PACK, encoding="utf-8")
    (packs / "crypto").mkdir()
    shutil.copyfile(root / "domains/crypto/domain.yaml", packs / "crypto/domain.yaml")
    manifest = dict(id="neutral", name="Neutral test", domain="neutral",
                    postgres_schema="neutral_test", chroma_prefix="neutral_test_",
                    vault_dir=str(tmp_path / "vault"), runtime_dir=str(tmp_path / "run"))
    path = tmp_path / "projects/neutral/project.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(yaml.safe_dump(manifest), encoding="utf-8")
    code = """\
import sys
from pathlib import Path
from core import domain
domain._DOMAINS_ROOT = Path(sys.argv[1])
from core import tool_rail
installed = tool_rail.installed_catalog()
tool_rail.installed_catalog = lambda: {**installed, 'synthetic_probe': tool_rail.ToolKind(False, True, True)}
from core.project import current_project, current_namespace
from core.storage_namespace import collection_names
p = current_project()
d = domain.current_domain()
from core import metric_recorder, contradictions
from analysis.scorer_registry import resolve_scorer
assert p.domain == d.name == 'neutral'
assert p.postgres_schema == 'neutral_test'
assert current_namespace() is p
assert all(name.startswith('neutral_test_') for name in collection_names(p.chroma_prefix))
assert p.vault_dir.name == 'vault' and p.runtime_dir.name == 'run'
assert domain.resolve_subject_entity('alpha rapid', d) == 'alpha'
assert contradictions.window_for('alpha rapid', 'beta gradual') == 1
assert tuple(metric_recorder._COLLECTIONS) == ('synthetic_probe',)
assert resolve_scorer(d, 'descriptive') is None
assert not any(n in sys.modules for n in ('analysis.thesis_backtest', 'analysis.thesis_backtest_descriptive', 'analysis.campaign_quality'))
print('neutral-project-ok')
"""
    env = {"HOME": str(tmp_path), "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8",
           "PYTHONPATH": str(root), "MORGOTH_HOME": str(tmp_path),
           "MORGOTH_PROJECT": "neutral"}
    result = subprocess.run([sys.executable, "-c", code, str(packs)], cwd=tmp_path,
                            env=env, capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode == 0, result.stderr[-1000:]
    assert result.stdout.strip() == "neutral-project-ok"


@pytest.mark.asyncio
async def test_neutral_metric_schedule_and_recording(neutral_pack):
    spec = neutral_pack.metric_collections["synthetic_probe"]
    assert (spec.source, spec.interval_secs, spec.fields, spec.args) == (
        "synthetic_source", 120, ("value",), {})
    schedule = metric_recorder.ScheduleState(neutral_pack.metric_collections)
    assert schedule.due_tools(1000) == ("synthetic_probe",)
    schedule.mark_snapshot(1000)
    assert schedule.due_tools(1119) == ()
    assert schedule.due_tools(1120) == ("synthetic_probe",)
    router = SimpleNamespace(execute_tool=AsyncMock(return_value={"success": True, "result": {"value": 7}}))
    memory = SimpleNamespace(record_metric_sample=AsyncMock())
    assert await metric_recorder.snapshot_once(memory, router, domain=neutral_pack) == 1
    router.execute_tool.assert_awaited_once_with("synthetic_probe", {})
    assert memory.record_metric_sample.await_args.args[0:2] == ("synthetic_metric", 7.0)
    assert memory.record_metric_sample.await_args.args[3] == "synthetic_source"


def test_collectors_keep_independent_intervals(neutral_pack):
    second = domain_mod.MetricCollection("other_source", 300, ("other_value",), {})
    specs = {**neutral_pack.metric_collections, "other_probe": second}
    schedule = metric_recorder.ScheduleState(specs)
    assert schedule.due_tools(1000) == ("synthetic_probe", "other_probe")
    schedule.mark_snapshot(1000)
    assert schedule.due_tools(1120) == ("synthetic_probe",)
    schedule.mark_snapshot(1120, tools=("synthetic_probe",))
    assert schedule.due_tools(1300) == ("synthetic_probe", "other_probe")
    assert metric_recorder.snapshot_interval_secs(neutral_pack) == 120
    no_metrics = replace(neutral_pack, metric_collections={})
    assert metric_recorder.snapshot_interval_secs(no_metrics) == 0


@pytest.mark.parametrize("replacement", [
    "semantic_windows_hours:\n  fast: 1",  # no declared default
    "semantic_windows_hours:\n  default: -1\n  fast: 1\n  slow: 12",
    "interval_secs: 10",  # recorder cadence below safe minimum
    "fields: [missing]",  # no logical metric mapping
    "source: ''",  # unnamed provenance
    "scorers:\n  descriptive: unknown_scorer",
])
def test_malformed_neutral_pack_fails_closed(tmp_path, monkeypatch, replacement):
    from core import tool_rail
    installed = tool_rail.installed_catalog()
    monkeypatch.setattr(tool_rail, "installed_catalog", lambda: {
        **installed, "synthetic_probe": tool_rail.ToolKind(False, True, True),
    })
    text = PACK
    if replacement.startswith("semantic_windows_hours:"):
        text = re.sub(r"semantic_windows_hours:\n  default: 4\n  fast: 1\n  slow: 12", replacement, text)
    elif replacement.startswith("interval_secs"):
        text = text.replace("interval_secs: 120", replacement)
    elif replacement.startswith("fields:"):
        text = text.replace("fields: [value]", replacement)
    elif replacement.startswith("source:"):
        text = text.replace("source: synthetic_source", replacement)
    else:
        text += replacement + "\n"
    directory = tmp_path / "domains" / "neutral"
    directory.mkdir(parents=True)
    (directory / "domain.yaml").write_text(text, encoding="utf-8")
    monkeypatch.setattr(domain_mod, "_DOMAINS_ROOT", tmp_path / "domains")
    with pytest.raises(domain_mod.DomainPackError):
        domain_mod._load("neutral")


def test_generic_runtime_has_no_executable_domain_literals():
    """Curated orchestration modules must not grow market-name decisions."""
    root = Path(__file__).resolve().parents[1]
    modules = ("core/contradictions.py", "core/metric_recorder.py",
               "core/objective_gen_context.py", "core/campaign.py")
    banned = re.compile(r"\b(?:BTC|ETH|Binance|Deribit)\b")
    for name in modules:
        tree = ast.parse((root / name).read_text(encoding="utf-8"))
        docs = {id(node.body[0].value) for node in ast.walk(tree)
                if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                and node.body and isinstance(node.body[0], ast.Expr)
                and isinstance(node.body[0].value, ast.Constant)
                and isinstance(node.body[0].value.value, str)}
        bad = [(node.lineno, node.value) for node in ast.walk(tree)
               if isinstance(node, ast.Constant) and isinstance(node.value, str)
               and id(node) not in docs and banned.search(node.value)]
        assert not bad, (name, bad)
