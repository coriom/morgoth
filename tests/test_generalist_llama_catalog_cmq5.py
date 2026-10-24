"""CMQ.5 durable protocol, local preload, and synthetic catalog proof."""

from __future__ import annotations

import asyncio
from hashlib import sha256
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from agents import generalist_llama_catalog_cmq5 as cmq5
from agents import generalist_mission_qualification_cmq4 as cmq4
from agents.generalist_browser_lab import (
    BrowserRefusal, BrowserRunProgress, LexicalObservationPolicy,
    SyntheticTracePolicy, run_isolated_policy,
)


ROOT = Path(__file__).resolve().parents[1]
BROWSER = cmq5.operator.BROWSER
REQUIRES_BROWSER = pytest.mark.skipif(
    not BROWSER.joinpath("chrome-headless-shell").is_file(),
    reason="reviewed Chromium unavailable")


def _sample() -> list[dict[str, object]]:
    return [{"GPU_FREE_MIB": 2048, "HOST_AVAILABLE_MIB": 4096,
             "SWAP_FREE_MIB": 1024, "MEMORY_PRESSURE_AVG10_PCT": 0.0,
             "MEMORY_PRESSURE_FULL_AVG10_PCT": 0.0,
             "CGROUP_MEMORY_PRESSURE_FULL_AVG10_PCT": 0.0,
             "SERVICE_CGROUP_OOM_COUNT": 0, "SERVICE_CGROUP_OOM_KILL_COUNT": 0}]


def _mock_readiness(monkeypatch, tmp_path: Path, *, residency: str = "LOADED",
                    digest: str = cmq5.MODEL_DIGEST):
    monkeypatch.setattr(cmq5, "STATE_DIR", tmp_path / "cmq5")
    monkeypatch.setattr(cmq5.compact, "verify_checkout", lambda *_args: None)
    preflight = {"MODEL_DIGEST_SHA256": digest, "MODEL_RESIDENCY": residency,
                 "MODEL_INSTALLED": True, "MODEL_SIZE_MIB": 4700,
                 "RESOURCE_PREFLIGHT_READY": True, "GPU_MEMORY_FREE_MIB": 1500,
                 "HOST_MEMORY_AVAILABLE_MIB": 5000, "HOST_SWAP_FREE_MIB": 1500}
    calls = []
    async def checked(*_args):
        calls.append("preflight")
        return preflight.copy(), SimpleNamespace(ollama_base_url="http://127.0.0.1:11434")
    async def fixture(**kwargs):
        calls.append("fixture")
        assert kwargs["fixture"] == "catalog"
        assert kwargs["target"] == "BK-17"
        assert kwargs["expected"] == "The Atlas of Rivers"
        return {"BROWSER_ISOLATED": True, "TASK_ACCEPTANCE_PASSED": True}
    frozen = cmq4.FrozenRequest((SimpleNamespace(content="safe system"),
                                  SimpleNamespace(content="safe user")), {},
                                 "1" * 64, cmq5.FIXTURE_DIGEST, {})
    async def snapshot(**kwargs):
        calls.append("snapshot")
        assert kwargs["scenario"] == cmq5.TASK
        return frozen
    monkeypatch.setattr(cmq5.compact, "_preflight", checked)
    monkeypatch.setattr(cmq5, "run_isolated_fixture", fixture)
    monkeypatch.setattr(cmq5.cmq4, "_frozen_post_fill", snapshot)
    return preflight, calls


def test_protocol_is_distinct_and_cmq4_stays_disabled() -> None:
    assert cmq5.PROTOCOL == "CMQ5_LLAMA_CATALOG_V1"
    assert cmq5.MODEL == "llama3.1:8b"
    assert cmq5.MAX_SEMANTIC_REQUESTS == 7
    assert cmq5.MAX_PRELOAD_REQUESTS == 1
    assert cmq4.EXECUTION_DISABLED is True
    policy = SyntheticTracePolicy.from_checkout(ROOT, "catalog")
    assert policy.digest == cmq5.FIXTURE_DIGEST
    assert policy.controls["Find work"] == "C04"
    assert (cmq5.TASK.target, cmq5.TASK.expected) == ("BK-17", "The Atlas of Rivers")
    assert sha256((ROOT / "tests/fixtures/generalist_browser_catalog.html").read_bytes()).hexdigest() == policy.digest


