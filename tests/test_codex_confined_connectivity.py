"""Hermetic doctor adapter checks; no Codex process or network access."""
from __future__ import annotations

import json
import subprocess

import pytest

from scripts import codex_confined_connectivity as doctor


SECRET = "TOP_SECRET_DO_NOT_PERSIST"


def fixture(*, http: int = 200, ws_status: str = "ok", ws_summary: str =
            "Responses WebSocket handshake succeeded", enabled: bool = True) -> str:
    """Create only the tagged fields the reducer may inspect."""
    return json.dumps({
        "schemaVersion": 1, "codexVersion": "0.162.1",
        "checks": {
            "network.provider_reachability": {
                "id": "network.provider_reachability", "status": "ok",
                "summary": doctor.HTTPS_SUCCESS_SUMMARY,
                "details": {"ChatGPT inference URL":
                            f"https://chatgpt.com/backend-api/codex/responses reachable (HTTP {http})",
                            "other": SECRET},
            },
            "network.websocket_reachability": {
                "id": "network.websocket_reachability", "status": ws_status,
                "summary": ws_summary,
                "details": {"supports websockets": str(enabled).lower(),
                            "handshake result": "HTTP 101",
                            "other": SECRET},
            },
            "mcp.config": {"details": SECRET},
        },
        "secret": SECRET,
    })


def test_success_and_nonzero_overall_exit() -> None:
    report = doctor.reduce_doctor(fixture(), 1)
    assert report["VERDICT"] == "CODEX_NON_INFERENCE_CONNECTIVITY_PASS"
    assert report["DOCTOR_EXIT"] == 1
    assert report["HTTPS_HTTP_STATUS_CLASS"] == "2XX"
    assert report["WEBSOCKET_HANDSHAKE_STATUS_CLASS"] == "1XX"
    assert SECRET not in json.dumps(report)


def test_redacted_success_has_status_only_not_invented_http_codes() -> None:
    """The tagged JSON output can redact URL and numeric handshake details."""
    data = json.loads(fixture())
    data["checks"]["network.provider_reachability"]["details"] = {
        "ChatGPT inference URL": "[redacted]", "other": SECRET}
    data["checks"]["network.websocket_reachability"]["details"] = {
        "other": SECRET}
    report = doctor.reduce_doctor(json.dumps(data), 0)
    assert report["VERDICT"] == "CODEX_NON_INFERENCE_CONNECTIVITY_PASS"
    assert report["HTTPS_CHECK_STATUS"] == "REACHABLE_STATUS_ONLY"
    assert report["WEBSOCKET_CHECK_STATUS"] == "HANDSHAKE_STATUS_ONLY"
    assert report["WEBSOCKET_ENABLED"] is True
    assert report["HTTPS_HTTP_STATUS_CLASS"] is None
    assert report["WEBSOCKET_HANDSHAKE_STATUS_CLASS"] is None
    assert SECRET not in json.dumps(report)


def test_ok_without_endpoint_probe_is_not_reachability_success() -> None:
    data = json.loads(fixture())
    data["checks"]["network.provider_reachability"]["summary"] = (
        "active provider has no HTTP endpoint to probe")
    data["checks"]["network.provider_reachability"]["details"] = {}
    assert doctor.reduce_doctor(json.dumps(data), 0)["VERDICT"] == "CODEX_DOCTOR_INCONCLUSIVE"


def test_ok_websocket_without_success_summary_is_not_a_handshake() -> None:
    data = json.loads(fixture())
    data["checks"]["network.websocket_reachability"]["summary"] = (
        "Responses WebSocket is not enabled for the active provider")
    data["checks"]["network.websocket_reachability"]["details"] = {}
    assert doctor.reduce_doctor(json.dumps(data), 0)["VERDICT"] == "CODEX_DOCTOR_INCONCLUSIVE"


def test_contradictory_websocket_success_details_fail_closed() -> None:
    data = json.loads(fixture())
    ws = data["checks"]["network.websocket_reachability"]
    ws["details"]["handshake result"] = "HTTP 500"
    assert doctor.reduce_doctor(json.dumps(data), 0)["VERDICT"] == "CODEX_DOCTOR_INCONCLUSIVE"
    ws["details"]["handshake result"] = "HTTP 101"
    ws["details"]["immediate close code"] = "1008"
    assert doctor.reduce_doctor(json.dumps(data), 0)["VERDICT"] == "CODEX_DOCTOR_INCONCLUSIVE"


@pytest.mark.parametrize("status", [401, 403])
def test_http_unauthenticated_is_reachable(status: int) -> None:
    report = doctor.reduce_doctor(fixture(http=status), 0)
    assert report["HTTPS_CHECK_STATUS"] == "REACHABLE_UNAUTHENTICATED"
    assert report["HTTPS_HTTP_STATUS_CLASS"] == "4XX"


