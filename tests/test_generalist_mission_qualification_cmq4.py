"""CMQ.4 preparation, accounting and real confined-browser protocol tests."""

from __future__ import annotations

import asyncio
from hashlib import sha256
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agents import generalist_mission_qualification_cmq4 as campaign
from agents.generalist_browser_lab import BrowserRefusal, LexicalObservationPolicy, run_isolated_policy
from scripts.codex_local_warning_review import _consent


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
    cells = campaign._cells()
    for key, cell in cells.items():
        cell.update(STATUS="BLOCKED", REASON=("MODEL_CONSENT_REQUIRED" if key == "QWEN:choice"
                                               else "PREREQUISITE_OR_PRIOR_CELL_BLOCKED"))
    original = {
        "CANDIDATES": [q.__dict__, c.__dict__], "CELLS": cells,
        "FROZEN_BUDGET_CALLS": 4, "FROZEN_BUDGET_SECONDS": 259,
        "INSTALLED_ALTERNATIVE_COUNT": 1, "MAX_REQUESTS": 38,
        "MAX_REQUESTS_PER_MODEL": 19,
        "MODEL_REQUESTS": {"QWEN": 0, "COMPETITOR": 0},
        "PAIRED_INPUT_SHA256": "3" * 64, "PRODUCTION_QUALIFIED": False,
        "REAL_EXPERIMENT_EXECUTED": False, "RENDERED_POST_FILL_VERIFIED": True,
        "REQUESTS_DISPATCHED": 0, "REQUESTS_RESERVED_MAX": 0,
        "REVIEWED_FIXTURE_SHA256": "2" * 64,
        "VERDICT": "CAMPAIGN_RECORDED_WITH_BLOCKED_CELLS",
    }
    source = campaign._ledger_path(campaign.RECOVERY_SOURCE_SHA)
    campaign._write_ledger(source, original, first=True)
    monkeypatch.setattr(campaign, "RECOVERY_LEDGER_SHA256", sha256(source.read_bytes()).hexdigest())
    return q, c


def test_actual_original_ledger_eligible_and_unchanged() -> None:
    """Local integration proof; hermetic mutation cases remain below."""
    source = campaign._ledger_path(campaign.RECOVERY_SOURCE_SHA)
    if not source.is_file():
        pytest.skip("operator's original ledger unavailable in this environment")
    before = source.read_bytes()
    evidence = campaign._inspect_original_ledger()
    assert evidence["RECOVERY_SOURCE_SHA"] == campaign.RECOVERY_SOURCE_SHA
    assert evidence["RECOVERY_ORIGINAL_LEDGER_SHA256"] == campaign.RECOVERY_LEDGER_SHA256
    assert source.read_bytes() == before


def test_exact_foreground_phrase_still_rejects_incident_typo() -> None:
    class Terminal:
        def __init__(self, answer: str): self.answer = answer
        def write(self, _value: str): pass
        def flush(self): pass
        def readline(self, _limit: int): return self.answer
    assert _consent(Terminal("MODEL ONCE COMPT\n"), "MODEL ONCE COMPACT") is False
    assert _consent(Terminal("MODEL ONCE COMPACT\n"), "MODEL ONCE COMPACT") is True