@REQUIRES_BROWSER
@pytest.mark.asyncio
async def test_real_rendered_catalog_frozen_request_has_no_expected_answer() -> None:
    frozen = await cmq4._frozen_post_fill(
        scenario=cmq5.TASK, setup_controls=("Book catalog", "Work code"),
        setup_codes=("C02", "C03"), useful_code="C04")
    assert frozen.source_digest == cmq5.FIXTURE_DIGEST
    assert frozen.summary["TARGET_IN_VISIBLE_INPUT"] is True
    assert frozen.summary["REMAINING_MODEL_CALLS"] == 4
    assert ("C04", "CLICK") in [(item["CONTROL"], item["KIND"])
                                 for item in frozen.summary["CANDIDATES"]]
    assert cmq5.TASK.expected not in frozen.messages[0].content + frozen.messages[1].content
    assert frozen.kwargs["tools"] is None
    assert frozen.kwargs["stream"] is False


@REQUIRES_BROWSER
@pytest.mark.asyncio
async def test_fresh_catalog_browser_accepts_only_rendered_result() -> None:
    progress = BrowserRunProgress()
    result = await run_isolated_policy(
        checkout=ROOT, browser_dir=BROWSER, fixture="catalog",
        objective=cmq5.TASK.objective, target=cmq5.TASK.target,
        expected=cmq5.TASK.expected, source=LexicalObservationPolicy(),
        max_model_calls=6, progress=progress, synthetic_trace=True)
    assert result["TASK_ACCEPTANCE_PASSED"] is True
    assert progress.task_acceptance_passed is True
    assert progress.synthetic_trace[0]["CONTROL"] == "C02"
    assert progress.synthetic_trace[-1]["CONTROL"] == "C05"


@pytest.mark.asyncio
async def test_readiness_only_checks_metadata_and_fixture(monkeypatch, tmp_path: Path) -> None:
    _, calls = _mock_readiness(monkeypatch, tmp_path)
    monkeypatch.setattr(cmq5, "_preload_request", lambda *_args: (_ for _ in ()).throw(
        AssertionError("preload attempted")))
    monkeypatch.setattr(cmq5.cmq4, "_choice_probe", lambda *_args: (_ for _ in ()).throw(
        AssertionError("inference attempted")))
    report = await cmq5.evaluate("f" * 40)
    assert report["VERDICT"] == "READINESS_READY"
    assert report["INFERENCE_ATTEMPTED"] is False
    assert report["PRELOAD_ATTEMPTED"] is False
    assert calls == ["preflight", "fixture"]
    assert not cmq5.STATE_DIR.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("digest,ready", [("b" * 64, True), (cmq5.MODEL_DIGEST, False)])
async def test_identity_or_resource_failure_blocks_calls(monkeypatch, tmp_path: Path,
                                                         digest: str, ready: bool) -> None:
    preflight, _ = _mock_readiness(monkeypatch, tmp_path, digest=digest)
    if not ready:
        preflight["RESOURCE_PREFLIGHT_READY"] = False
        async def blocked(*_args):
            return preflight.copy(), None
        monkeypatch.setattr(cmq5.compact, "_preflight", blocked)
    monkeypatch.setattr(cmq5.compact, "_consented", lambda *_args: (_ for _ in ()).throw(
        AssertionError("consent requested")))
    result = await cmq5.evaluate("f" * 40, run=True)
    assert result["VERDICT"] == "READINESS_BLOCKED"
    assert not cmq5.STATE_DIR.exists()


@pytest.mark.asyncio
async def test_checkout_refusal_blocks_before_metadata_or_inference(monkeypatch,
                                                                    tmp_path: Path) -> None:
    _, calls = _mock_readiness(monkeypatch, tmp_path)
    def refused(*_args):
        raise cmq5.operator.GitCheckoutRefusal("REMOTE_HEAD", "TIMEOUT")
    monkeypatch.setattr(cmq5.compact, "verify_checkout", refused)
    result = await cmq5.evaluate("f" * 40, run=True)
    assert result["VERDICT"] == "READINESS_BLOCKED"
    assert result["FAILURE_CLASS"] == "PREFLIGHT_CHECKOUT"
    assert result["CHECKOUT_OPERATION"] == "REMOTE_HEAD"
    assert result["CHECKOUT_FAILURE"] == "TIMEOUT"
    assert calls == []
    assert not cmq5.STATE_DIR.exists()


