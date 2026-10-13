"""No-inference task-consumer checks after operator-reported text transport success.

These are deterministic parser and routing assertions, not live Codex task
acceptance. In particular a model's JSON claim is not source evidence.
"""

import json

from core.brain import Brain
from core.llm import codex_cli, fallback, tasks
from core.numeric_fidelity import check_thesis
from self_modify import reflect, scout, shadow


def test_task_routes_preserve_local_chat_and_blocked_external_workloads() -> None:
    assert tasks.DEFAULTS[tasks.CHAT] == "ollama:default"
    for task in (tasks.THESIS, tasks.SYNTHESIS, tasks.REFLECT, tasks.SHADOW, tasks.SCOUT):
        assert tasks.DEFAULTS[task] == "codex-cli:default"
    assert codex_cli.SAFE_FOR_WORKLOADS is False
    assert fallback._next_provider_down("codex-cli") == "ollama"
    assert fallback._next_provider_down("ollama") is None


def test_thesis_parser_and_current_source_gate_are_separate() -> None:
    source = ['TOOL RESULTS:\n- fixture_source: {"backlog": 10}']
    base = {"subject": "fixture backlog", "claim": "increasing"}
    grounded = Brain._parse_thesis_json(json.dumps([{**base, "evidence": [
        {"source": "fixture_source", "detail": "Backlog 10 items"}]}]))
    invented = Brain._parse_thesis_json(json.dumps([{**base, "evidence": [
        {"source": "fixture_source", "detail": "Backlog 999 items"}]}]))
    assert len(grounded) == len(invented) == 1  # JSON parsing is not grounding.
    assert check_thesis(grounded[0], source).action == "pass"
    assert check_thesis(invented[0], source).action == "drop"
    assert check_thesis(grounded[0], []).action == "drop"


def test_thesis_malformed_and_abstention_are_nonclaims() -> None:
    assert Brain._parse_thesis_json("not JSON") == []
    assert Brain._parse_thesis_json("[]") == []
    assert Brain._parse_thesis_json(json.dumps([{
        "subject": "fixture backlog", "claim": "unclear", "evidence": [
            {"source": "fixture_source", "detail": "Backlog 10 items"}],
    }])) == []


def test_reflect_proposal_requires_shape_after_json_parse() -> None:
    assert reflect._parse_spec("NONE") is None
    assert reflect._parse_spec("unstructured output") is None
    parsed = reflect._parse_spec('{"tool_name":"bad-name"}')
    assert parsed is not None
    assert reflect._spec_is_well_formed(parsed) is not None


def test_shadow_unknown_or_malformed_verdict_fails_closed() -> None:
    assert shadow.parse_verdict("not JSON")["verdict"] == "ERROR"
    assert shadow.parse_verdict('{"verdict":"AUTOMATIC_APPROVAL"}')["verdict"] == "ERROR"
    warning = shadow.parse_verdict('{"verdict":"FLAG","axes":{"api_liveness":"UNKNOWN"}}')
    assert warning["verdict"] == "FLAG"
    assert warning["axes"]["api_liveness"] == "WARN"


def test_scout_catalog_is_data_not_a_tool_grant() -> None:
    catalog = """### Government
API | Description | Auth | HTTPS | CORS |
|:---|:---|:---|:---|:---|
| [Fixture](https://fixture.example/docs) | test fixture | No | Yes | Yes |
"""
    rows = scout.parse_catalog(catalog)
    assert len(rows) == 1
    assert rows[0].name == "Fixture"
    assert not hasattr(rows[0], "execute")