@pytest.mark.parametrize("mutation", [
    "dispatched", "reservation", "model_count", "unknown_execution", "missing_cell",
    "unknown_cell", "positive_cell", "extra_cell_field", "wrong_reason",
    "missing_top_field", "wrong_verdict",
])
def test_recovery_refuses_inconsistent_or_uncertain_original(monkeypatch,
                                                             tmp_path: Path,
                                                             mutation: str) -> None:
    _mock_setup(monkeypatch, tmp_path)
    path = campaign._ledger_path(campaign.RECOVERY_SOURCE_SHA)
    original = json.loads(path.read_bytes())
    if mutation == "dispatched": original["REQUESTS_DISPATCHED"] = 1
    elif mutation == "reservation": original["REQUESTS_RESERVED_MAX"] = 1
    elif mutation == "model_count": original["MODEL_REQUESTS"]["QWEN"] = 1
    elif mutation == "unknown_execution": original["REAL_EXPERIMENT_EXECUTED"] = None
    elif mutation == "missing_cell": del original["CELLS"]["COMPETITOR:catalog"]
    elif mutation == "unknown_cell": original["CELLS"]["QWEN:choice"]["STATUS"] = "ATTEMPTED_RESULT_UNKNOWN"
    elif mutation == "positive_cell": original["CELLS"]["QWEN:choice"]["REQUESTS"] = 1
    elif mutation == "extra_cell_field": original["CELLS"]["QWEN:choice"]["EVIDENCE"] = {}
    elif mutation == "wrong_reason": original["CELLS"]["QWEN:choice"]["REASON"] = "OTHER"
    elif mutation == "missing_top_field": del original["MODEL_REQUESTS"]
    elif mutation == "wrong_verdict": original["VERDICT"] = "CAMPAIGN_BLOCKED"
    path.write_text(json.dumps(original))
    monkeypatch.setattr(campaign, "RECOVERY_LEDGER_SHA256", sha256(path.read_bytes()).hexdigest())
    with pytest.raises(BrowserRefusal, match="RECOVERY_ORIGINAL_EVIDENCE_INVALID"):
        campaign._inspect_original_ledger()


def test_recovery_refuses_hash_change_duplicate_keys_and_bad_file(monkeypatch,
                                                                  tmp_path: Path) -> None:
    _mock_setup(monkeypatch, tmp_path)
    path = campaign._ledger_path(campaign.RECOVERY_SOURCE_SHA)
    before = path.read_bytes()
    path.write_bytes(before + b" ")
    with pytest.raises(BrowserRefusal, match="RECOVERY_ORIGINAL_EVIDENCE_INVALID"):
        campaign._inspect_original_ledger()
    path.write_bytes(before.replace(b'"REQUESTS_DISPATCHED":0',
                                    b'"REQUESTS_DISPATCHED":0,"REQUESTS_DISPATCHED":0'))
    monkeypatch.setattr(campaign, "RECOVERY_LEDGER_SHA256", sha256(path.read_bytes()).hexdigest())
    with pytest.raises(BrowserRefusal, match="RECOVERY_ORIGINAL_EVIDENCE_INVALID"):
        campaign._inspect_original_ledger()
    path.write_bytes(before)
    monkeypatch.setattr(campaign, "RECOVERY_LEDGER_SHA256", sha256(before).hexdigest())
    path.chmod(0o644)
    with pytest.raises(BrowserRefusal, match="RECOVERY_ORIGINAL_EVIDENCE_INVALID"):
        campaign._inspect_original_ledger()
    path.unlink()
    path.symlink_to(tmp_path / "elsewhere")
    with pytest.raises(BrowserRefusal, match="RECOVERY_ORIGINAL_EVIDENCE_INVALID"):
        campaign._inspect_original_ledger()


@pytest.mark.asyncio
async def test_new_commit_or_plain_run_does_not_authorize_recovery(monkeypatch,
                                                                  tmp_path: Path) -> None:
    _mock_setup(monkeypatch, tmp_path)
    async def forbidden(*_args, **_kwargs): raise AssertionError("inference attempted")
    monkeypatch.setattr(campaign, "_choice_probe", forbidden)
    result = await campaign.evaluate("f" * 40, run=True)
    assert result["FAILURE_CLASS"] == "RECOVERY_REQUIRED"
    assert result["REQUESTS_DISPATCHED"] == 0
    assert not campaign._ledger_path("f" * 40).exists()


@pytest.mark.asyncio
async def test_changed_competitor_identity_refuses_before_consent(monkeypatch,
                                                                 tmp_path: Path) -> None:
    qwen, competitor = _mock_setup(monkeypatch, tmp_path)
    async def changed(_):
        return qwen, campaign.Candidate(competitor.name, "b" * 64,
                                        competitor.quantization, competitor.size_mib), 1
    def forbidden(_): raise AssertionError("consent requested")
    monkeypatch.setattr(campaign, "_installed_candidates", changed)
    monkeypatch.setattr(campaign.compact, "_consented", forbidden)
    result = await campaign.evaluate("f" * 40, run=True, recover=True)
    assert result["FAILURE_CLASS"] == "RECOVERY_CANDIDATE_IDENTITY_CHANGED"
    assert not campaign._recovery_claim_path().exists()