@pytest.mark.asyncio
async def test_preload_empty_prompt_only_and_generated_content_rejected(monkeypatch) -> None:
    async def samples(*_args):
        return _sample()
    monkeypatch.setattr(cmq5.r2, "sample_during_request", samples)
    seen = []
    def response(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"model": cmq5.MODEL, "done": True,
                                         "done_reason": "load", "response": "",
                                         "eval_count": 0, "prompt_eval_count": 0,
                                         "load_duration": 10_000_000_000,
                                         "total_duration": 11_000_000_000})
    trial = await cmq5._preload_request("http://127.0.0.1:11434", transport=httpx.MockTransport(response))
    assert trial["COMPLETE_NON_GENERATIVE_LOAD"] is True
    assert trial["RESOURCE_ASSESSMENT"] == "RESOURCE_CLEAR"
    assert trial["LOAD_DURATION_MS"] == 10000
    assert seen == [("/api/generate", {"model": cmq5.MODEL, "prompt": "",
                                         "stream": False, "keep_alive": cmq5.KEEP_ALIVE})]
    def generated(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"model": cmq5.MODEL, "done": True,
                                         "done_reason": "stop", "response": "PRIVATE_OUTPUT",
                                         "eval_count": 2})
    blocked = await cmq5._preload_request("http://127.0.0.1:11434", transport=httpx.MockTransport(generated))
    assert blocked["FAILURE_CLASS"] == "PRELOAD_NOT_NON_GENERATIVE"
    assert "PRIVATE_OUTPUT" not in str(blocked)


@pytest.mark.asyncio
async def test_preload_timeout_and_redirect_remain_blocked(monkeypatch) -> None:
    async def samples(*_args):
        return _sample()
    monkeypatch.setattr(cmq5.r2, "sample_during_request", samples)
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("private transport detail", request=request)
    timed = await cmq5._preload_request("http://127.0.0.1:11434",
                                       transport=httpx.MockTransport(timeout))
    assert timed["FAILURE_CLASS"] == "PRELOAD_TIMEOUT"
    assert timed["RESPONSE_RECEIVED"] is False
    assert "private transport detail" not in str(timed)
    redirected = await cmq5._preload_request("http://127.0.0.1:11434",
        transport=httpx.MockTransport(lambda _: httpx.Response(302, headers={
            "Location": "https://example.invalid"})))
    assert redirected["FAILURE_CLASS"] == "PRELOAD_HTTP_REJECTED"
    assert redirected["HTTP_STATUS"] == 302
    assert "example.invalid" not in str(redirected)


@pytest.mark.asyncio
async def test_cold_preload_then_one_choice_and_fresh_mission(monkeypatch,
                                                              tmp_path: Path) -> None:
    preflight, calls = _mock_readiness(monkeypatch, tmp_path, residency="NOT_LOADED")
    answers = []
    monkeypatch.setattr(cmq5.compact, "_consented", lambda phrase: answers.append(phrase) or True)
    async def load(*_args):
        calls.append("load")
        return {"REQUEST_ATTEMPTED": True, "RESPONSE_RECEIVED": True,
                "COMPLETE_NON_GENERATIVE_LOAD": True, "RESOURCE_ASSESSMENT": "RESOURCE_CLEAR",
                "RESOURCE_PRESSURE_WARNING": False, "FAILURE_CLASS": "NONE"}
    async def checked(*_args):
        calls.append("postload-preflight")
        return {**preflight, "MODEL_RESIDENCY": "LOADED"}, SimpleNamespace(
            ollama_base_url="http://127.0.0.1:11434")
    async def choice(*_args, **_kwargs):
        calls.append("choice")
        return {"MODEL_CALL_COUNT": 1, "MODEL_RESPONSE_COUNT": 1,
                "FAILURE_CLASS": "NONE", "TECHNICALLY_QUALIFIED": True,
                "RESOURCE_PRESSURE_WARNING": False,
                "CHOICE_USEFUL_AT_FROZEN_STATE": True, "SYNTHETIC_CHOICE_CODE": "C04"}
    async def mission(*_args, **_kwargs):
        calls.append("mission")
        return {"FAILURE_CLASS": "NONE", "TASK_ACCEPTANCE_PASSED": True,
                "STAGE_B": {"MODEL_CALL_COUNT": 4, "MODEL_RESPONSE_COUNT": 4,
                            "TRACE_SOURCE_VERIFIED": True, "TRACE_COMPLETE": True}}
    monkeypatch.setattr(cmq5, "_preload_request", load)
    original = cmq5.compact._preflight
    async def first_then_loaded(*args):
        if calls.count("preflight") == 0:
            return await original(*args)
        return await checked(*args)
    monkeypatch.setattr(cmq5.compact, "_preflight", first_then_loaded)
    monkeypatch.setattr(cmq5.cmq4, "_choice_probe", choice)
    monkeypatch.setattr(cmq5.compact, "_stage_b_after_a", mission)
    result = await cmq5.evaluate("f" * 40, run=True)
    assert result["VERDICT"] == "SYNTHETIC_MISSION_COMPLETED"
    assert result["PRELOAD_REQUESTS_ATTEMPTED"] == 1
    assert result["PRELOAD_RESPONSE_COUNT"] == 1
    assert result["PRELOAD_RESERVED_MAX"] == 0
    assert result["SEMANTIC_REQUESTS_ATTEMPTED"] == 5
    assert answers == ["LOAD LLAMA ONCE", "MODEL ONCE LLAMA"]
    assert calls == ["preflight", "fixture", "snapshot", "load",
                     "postload-preflight", "choice", "mission"]


