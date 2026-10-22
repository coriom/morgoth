"""Hermetic privacy and lifecycle tests; no real Ollama inference or journals."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import asyncio
import subprocess
import sys
from types import SimpleNamespace

import pytest

from agents import generalist_ollama_reliability as reliability
from agents import generalist_ollama_runner_diagnostic as diagnostic
from agents import generalist_browser_lab as lab


def _record(message: str, second: int) -> bytes:
    return json.dumps({"MESSAGE": message,
                       "__REALTIME_TIMESTAMP": str(second * 1_000_000)}).encode()


def test_journal_classes_do_not_disclose_private_text_or_infer_cuda_from_mention(
        monkeypatch: pytest.MonkeyPatch) -> None:
    private = "PRIVATE_PROMPT=/home/operator/secret response=private"
    lines = [
        _record("runner failed; cuda backend mentioned; timeout " + private, 1),
        _record("model load failed: context canceled " + private, 2),
        _record("CUDA_ERROR_OUT_OF_MEMORY " + private, 3),
    ]
    monkeypatch.setattr(diagnostic, "_journal_bytes", lambda *_: (b"\n".join(lines), "OK"))
    result = diagnostic.journal_evidence("start", "end")
    assert result["RECORDS_SCANNED"] == 3
    assert result["EVENTS"][0]["CLASSES"] == [
        "CUDA_MENTION_IN_FAILURE", "TIMEOUT_REPORTED", "RUNNER_FAILURE_REPORTED"]
    assert result["EVENTS"][1]["CLASSES"] == [
        "MODEL_LOAD_FAILURE_REPORTED", "CANCELLATION_REPORTED"]
    assert "CUDA_ERROR_CODE_REPORTED" in result["EVENTS"][2]["CLASSES"]
    assert "EXPLICIT_OOM_REPORTED" in result["EVENTS"][2]["CLASSES"]
    assert private not in json.dumps(result)
    assert "/home/operator" not in json.dumps(result)


def test_journal_missing_or_oversize_stays_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(diagnostic, "_journal_bytes", lambda *_: (None, "OVERSIZE"))
    result = diagnostic.journal_evidence("start", "end")
    assert result["STATUS"] == "OVERSIZE" and result["EVENTS"] == []
    assert diagnostic.classify_message("cuda runner initialized") == []
    assert diagnostic.classify_message("x" * 16385) == []


@pytest.mark.parametrize("mode,expected", [("stall", "TIMEOUT"),
                                            ("oversize", "OVERSIZE")])
def test_journal_reader_caps_time_and_bytes_and_reaps_child(
        monkeypatch: pytest.MonkeyPatch, mode: str, expected: str) -> None:
    original = subprocess.Popen
    children: list[subprocess.Popen] = []

    def start(_args, **kwargs):
        script = ("import time; time.sleep(2)" if mode == "stall" else
                  "import sys; sys.stdout.write('x'*200); sys.stdout.flush()")
        child = original([sys.executable, "-c", script], **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(diagnostic.subprocess, "Popen", start)
    monkeypatch.setattr(diagnostic, "MAX_JOURNAL_SECONDS", 0.05)
    monkeypatch.setattr(diagnostic, "MAX_JOURNAL_BYTES", 64)
    payload, status = diagnostic._journal_bytes("start", "end")
    assert payload is None and status == expected
    assert len(children) == 1 and children[0].poll() is not None


def test_service_and_gpu_outputs_are_strictly_allowlisted(monkeypatch: pytest.MonkeyPatch) -> None:
    private = b"PRIVATE_TOKEN"

    def command(args: list[str], **_kwargs):
        if args[0] == "systemctl":
            return b"ActiveState=active\nNRestarts=0\nMemoryCurrent=104857600\nMemoryPeak=209715200\n" + private
        if args[0] == "ollama":
            return b"ollama version is 0.20.6\n"
        return b"6144, 496, 5499\n"

    monkeypatch.setattr(diagnostic, "_limited_command", command)
    service = diagnostic.service_evidence()
    gpu = diagnostic.gpu_evidence()
    assert service["SERVICE_STATE"] == "ACTIVE"
    assert service["SERVICE_RESTARTS"] == 0
    assert service["SERVICE_MEMORY_CURRENT_MIB"] == 100
    assert service["OLLAMA_VERSION"] == "0.20.6"
    assert gpu["GPU_MEMORY_TOTAL_MIB"] == 6144
    assert gpu["CPU_GPU_PLACEMENT"] == "UNKNOWN"
    assert private.decode() not in json.dumps({**service, **gpu})


@pytest.mark.asyncio
async def test_diagnostic_default_never_probes_and_optional_probe_is_separate(
        monkeypatch: pytest.MonkeyPatch) -> None:
    async def preflight(branch: str):
        assert branch == diagnostic.BRANCH
        return ({"VERDICT": "NON_GENERATIVE_PREFLIGHT_COMPLETE",
                 "PRODUCTION_QUALIFIED": False}, SimpleNamespace())

    calls = 0

    async def probe(report: dict[str, object], _config: object):
        nonlocal calls
        calls += 1
        report["MODEL_REQUEST_STARTED_AT_UTC"] = datetime.now(timezone.utc).isoformat()
        report["MODEL_REQUEST_ENDED_AT_UTC"] = datetime.now(timezone.utc).isoformat()
        return report

    monkeypatch.setattr(reliability, "_preflight", preflight)
    monkeypatch.setattr(reliability, "_probe", probe)
    monkeypatch.setattr(diagnostic, "service_evidence", lambda: {})
    monkeypatch.setattr(diagnostic, "gpu_evidence", lambda: {})
    monkeypatch.setattr(diagnostic, "journal_evidence", lambda *_: {"STATUS": "OK", "EVENTS": []})
    default = await diagnostic.evaluate()
    assert calls == 0 and default["PRODUCTION_QUALIFIED"] is False
    optional = await diagnostic.evaluate(probe=True)
    assert calls == 1 and optional["PRODUCTION_QUALIFIED"] is False
    assert optional["PROBE_JOURNAL"]["EVENTS"] == []


@pytest.mark.asyncio
async def test_preflight_branch_override_preserves_default_checkout_behavior(
        monkeypatch: pytest.MonkeyPatch) -> None:
    branches: list[str | None] = []
    monkeypatch.setattr(reliability, "_checkout_verified",
                        lambda branch=None: branches.append(branch))
    monkeypatch.setattr(reliability, "_local_model_config", lambda:
                        SimpleNamespace(ollama_base_url="http://127.0.0.1:11434",
                                        ollama_primary_model="fixture"))
    monkeypatch.setattr(reliability, "local_resource_evidence", lambda: {})
    monkeypatch.setattr(reliability, "_tcp_probe", lambda *_: False)
    await reliability._preflight()
    await reliability._preflight(diagnostic.BRANCH)
    assert branches == [None, diagnostic.BRANCH]


@pytest.mark.asyncio
async def test_probe_timestamps_exist_only_after_consent(monkeypatch: pytest.MonkeyPatch) -> None:
    from contextlib import nullcontext
    monkeypatch.setattr(reliability, "open_operator_terminal", lambda: nullcontext(object()))
    monkeypatch.setattr(reliability, "_consent", lambda *_: False)
    report = {"VERDICT": "NON_GENERATIVE_PREFLIGHT_COMPLETE",
              "MODEL_RESIDENCY": "NOT_LOADED", "HOST_MEMORY_AVAILABLE_MIB": 4000,
              "CGROUP_MEMORY_HEADROOM_MIB": None, "INFERENCE_ATTEMPTED": False}
    declined = await reliability._probe(report, SimpleNamespace())
    assert "MODEL_REQUEST_STARTED_AT_UTC" not in declined
    assert declined["INFERENCE_ATTEMPTED"] is False


@pytest.mark.asyncio
async def test_client_deadline_cancels_inflight_chat_without_response(
        monkeypatch: pytest.MonkeyPatch) -> None:
    cancelled = False

    class SlowClient:
        async def chat(self, *_args, **_kwargs):
            nonlocal cancelled
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                cancelled = True
                raise

    monkeypatch.setattr(lab, "FIRST_COLD_MODEL_SECONDS", 0.01)
    source = lab.OllamaBrowserActionSource(SlowClient(), "fixture", loaded=False)
    with pytest.raises(lab.BrowserRefusal, match="MODEL_TIMEOUT"):
        await source.propose("Look up a synthetic item", "ITEM-1",
                             reliability._synthetic_observation())
    assert cancelled is True
    assert source.calls == 1 and source.responses == 0