@pytest.mark.asyncio
async def test_missing_consent_zero_requests_and_retryable(monkeypatch, tmp_path: Path) -> None:
    _mock_setup(monkeypatch, tmp_path)
    async def preflight(*_args):
        return {"MODEL_DIGEST_SHA256": campaign.QWEN_DIGEST}, SimpleNamespace()
    async def forbidden(*_args): raise AssertionError("model request attempted")
    monkeypatch.setattr(campaign.compact, "_preflight", preflight)
    monkeypatch.setattr(campaign.compact, "_consented", lambda _: False)
    monkeypatch.setattr(campaign, "_choice_probe", forbidden)
    original = campaign._ledger_path(campaign.RECOVERY_SOURCE_SHA)
    original_bytes = original.read_bytes()
    first = await campaign.evaluate("f" * 40, run=True, recover=True)
    assert first["REQUESTS_DISPATCHED"] == 0
    assert first["VERDICT"] == "CONSENT_REFUSED_BEFORE_INFERENCE"
    assert first["RECOVERY_RETRYABLE"] is True
    assert first["CELLS"]["QWEN:choice"]["REASON"] == "MODEL_CONSENT_REQUIRED"
    assert all(item["STATUS"] == "BLOCKED" for item in first["CELLS"].values())
    assert not campaign._ledger_path("f" * 40).exists()
    assert not campaign._recovery_claim_path().exists()
    receipts = sorted(tmp_path.glob("morgoth-cmq4-consent-refusal-*.json"))
    assert len(receipts) == 1
    assert json.loads(receipts[0].read_text()) == {
        "REASON": "MODEL_CONSENT_REQUIRED", "SOURCE_SHA": campaign.RECOVERY_SOURCE_SHA,
        "SOURCE_LEDGER_SHA256": sha256(original_bytes).hexdigest(),
        "SUCCESSOR_SHA": "f" * 40, "REQUESTS_DISPATCHED": 0,
        "REQUESTS_RESERVED_MAX": 0,
    }
    second = await campaign.evaluate("f" * 40, run=True, recover=True)
    assert second["VERDICT"] == "CONSENT_REFUSED_BEFORE_INFERENCE"
    assert second["REQUESTS_DISPATCHED"] == 0
    assert len(list(tmp_path.glob("morgoth-cmq4-consent-refusal-*.json"))) == 2
    assert original.read_bytes() == original_bytes