@pytest.mark.asyncio
async def test_incomplete_preload_blocks_inference_with_unknown_reservation(monkeypatch,
                                                                            tmp_path: Path) -> None:
    _mock_readiness(monkeypatch, tmp_path, residency="NOT_LOADED")
    monkeypatch.setattr(cmq5.compact, "_consented", lambda *_args: True)
    async def load(*_args):
        return {"REQUEST_ATTEMPTED": True, "RESPONSE_RECEIVED": False,
                "COMPLETE_NON_GENERATIVE_LOAD": False,
                "RESOURCE_ASSESSMENT": "RESOURCE_EVIDENCE_INSUFFICIENT",
                "FAILURE_CLASS": "PRELOAD_TIMEOUT"}
    async def forbidden(*_args, **_kwargs):
        raise AssertionError("inference dispatched")
    monkeypatch.setattr(cmq5, "_preload_request", load)
    monkeypatch.setattr(cmq5.cmq4, "_choice_probe", forbidden)
    result = await cmq5.evaluate("f" * 40, run=True)
    assert result["VERDICT"] == "PRELOAD_BLOCKED"
    assert result["FAILURE_CLASS"] == "PRELOAD_TIMEOUT"
    assert result["PRELOAD_REQUESTS_ATTEMPTED"] == 1
    assert result["PRELOAD_RESERVED_MAX"] == 1
    assert result["SEMANTIC_REQUESTS_ATTEMPTED"] == 0
    assert result["STAGE_A"] == {"STATUS": "NOT_RUN"}
    assert json.loads((cmq5.STATE_DIR / "attempt.json").read_text()) == result


@pytest.mark.asyncio
async def test_missing_consents_leave_receipts_but_no_claim(monkeypatch, tmp_path: Path) -> None:
    _mock_readiness(monkeypatch, tmp_path, residency="NOT_LOADED")
    answers = iter([False, True, False])
    monkeypatch.setattr(cmq5.compact, "_consented", lambda _phrase: next(answers))
    first = await cmq5.evaluate("f" * 40, run=True)
    second = await cmq5.evaluate("f" * 40, run=True)
    assert first["FAILURE_CLASS"] == "LOAD_CONSENT_REQUIRED"
    assert second["FAILURE_CLASS"] == "MODEL_CONSENT_REQUIRED"
    assert not (cmq5.STATE_DIR / "campaign.claim").exists()
    assert not (cmq5.STATE_DIR / "attempt.json").exists()
    receipts = sorted(cmq5.STATE_DIR.glob("consent-refusal-*.json"))
    assert len(receipts) == 2
    assert {json.loads(path.read_text())["REASON"] for path in receipts} == {
        "LOAD_CONSENT_REQUIRED", "MODEL_CONSENT_REQUIRED"}
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in receipts)


