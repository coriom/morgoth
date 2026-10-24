"""CMQ.4 preparation, accounting and real confined-browser protocol tests."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agents import generalist_mission_qualification_cmq4 as campaign
from agents.generalist_browser_lab import BrowserRefusal, LexicalObservationPolicy, run_isolated_policy


ROOT = Path(__file__).resolve().parents[1]
BROWSER = campaign.operator.BROWSER
REQUIRES_BROWSER = pytest.mark.skipif(
    not BROWSER.joinpath("chrome-headless-shell").is_file(),
    reason="reviewed Chromium unavailable")


def _sample() -> list[dict[str, object]]:
    return [{"GPU_FREE_MIB": 2048, "HOST_AVAILABLE_MIB": 4096,
             "SWAP_FREE_MIB": 1024, "MEMORY_PRESSURE_AVG10_PCT": 0.0,
             "MEMORY_PRESSURE_FULL_AVG10_PCT": 0.0,
             "CGROUP_MEMORY_PRESSURE_FULL_AVG10_PCT": 0.0,
             "SERVICE_CGROUP_OOM_COUNT": 0, "SERVICE_CGROUP_OOM_KILL_COUNT": 0}]


def test_scheduled_matrix_and_budget_are_fixed() -> None:
    cells = campaign._cells()
    assert len(cells) == 8
    assert set(cells) == {f"{role}:{fixture}" for role in ("QWEN", "COMPETITOR")
                          for fixture in ("choice", "heldout", "catalog", "relayout")}
    assert campaign.MAX_REQUESTS == 2 + 2 * 3 * 6 == 38
    assert campaign.MAX_PER_MODEL == 19
    assert [task.fixture for task in campaign.SCENARIOS] == ["heldout", "catalog", "relayout"]
    assert [(task.target, task.expected) for task in campaign.SCENARIOS] == [
        ("SKU-44", "12 units"), ("BK-17", "The Atlas of Rivers"),
        ("SKU-44", "12 units")]


def test_attempt_ledger_refuses_rerun_and_keeps_only_bounded_facts(tmp_path: Path) -> None:
    path = tmp_path / "attempt.json"
    report = {"CELLS": campaign._cells(), "REQUESTS_DISPATCHED": 0}
    campaign._write_ledger(path, report, first=True)
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        campaign._write_ledger(path, report, first=True)
    report["CELLS"]["QWEN:choice"] = {"STATUS": "RECORDED", "REQUESTS": 1}
    report["REQUESTS_DISPATCHED"] = 1
    campaign._write_ledger(path, report)
    assert json.loads(path.read_text())["REQUESTS_DISPATCHED"] == 1


def test_duplicate_key_and_extra_field_fail_without_disclosure() -> None:
    request = campaign.FrozenRequest((), {"format": {"properties": {
        "revision": {"enum": [3]}, "choice_id": {"enum": ["c1", "stop"]}}}},
        "0" * 64, "1" * 64, {})
    for content, code in [
        ('{"revision":3,"choice_id":"c1","choice_id":"c1"}', "MODEL_DUPLICATE_JSON_KEY"),
        ('{"revision":3,"choice_id":"c1","private":"SECRET"}', "MODEL_ACTION_SCHEMA_INVALID"),
        ('```json\n{"revision":3,"choice_id":"c1"}\n```', "MODEL_INVALID_JSON"),
        ('{"revision":3,"choice_id":"c9"}', "MODEL_CHOICE_INVALID"),
    ]:
        response = SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=[]),
                                   done=True, done_reason="stop")
        with pytest.raises(BrowserRefusal) as error:
            campaign._decode_choice(response, request)
        assert error.value.code == code
        assert "SECRET" not in str(error.value)


@pytest.mark.asyncio
@REQUIRES_BROWSER
async def test_frozen_request_is_from_reviewed_rendered_post_fill_state() -> None:
    frozen = await campaign._frozen_post_fill()
    assert frozen.summary["REVIEWED_FIXTURE_DIGEST_VERIFIED"] is True
    assert frozen.summary["TARGET_IN_VISIBLE_INPUT"] is True
    assert frozen.summary["REMAINING_MODEL_CALLS"] == 4
    assert frozen.summary["CHOICE_SCHEMA_MATCHES_CATALOG"] is True
    assert {"CONTROL": "C04", "KIND": "CLICK"} in frozen.summary["CANDIDATES"]
    assert frozen.summary["VERIFIED_EFFECTS"][-1] == {
        "CONTROL": "C03", "KIND": "FILL", "EFFECT": "UI_CHANGE_OBSERVED"}
    assert frozen.kwargs["tools"] is None and frozen.kwargs["stream"] is False
    assert frozen.kwargs["options"] == {"temperature": 0, "num_predict": 384}
    assert frozen.source_digest == campaign.SyntheticTracePolicy.from_checkout(ROOT, "heldout").digest
    assert len(frozen.digest) == 64


@pytest.mark.asyncio
@REQUIRES_BROWSER
async def test_scenario_preflights_and_fresh_scripted_complete_missions() -> None:
    await campaign._browser_preflight()
    for task in campaign.SCENARIOS:
        result = await run_isolated_policy(
            checkout=ROOT, browser_dir=BROWSER, fixture=task.fixture,
            objective=task.objective, target=task.target, expected=task.expected,
            source=LexicalObservationPolicy(), max_model_calls=6,
            synthetic_trace=True)
        assert result["TASK_ACCEPTANCE_PASSED"] is True
        assert result["ACTION_COUNT"] <= 6


@pytest.mark.asyncio
async def test_pair_reuses_exact_request_and_stage_a_resource_assessment(monkeypatch) -> None:
    seen = []

    class Client:
        def __init__(self, *_args, **_kwargs):
            pass

        async def chat(self, messages, **kwargs):
            seen.append((tuple(item.model_dump_json() for item in messages),
                         {key: value for key, value in kwargs.items() if key != "model"}))
            return SimpleNamespace(message=SimpleNamespace(
                content='{"revision":3,"choice_id":"c1"}', tool_calls=[]),
                done=True, done_reason="stop", load_duration=1000000000,
                total_duration=2000000000, prompt_eval_count=40, eval_count=10)

        async def close(self):
            pass

    async def samples(*_args):
        return _sample()

    monkeypatch.setattr(campaign, "OllamaLLMClient", Client)
    monkeypatch.setattr(campaign.r2, "sample_during_request", samples)
    class Message:
        def __init__(self, content):
            self.content = content
        def model_dump_json(self):
            return json.dumps({"content": self.content})
    payload = {"observation": {"actions": [{"id": "c1", "name": "Search stock", "kind": "click"}]}}
    frozen = campaign.FrozenRequest(
        (Message("system"), Message(json.dumps(payload))),
        {"tools": None, "stream": False, "options": {"temperature": 0, "num_predict": 384},
         "format": {"properties": {"revision": {"enum": [3]},
                                   "choice_id": {"enum": ["c1"]}}}},
        "2" * 64, campaign.SyntheticTracePolicy.from_checkout(ROOT, "heldout").digest, {})
    for name in ("qwen3:4b-instruct", "other:small"):
        result = await campaign._choice_probe(
            campaign.Candidate(name, "a" * 64, "Q4_K_M", 2500), frozen,
            {"MODEL_RESIDENCY": "NOT_LOADED"}, SimpleNamespace(ollama_base_url="http://127.0.0.1:11434"))
        assert result["TECHNICALLY_QUALIFIED"] is True
        assert result["CHOICE_USEFUL_AT_FROZEN_STATE"] is True
        assert result["MODEL_CALL_COUNT"] == result["MODEL_RESPONSE_COUNT"] == 1
        assert result["INPUT_SHA256"] == frozen.digest
    assert seen[0] == seen[1]


@pytest.mark.asyncio
async def test_preparation_path_never_calls_inference(monkeypatch) -> None:
    monkeypatch.setattr(campaign.compact, "verify_checkout", lambda *_: None)
    monkeypatch.setattr(campaign, "_local_model_config", lambda: SimpleNamespace(ollama_base_url="http://127.0.0.1:11434"))
    q = campaign.Candidate(campaign.QWEN, campaign.QWEN_DIGEST, "Q4_K_M", 2500)
    c = campaign.Candidate("other:small", "a" * 64, "Q4_K_M", 2000)
    async def candidates(_): return q, c, 1
    async def preflight(): pass
    async def frozen(): return campaign.FrozenRequest((), {}, "1" * 64, "2" * 64,
                            {"REMAINING_MODEL_CALLS": 4, "REMAINING_SECONDS": 200})
    async def forbidden(*_args, **_kwargs): raise AssertionError("inference attempted")
    monkeypatch.setattr(campaign, "_installed_candidates", candidates)
    monkeypatch.setattr(campaign, "_browser_preflight", preflight)
    monkeypatch.setattr(campaign, "_frozen_post_fill", frozen)
    monkeypatch.setattr(campaign, "_choice_probe", forbidden)
    report = await campaign.evaluate("f" * 40)
    assert report["VERDICT"] == "PREPARATION_READY_REAL_EXPERIMENT_NOT_EXECUTED"
    assert report["REQUESTS_DISPATCHED"] == 0
    assert all(item["STATUS"] == "NOT_RUN" for item in report["CELLS"].values())


def _mock_setup(monkeypatch, tmp_path: Path) -> tuple[campaign.Candidate, campaign.Candidate]:
    monkeypatch.setattr(campaign, "LEDGER_DIR", tmp_path)
    monkeypatch.setattr(campaign.compact, "verify_checkout", lambda *_: None)
    monkeypatch.setattr(campaign, "_local_model_config", lambda: SimpleNamespace(
        ollama_base_url="http://127.0.0.1:11434"))
    q = campaign.Candidate(campaign.QWEN, campaign.QWEN_DIGEST, "Q4_K_M", 2500)
    c = campaign.Candidate("llama3.1:8b", "a" * 64, "Q4_K_M", 4700)
    async def candidates(_): return q, c, 1
    async def browser_preflight(): pass
    async def frozen(): return campaign.FrozenRequest((), {}, "1" * 64, "2" * 64,
                            {"REMAINING_MODEL_CALLS": 4, "REMAINING_SECONDS": 200})
    monkeypatch.setattr(campaign, "_installed_candidates", candidates)
    monkeypatch.setattr(campaign, "_browser_preflight", browser_preflight)
    monkeypatch.setattr(campaign, "_frozen_post_fill", frozen)
    return q, c


@pytest.mark.asyncio
async def test_missing_consent_zero_requests_and_rerun_refused(monkeypatch, tmp_path: Path) -> None:
    _mock_setup(monkeypatch, tmp_path)
    async def preflight(*_args):
        return {"MODEL_DIGEST_SHA256": campaign.QWEN_DIGEST}, SimpleNamespace()
    async def forbidden(*_args): raise AssertionError("model request attempted")
    monkeypatch.setattr(campaign.compact, "_preflight", preflight)
    monkeypatch.setattr(campaign.compact, "_consented", lambda _: False)
    monkeypatch.setattr(campaign, "_choice_probe", forbidden)
    first = await campaign.evaluate("f" * 40, run=True)
    assert first["REQUESTS_DISPATCHED"] == 0
    assert first["CELLS"]["QWEN:choice"]["REASON"] == "MODEL_CONSENT_REQUIRED"
    assert all(item["STATUS"] == "BLOCKED" for item in first["CELLS"].values())
    second = await campaign.evaluate("f" * 40, run=True)
    assert second["FAILURE_CLASS"] == "CAMPAIGN_ALREADY_ATTEMPTED"
    assert second["REQUESTS_DISPATCHED"] == 0


@pytest.mark.asyncio
async def test_incorrect_commit_or_changed_identity_blocks_without_inference(
        monkeypatch, tmp_path: Path) -> None:
    _mock_setup(monkeypatch, tmp_path)
    def reject(*_args): raise BrowserRefusal("PREFLIGHT_CHECKOUT")
    monkeypatch.setattr(campaign.compact, "verify_checkout", reject)
    wrong = await campaign.evaluate("f" * 40, run=True)
    assert wrong["FAILURE_CLASS"] == "PREFLIGHT_CHECKOUT"
    assert wrong["REQUESTS_DISPATCHED"] == 0
    assert not list(tmp_path.iterdir())
    monkeypatch.setattr(campaign.compact, "verify_checkout", lambda *_: None)
    async def preflight(*_args):
        return {"MODEL_DIGEST_SHA256": "b" * 64}, SimpleNamespace()
    monkeypatch.setattr(campaign.compact, "_preflight", preflight)
    changed = await campaign.evaluate("f" * 40, run=True)
    assert changed["CELLS"]["QWEN:choice"]["REASON"] == "MODEL_OR_RESOURCE_PREFLIGHT_BLOCKED"
    assert changed["REQUESTS_DISPATCHED"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("calls", [4, 6])
async def test_campaign_accounting_scenarios_and_technical_vs_useful(monkeypatch,
                                                                    tmp_path: Path,
                                                                    calls: int) -> None:
    q, c = _mock_setup(monkeypatch, tmp_path)
    consented = []
    monkeypatch.setattr(campaign.compact, "_consented", lambda phrase:
                        consented.append(phrase) or True)
    async def preflight(model, *_args):
        digest = q.digest if model == q.name else c.digest
        return {"MODEL_DIGEST_SHA256": digest, "MODEL_RESIDENCY": "NOT_LOADED"}, SimpleNamespace()
    monkeypatch.setattr(campaign.compact, "_preflight", preflight)
    async def probe(candidate, frozen, *_args):
        return {"FAILURE_CLASS": "NONE", "TECHNICALLY_QUALIFIED": True,
                "RESOURCE_PRESSURE_WARNING": False, "INPUT_SHA256": frozen.digest,
                "CHOICE_USEFUL_AT_FROZEN_STATE": candidate.name == c.name}
    monkeypatch.setattr(campaign, "_choice_probe", probe)
    seen = []
    async def mission(report, model, *_args, **kwargs):
        seen.append((model, kwargs["fixture"], kwargs["objective"],
                     kwargs["target"], kwargs["expected"], kwargs["hierarchical"]))
        return {"STAGE_B": {"MODEL_CALL_COUNT": calls, "ACTION_COUNT": 4},
                "TASK_ACCEPTANCE_PASSED": kwargs["fixture"] != "relayout",
                "FAILURE_CLASS": "NONE"}
    monkeypatch.setattr(campaign.compact, "_stage_b_after_a", mission)
    report = await campaign.evaluate("f" * 40, run=True)
    assert report["VERDICT"] == "CAMPAIGN_RECORDED"
    assert report["REQUESTS_DISPATCHED"] == 2 + 6 * calls <= 38
    assert report["REQUESTS_RESERVED_MAX"] == 0
    assert len(seen) == 6
    assert [item[1] for item in seen] == ["heldout", "catalog", "relayout"] * 2
    assert all(item[-1] is False for item in seen)
    assert seen[1][3:5] == ("BK-17", "The Atlas of Rivers")
    assert report["CELLS"]["QWEN:choice"]["EVIDENCE"]["CHOICE_USEFUL_AT_FROZEN_STATE"] is False
    assert report["CELLS"]["QWEN:heldout"]["TASK_ACCEPTANCE_PASSED"] is True
    assert consented.count("MODEL ONCE COMPACT") == 2
    assert consented.count("CONTINUE MODEL BLOCK") == 1


@pytest.mark.asyncio
async def test_resource_block_of_other_loaded_model_prevents_browser(monkeypatch,
                                                                     tmp_path: Path) -> None:
    _mock_setup(monkeypatch, tmp_path)
    async def preflight(*_args):
        return {"FAILURE_CLASS": "RESOURCE_PREFLIGHT_REJECTED"}, None
    async def forbidden(*_args, **_kwargs): raise AssertionError("inference or browser")
    monkeypatch.setattr(campaign.compact, "_preflight", preflight)
    monkeypatch.setattr(campaign, "_choice_probe", forbidden)
    monkeypatch.setattr(campaign.compact, "_stage_b_after_a", forbidden)
    result = await campaign.evaluate("f" * 40, run=True)
    assert result["REQUESTS_DISPATCHED"] == 0
    assert all(cell["STATUS"] == "BLOCKED" for cell in result["CELLS"].values())


@pytest.mark.asyncio
async def test_installed_competitor_rule_is_metadata_only(monkeypatch) -> None:
    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): return None
    monkeypatch.setattr(campaign.httpx, "AsyncClient", lambda **_kwargs: Client())
    entries = [
        {"name": campaign.QWEN, "digest": campaign.QWEN_DIGEST,
         "size": 2497293803, "details": {"family": "qwen3", "quantization_level": "Q4_K_M"}},
        {"name": "codeqwen:latest", "digest": "b" * 64,
         "size": 2000000000, "details": {"family": "qwen2", "quantization_level": "Q4_0"}},
        {"name": "llama3:8b", "digest": "c" * 64,
         "size": 3000000000, "details": {"family": "llama", "quantization_level": "Q4_0"}},
        {"name": "llama3.1:8b", "digest": "d" * 64,
         "size": 4920753328, "details": {"family": "llama", "quantization_level": "Q4_K_M"}},
    ]
    async def metadata(*_args): return {"models": entries}, 0
    monkeypatch.setattr(campaign, "_bounded_metadata_get", metadata)
    qwen, competitor, count = await campaign._installed_candidates("http://127.0.0.1:11434")
    assert qwen.digest == campaign.QWEN_DIGEST
    assert competitor.name == "llama3.1:8b" and count == 1
    with pytest.raises(BrowserRefusal, match="LOCAL_MODEL_CONFIG_REJECTED"):
        await campaign._installed_candidates("https://example.com")


@pytest.mark.asyncio
@pytest.mark.parametrize("failure,expected", [
    (TimeoutError(), "MODEL_TIMEOUT"),
    (ValueError("PRIVATE_RESPONSE_MARKER"), "MODEL_ENVELOPE_INVALID"),
])
async def test_choice_probe_timeout_or_invalid_envelope_is_accounted_and_private(
        monkeypatch, failure: Exception, expected: str) -> None:
    class Client:
        def __init__(self, *_args, **_kwargs): pass
        async def chat(self, *_args, **_kwargs): raise failure
        async def close(self): pass
    async def samples(*_args): return _sample()
    monkeypatch.setattr(campaign, "OllamaLLMClient", Client)
    monkeypatch.setattr(campaign.r2, "sample_during_request", samples)
    frozen = campaign.FrozenRequest((SimpleNamespace(content="a"),
        SimpleNamespace(content="b")), {"tools": None, "stream": False,
        "options": {"temperature": 0, "num_predict": 384}, "format": {}},
        "0" * 64, "1" * 64, {})
    result = await campaign._choice_probe(
        campaign.Candidate("llama3.1:8b", "a" * 64, "Q4_K_M", 4693),
        frozen, {"MODEL_RESIDENCY": "NOT_LOADED"},
        SimpleNamespace(ollama_base_url="http://127.0.0.1:11434"))
    assert result["MODEL_CALL_COUNT"] == 1 and result["MODEL_RESPONSE_COUNT"] == 0
    assert result["FAILURE_CLASS"] == expected
    assert result["TECHNICALLY_QUALIFIED"] is False
    assert "PRIVATE_RESPONSE_MARKER" not in json.dumps(result)


def test_mission_outcomes_do_not_confuse_actions_with_acceptance() -> None:
    assert campaign._mission_outcome({"TASK_ACCEPTANCE_PASSED": True}) == "INDEPENDENTLY_ACCEPTED_MISSION"
    assert campaign._mission_outcome({"FAILURE_CLASS": "BROWSER_LOOP_DETECTED"}) == "LOOP_TERMINATED"
    assert campaign._mission_outcome({"FAILURE_CLASS": "MODEL_CALL_BUDGET_EXCEEDED"}) == "MISSION_BUDGET_EXHAUSTED"
    assert campaign._mission_outcome({"FAILURE_CLASS": "NONE", "STAGE_B": {
        "ACTION_COUNT": 3, "NO_PROGRESS_COUNT": 2}}) == "VALID_BUT_UNPRODUCTIVE_ACTIONS"
    assert campaign._mission_outcome({"FAILURE_CLASS": "MODEL_TIMEOUT"}) == "TECHNICAL_OR_PREFLIGHT_FAILURE"


@pytest.mark.asyncio
async def test_structured_format_http_refusal_uses_machine_code_only(monkeypatch) -> None:
    import httpx
    request = httpx.Request("POST", "http://127.0.0.1:11434/api/chat")
    response = httpx.Response(400, request=request,
                              json={"code": "unsupported_format", "error": "PRIVATE_BODY_MARKER"})
    class Client:
        def __init__(self, *_args, **_kwargs): pass
        async def chat(self, *_args, **_kwargs):
            raise httpx.HTTPStatusError("PRIVATE_BODY_MARKER", request=request,
                                        response=response)
        async def close(self): pass
    async def samples(*_args): return _sample()
    monkeypatch.setattr(campaign, "OllamaLLMClient", Client)
    monkeypatch.setattr(campaign.r2, "sample_during_request", samples)
    frozen = campaign.FrozenRequest((SimpleNamespace(content="a"),
        SimpleNamespace(content="b")), {"tools": None, "stream": False,
        "options": {"temperature": 0, "num_predict": 384}, "format": {}},
        "0" * 64, "1" * 64, {})
    result = await campaign._choice_probe(
        campaign.Candidate("llama3.1:8b", "a" * 64, "Q4_K_M", 4693),
        frozen, {"MODEL_RESIDENCY": "NOT_LOADED"},
        SimpleNamespace(ollama_base_url="http://127.0.0.1:11434"))
    assert result["FAILURE_CLASS"] == "MODEL_FORMAT_UNSUPPORTED"
    assert result["HTTP_STATUS"] == 400
    assert result["TECHNICALLY_QUALIFIED"] is False
    assert "PRIVATE_BODY_MARKER" not in json.dumps(result)