@pytest.mark.asyncio
async def test_incorrect_commit_or_changed_identity_blocks_without_inference(
        monkeypatch, tmp_path: Path) -> None:
    _mock_setup(monkeypatch, tmp_path)
    def reject(*_args): raise BrowserRefusal("PREFLIGHT_CHECKOUT")
    monkeypatch.setattr(campaign.compact, "verify_checkout", reject)
    wrong = await campaign.evaluate("f" * 40, run=True)
    assert wrong["FAILURE_CLASS"] == "PREFLIGHT_CHECKOUT"
    assert wrong["REQUESTS_DISPATCHED"] == 0
    assert len(list(tmp_path.iterdir())) == 1
    monkeypatch.setattr(campaign.compact, "verify_checkout", lambda *_: None)
    async def preflight(*_args):
        return {"MODEL_DIGEST_SHA256": "b" * 64}, SimpleNamespace()
    monkeypatch.setattr(campaign.compact, "_preflight", preflight)
    changed = await campaign.evaluate("f" * 40, run=True, recover=True)
    assert changed["CELLS"]["QWEN:choice"]["REASON"] == "MODEL_OR_RESOURCE_PREFLIGHT_BLOCKED"
    assert changed["REQUESTS_DISPATCHED"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("calls", [4, 6])
async def test_campaign_accounting_scenarios_and_technical_vs_useful(monkeypatch,
                                                                    tmp_path: Path,
                                                                    calls: int) -> None:
    q, c = _mock_setup(monkeypatch, tmp_path)
    old_path = campaign._ledger_path(campaign.RECOVERY_SOURCE_SHA)
    old_bytes = old_path.read_bytes()
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
    report = await campaign.evaluate("f" * 40, run=True, recover=True)
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
    assert old_path.read_bytes() == old_bytes
    successor = json.loads(campaign._ledger_path("f" * 40).read_text())
    assert successor["RECOVERY_SOURCE_SHA"] == campaign.RECOVERY_SOURCE_SHA
    assert successor["RECOVERY_ORIGINAL_LEDGER_SHA256"] == sha256(old_bytes).hexdigest()
    assert successor["RECOVERY_ORIGINAL_INPUT_SHA256"] == "3" * 64
    assert successor["PAIRED_INPUT_SHA256"] == "1" * 64
    assert successor["REQUESTS_DISPATCHED"] == 2 + 6 * calls


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
    result = await campaign.evaluate("f" * 40, run=True, recover=True)
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


@pytest.mark.asyncio
async def test_unknown_browser_call_count_keeps_reservation_and_blocks_rerun(
        monkeypatch, tmp_path: Path) -> None:
    _mock_setup(monkeypatch, tmp_path)
    async def preflight(*_args):
        return {"MODEL_DIGEST_SHA256": campaign.QWEN_DIGEST}, SimpleNamespace()
    async def probe(*_args):
        return {"FAILURE_CLASS": "NONE", "TECHNICALLY_QUALIFIED": True,
                "RESOURCE_PRESSURE_WARNING": False}
    async def browser(*_args, **_kwargs):
        return {"FAILURE_CLASS": "BROWSER_EVALUATION_FAILED",
                "STAGE_B_CALL_COUNT_UNKNOWN": True}
    monkeypatch.setattr(campaign.compact, "_preflight", preflight)
    monkeypatch.setattr(campaign.compact, "_consented", lambda _: True)
    monkeypatch.setattr(campaign, "_choice_probe", probe)
    monkeypatch.setattr(campaign.compact, "_stage_b_after_a", browser)
    result = await campaign.evaluate("f" * 40, run=True, recover=True)
    assert result["FAILURE_CLASS"] == "CAMPAIGN_ATTEMPT_COUNT_UNKNOWN"
    assert result["REQUESTS_DISPATCHED"] == 1
    assert result["REQUESTS_RESERVED_MAX"] == 6
    assert result["REAL_EXPERIMENT_EXECUTED"] is None
    ledger = json.loads((tmp_path / f"morgoth-cmq4-{'f' * 40}.json").read_text())
    assert ledger["CELLS"]["QWEN:heldout"]["STATUS"] == "ATTEMPTED_RESULT_UNKNOWN"
    rerun = await campaign.evaluate("f" * 40, run=True, recover=True)
    assert rerun["FAILURE_CLASS"] == "RECOVERY_ALREADY_CLAIMED"
    another_sha = await campaign.evaluate("e" * 40, run=True, recover=True)
    assert another_sha["FAILURE_CLASS"] == "RECOVERY_ALREADY_CLAIMED"


@pytest.mark.asyncio
async def test_concurrent_recovery_claim_dispatches_only_once(monkeypatch,
                                                              tmp_path: Path) -> None:
    _mock_setup(monkeypatch, tmp_path)
    sent = 0
    async def preflight(*_args):
        await asyncio.sleep(0)
        return {"MODEL_DIGEST_SHA256": campaign.QWEN_DIGEST}, SimpleNamespace()
    async def probe(*_args):
        nonlocal sent
        sent += 1
        await asyncio.sleep(0)
        return {"FAILURE_CLASS": "MODEL_TIMEOUT", "TECHNICALLY_QUALIFIED": False,
                "RESOURCE_PRESSURE_WARNING": False}
    monkeypatch.setattr(campaign.compact, "_preflight", preflight)
    monkeypatch.setattr(campaign.compact, "_consented", lambda _: True)
    monkeypatch.setattr(campaign, "_choice_probe", probe)
    first, second = await asyncio.gather(
        campaign.evaluate("f" * 40, run=True, recover=True),
        campaign.evaluate("f" * 40, run=True, recover=True))
    assert sent == 1
    assert sorted([first["REQUESTS_DISPATCHED"], second["REQUESTS_DISPATCHED"]]) == [0, 1]
    assert "RECOVERY_ALREADY_CLAIMED" in {first.get("FAILURE_CLASS"), second.get("FAILURE_CLASS")}
    assert campaign._recovery_claim_path().exists()