@pytest.mark.asyncio
async def test_loaded_model_unproductive_choice_still_runs_fresh_mission(monkeypatch,
                                                                       tmp_path: Path) -> None:
    _, calls = _mock_readiness(monkeypatch, tmp_path)
    consents = []
    monkeypatch.setattr(cmq5.compact, "_consented", lambda phrase: consents.append(phrase) or True)
    async def choice(*_args, **kwargs):
        assert kwargs == {"fixture": "catalog", "useful_code": "C04"}
        calls.append("choice")
        return {"MODEL_CALL_COUNT": 1, "MODEL_RESPONSE_COUNT": 1,
                "FAILURE_CLASS": "NONE", "TECHNICALLY_QUALIFIED": True,
                "CHOICE_USEFUL_AT_FROZEN_STATE": False, "SYNTHETIC_CHOICE_CODE": "C03",
                "RESOURCE_PRESSURE_WARNING": False}
    async def mission(*_args, **kwargs):
        calls.append("mission")
        assert kwargs["fixture"] == "catalog"
        assert kwargs["hierarchical"] is False
        assert kwargs["expected"] == cmq5.TASK.expected
        return {"FAILURE_CLASS": "NONE", "TASK_ACCEPTANCE_PASSED": True,
                "STAGE_B": {"MODEL_CALL_COUNT": 4, "MODEL_RESPONSE_COUNT": 4,
                            "TRACE_SOURCE_VERIFIED": True, "TRACE_COMPLETE": True,
                            "TASK_ACCEPTANCE_PASSED": True}}
    monkeypatch.setattr(cmq5.cmq4, "_choice_probe", choice)
    monkeypatch.setattr(cmq5.compact, "_stage_b_after_a", mission)
    result = await cmq5.evaluate("f" * 40, run=True)
    assert result["VERDICT"] == "SYNTHETIC_MISSION_COMPLETED"
    assert result["STAGE_A"]["CHOICE_USEFUL_AT_FROZEN_STATE"] is False
    assert result["SEMANTIC_REQUESTS_ATTEMPTED"] == 5 <= 7
    assert result["SEMANTIC_RESPONSE_COUNT"] == 5
    assert result["SEMANTIC_RESERVED_MAX"] == 0
    assert result["TASK_ACCEPTANCE_PASSED"] is True
    assert consents == ["MODEL ONCE LLAMA"]
    assert calls == ["preflight", "fixture", "snapshot", "choice", "mission"]
    assert json.loads((cmq5.STATE_DIR / "attempt.json").read_text()) == result
    assert (cmq5.STATE_DIR / "campaign.claim").exists()
    assert (await cmq5.evaluate("e" * 40, run=True))["FAILURE_CLASS"] == "CAMPAIGN_ALREADY_CONSUMED"


@pytest.mark.asyncio
async def test_browser_claim_without_verified_trace_never_passes_acceptance(monkeypatch,
                                                                             tmp_path: Path) -> None:
    _mock_readiness(monkeypatch, tmp_path)
    monkeypatch.setattr(cmq5.compact, "_consented", lambda *_args: True)
    async def choice(*_args, **_kwargs):
        return {"MODEL_CALL_COUNT": 1, "MODEL_RESPONSE_COUNT": 1,
                "FAILURE_CLASS": "NONE", "TECHNICALLY_QUALIFIED": True,
                "RESOURCE_PRESSURE_WARNING": False}
    async def mission(*_args, **_kwargs):
        return {"FAILURE_CLASS": "NONE", "TASK_ACCEPTANCE_PASSED": True,
                "STAGE_B": {"MODEL_CALL_COUNT": 1, "MODEL_RESPONSE_COUNT": 1,
                            "TRACE_SOURCE_VERIFIED": False, "TRACE_COMPLETE": False}}
    monkeypatch.setattr(cmq5.cmq4, "_choice_probe", choice)
    monkeypatch.setattr(cmq5.compact, "_stage_b_after_a", mission)
    result = await cmq5.evaluate("f" * 40, run=True)
    assert result["VERDICT"] == "SYNTHETIC_MISSION_BLOCKED"
    assert result["TASK_ACCEPTANCE_PASSED"] is False
    assert result["SEMANTIC_REQUESTS_ATTEMPTED"] == 2