def test_websocket_immediate_close() -> None:
    data = json.loads(fixture(ws_status="warning", ws_summary=
                              "Responses WebSocket closed immediately after handshake"))
    data["checks"]["network.websocket_reachability"]["details"]["immediate close code"] = "1008"
    report = doctor.reduce_doctor(json.dumps(data), 0)
    assert report["WEBSOCKET_IMMEDIATE_CLOSE"] is True
    assert report["VERDICT"] == "CODEX_WEBSOCKET_REACHABILITY_BLOCKED"


def test_websocket_unavailable_is_not_success() -> None:
    report = doctor.reduce_doctor(fixture(enabled=False), 0)
    assert report["WEBSOCKET_CHECK_STATUS"] == "UNAVAILABLE"
    assert report["VERDICT"] == "CODEX_WEBSOCKET_REACHABILITY_BLOCKED"


@pytest.mark.parametrize("payload", ["", "{", "[]", "{}", SECRET, "x" * 1_000_001])
def test_missing_or_malformed_json(payload: str) -> None:
    report = doctor.reduce_doctor(payload, 1)
    assert report["VERDICT"] == "CODEX_DOCTOR_INCONCLUSIVE"
    assert SECRET not in json.dumps(report)


def test_missing_required_check_is_inconclusive() -> None:
    data = json.loads(fixture())
    del data["checks"]["network.websocket_reachability"]
    report = doctor.reduce_doctor(json.dumps(data), 0)
    assert report["VERDICT"] == "CODEX_DOCTOR_INCONCLUSIVE"


def test_https_failure_and_unexpected_detail_rejected() -> None:
    data = json.loads(fixture())
    check = data["checks"]["network.provider_reachability"]
    check["status"] = "fail"
    check["details"]["ChatGPT inference URL"] = SECRET
    report = doctor.reduce_doctor(json.dumps(data), 1)
    assert report["VERDICT"] == "CODEX_HTTPS_REACHABILITY_BLOCKED"
    assert report["HTTPS_HTTP_STATUS_CLASS"] is None
    assert SECRET not in json.dumps(report)


def test_one_owned_doctor_command(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[object] = []
    monkeypatch.setattr(doctor, "preflight", lambda: ("/fixed/codex", {"HOME": "/operator"}))
    monkeypatch.setattr(doctor, "preflight_text_runtime", lambda *args: None)

    def fake_run(argv: list[str], cwd: str, env: dict[str, str], prompt: str,
                 timeout: float) -> subprocess.CompletedProcess[str]:
        seen.append((argv, prompt, timeout, cwd))
        if argv[1:3] == ["mcp", "list"]:
            return subprocess.CompletedProcess(argv, 0, "[]", SECRET)
        return subprocess.CompletedProcess(argv, 0, fixture(), SECRET)

    monkeypatch.setattr(doctor.cli, "_run", fake_run)
    report = doctor.run_once()
    assert len(seen) == 2
    assert seen[0][0] == ["/fixed/codex", "mcp", "list", "--json"]
    assert seen[1][0] == ["/fixed/codex", "doctor", "--json"]
    assert seen[1][1:3] == ("", 90)
    assert report["VERDICT"] == "CODEX_NON_INFERENCE_CONNECTIVITY_PASS"
    assert SECRET not in json.dumps(report)


def test_timeout_is_safe_and_cleanup_owned_by_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "preflight", lambda: ("/fixed/codex", {}))
    monkeypatch.setattr(doctor, "preflight_text_runtime", lambda *args: None)

    def timeout(*args: object) -> subprocess.CompletedProcess[str]:
        raise doctor.cli.CodexCliError("timeout")

    monkeypatch.setattr(doctor.cli, "_run", timeout)
    with pytest.raises(doctor.cli.CodexCliError) as exc:
        doctor.run_once()
    assert exc.value.code == "timeout"


def test_configured_mcp_stops_before_doctor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "preflight", lambda: ("/fixed/codex", {}))
    monkeypatch.setattr(doctor, "preflight_text_runtime", lambda *args: None)
    calls: list[list[str]] = []

    def fake_run(argv: list[str], *args: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, json.dumps([{"name": SECRET}]), "")

    monkeypatch.setattr(doctor.cli, "_run", fake_run)
    with pytest.raises(RuntimeError, match="PREFLIGHT_MCP_CONFIG"):
        doctor.run_once()
    assert len(calls) == 1
    assert calls[0][1:3] == ["mcp", "list"]