@pytest.mark.asyncio
async def test_stage_a_valid_but_not_qualified_blocks_mission(monkeypatch, tmp_path: Path) -> None:
    _mock_readiness(monkeypatch, tmp_path)
    monkeypatch.setattr(cmq5.compact, "_consented", lambda *_args: True)
    async def choice(*_args, **_kwargs):
        return {"MODEL_CALL_COUNT": 1, "MODEL_RESPONSE_COUNT": 1,
                "FAILURE_CLASS": "MODEL_ACTION_SCHEMA_INVALID",
                "TECHNICALLY_QUALIFIED": False, "RESOURCE_PRESSURE_WARNING": False}
    async def forbidden(*_args, **_kwargs):
        raise AssertionError("browser mission attempted")
    monkeypatch.setattr(cmq5.cmq4, "_choice_probe", choice)
    monkeypatch.setattr(cmq5.compact, "_stage_b_after_a", forbidden)
    result = await cmq5.evaluate("f" * 40, run=True)
    assert result["VERDICT"] == "STAGE_A_BLOCKED"
    assert result["SEMANTIC_REQUESTS_ATTEMPTED"] == 1
    assert result["SEMANTIC_RESPONSE_COUNT"] == 1
    assert result["TASK_ACCEPTANCE_PASSED"] is False
    assert result["STAGE_B"] == {"STATUS": "NOT_RUN"}


@pytest.mark.asyncio
async def test_unknown_browser_count_keeps_six_reserved_and_blocks_replay(monkeypatch,
                                                                         tmp_path: Path) -> None:
    _mock_readiness(monkeypatch, tmp_path)
    monkeypatch.setattr(cmq5.compact, "_consented", lambda *_args: True)
    async def choice(*_args, **_kwargs):
        return {"MODEL_CALL_COUNT": 1, "MODEL_RESPONSE_COUNT": 1,
                "FAILURE_CLASS": "NONE", "TECHNICALLY_QUALIFIED": True,
                "RESOURCE_PRESSURE_WARNING": False}
    async def mission(*_args, **_kwargs):
        return {"STAGE_B_CALL_COUNT_UNKNOWN": True, "FAILURE_CLASS": "BROWSER_EVALUATION_FAILED"}
    monkeypatch.setattr(cmq5.cmq4, "_choice_probe", choice)
    monkeypatch.setattr(cmq5.compact, "_stage_b_after_a", mission)
    result = await cmq5.evaluate("f" * 40, run=True)
    assert result["FAILURE_CLASS"] == "MISSION_CALL_COUNT_UNKNOWN"
    assert result["SEMANTIC_REQUESTS_ATTEMPTED"] == 1
    assert result["SEMANTIC_RESERVED_MAX"] == 6
    assert result["REAL_EXPERIMENT_EXECUTED"] is None
    assert json.loads((cmq5.STATE_DIR / "attempt.json").read_text()) == result
    assert (await cmq5.evaluate("e" * 40, run=True))["FAILURE_CLASS"] == "CAMPAIGN_ALREADY_CONSUMED"


@pytest.mark.asyncio
async def test_cancelled_stage_a_keeps_request_reserved(monkeypatch, tmp_path: Path) -> None:
    _mock_readiness(monkeypatch, tmp_path)
    monkeypatch.setattr(cmq5.compact, "_consented", lambda *_args: True)
    async def cancelled(*_args, **_kwargs):
        raise asyncio.CancelledError
    monkeypatch.setattr(cmq5.cmq4, "_choice_probe", cancelled)
    result = await cmq5.evaluate("f" * 40, run=True)
    assert result["FAILURE_CLASS"] == "CAMPAIGN_INTERRUPTED"
    assert result["SEMANTIC_RESERVED_MAX"] == 1
    assert result["REAL_EXPERIMENT_EXECUTED"] is None
    assert json.loads((cmq5.STATE_DIR / "attempt.json").read_text()) == result
    assert (cmq5.STATE_DIR / "campaign.claim").exists()


def test_state_dir_rejects_symlink_and_unsafe_mode(monkeypatch, tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    monkeypatch.setattr(cmq5, "STATE_DIR", link)
    with pytest.raises(BrowserRefusal, match="STATE_DIRECTORY_UNTRUSTED"):
        cmq5._secure_directory(create=True)
    proper = tmp_path / "private"
    proper.mkdir(mode=0o755)
    monkeypatch.setattr(cmq5, "STATE_DIR", proper)
    with pytest.raises(BrowserRefusal, match="STATE_DIRECTORY_UNTRUSTED"):
        cmq5._secure_directory(create=False)
