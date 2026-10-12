"""One bounded, non-inference Codex doctor run in the production confinement.

Doctor's complete report and subprocess output are private, in-memory inputs.
Only fixed status codes and numeric HTTP classes may leave this module.
"""
from __future__ import annotations

import json
import re
import subprocess
import tempfile

from core.llm import codex_cli as cli
from scripts.diagnose_codex_cli import preflight, preflight_text_runtime


PROXY_NAMES = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
               "http_proxy", "https_proxy", "all_proxy", "no_proxy")
STATUS = frozenset({"ok", "warning", "fail"})
REACHABLE = re.compile(r"^https://chatgpt\.com/backend-api/codex/responses reachable \(HTTP ([1-5][0-9]{2})\)$")
HANDSHAKE = re.compile(r"^HTTP ([1-5][0-9]{2})$")


def _base(exit_code: int | None) -> dict[str, object]:
    """Return only the permitted public fields, including on failure."""
    return {
        "CODEX_VERSION": "0.162.1",
        "DOCTOR_EXIT": exit_code if isinstance(exit_code, int) and -255 <= exit_code <= 255 else None,
        "DOCTOR_JSON_VALID": False,
        "HTTPS_CHECK_PRESENT": False,
        "HTTPS_CHECK_STATUS": "MISSING",
        "WEBSOCKET_CHECK_PRESENT": False,
        "WEBSOCKET_CHECK_STATUS": "MISSING",
        "WEBSOCKET_ENABLED": None,
        "HTTPS_HTTP_STATUS_CLASS": None,
        "WEBSOCKET_HANDSHAKE_STATUS_CLASS": None,
        "WEBSOCKET_IMMEDIATE_CLOSE": None,
        "PROXY_ENV_NAME_PRESENCE": {name: False for name in PROXY_NAMES},
    }


def _detail(check: dict[str, object], key: str) -> str | None:
    """Read one fixed detail key without returning arbitrary report content."""
    details = check.get("details")
    value = details.get(key) if isinstance(details, dict) else None
    return value if isinstance(value, str) and len(value) < 1024 else None


def _http_class(status: int) -> str:
    return f"{status // 100}XX"


def reduce_doctor(stdout: str, exit_code: int | None) -> dict[str, object]:
    """Reduce only two known 0.162.1 check IDs; fail closed on bad structure."""
    report = _base(exit_code)
    if not isinstance(stdout, str) or len(stdout) > 1_000_000:
        report["VERDICT"] = "CODEX_DOCTOR_INCONCLUSIVE"
        return report
    try:
        document = json.loads(stdout)
    except (ValueError, TypeError):
        report["VERDICT"] = "CODEX_DOCTOR_INCONCLUSIVE"
        return report
    if (not isinstance(document, dict) or document.get("schemaVersion") != 1
            or document.get("codexVersion") != "0.162.1"
            or not isinstance(document.get("checks"), dict)):
        report["VERDICT"] = "CODEX_DOCTOR_INCONCLUSIVE"
        return report
    report["DOCTOR_JSON_VALID"] = True
    checks = document["checks"]
    https = checks.get("network.provider_reachability")
    ws = checks.get("network.websocket_reachability")
    if isinstance(https, dict) and https.get("id") == "network.provider_reachability":
        report["HTTPS_CHECK_PRESENT"] = True
        status = https.get("status")
        report["HTTPS_CHECK_STATUS"] = status.upper() if status in STATUS else "INVALID"
        # The tagged doctor labels its source-defined ChatGPT endpoint this way.
        # A 401/403 demonstrates reachability, not valid authentication.
        value = _detail(https, "ChatGPT inference URL")
        match = REACHABLE.fullmatch(value or "")
        if match:
            code = int(match.group(1))
            report["HTTPS_HTTP_STATUS_CLASS"] = _http_class(code)
            report["HTTPS_CHECK_STATUS"] = ("REACHABLE_UNAUTHENTICATED" if code in (401, 403)
                                             else "REACHABLE" if status == "ok" else "INVALID")
    if isinstance(ws, dict) and ws.get("id") == "network.websocket_reachability":
        report["WEBSOCKET_CHECK_PRESENT"] = True
        status = ws.get("status")
        report["WEBSOCKET_CHECK_STATUS"] = status.upper() if status in STATUS else "INVALID"
        supported = _detail(ws, "supports websockets")
        if supported in ("true", "false"):
            report["WEBSOCKET_ENABLED"] = supported == "true"
        handshake = HANDSHAKE.fullmatch(_detail(ws, "handshake result") or "")
        if handshake:
            report["WEBSOCKET_HANDSHAKE_STATUS_CLASS"] = _http_class(int(handshake.group(1)))
        report["WEBSOCKET_IMMEDIATE_CLOSE"] = _detail(ws, "immediate close code") is not None
        if supported == "false":
            report["WEBSOCKET_CHECK_STATUS"] = "UNAVAILABLE"
        elif status == "ok" and ws.get("summary") == "Responses WebSocket handshake succeeded" and handshake:
            report["WEBSOCKET_CHECK_STATUS"] = "HANDSHAKE_OK"
        elif ws.get("summary") == "Responses WebSocket closed immediately after handshake":
            report["WEBSOCKET_CHECK_STATUS"] = "IMMEDIATE_CLOSE"
    if (not report["HTTPS_CHECK_PRESENT"] or not report["WEBSOCKET_CHECK_PRESENT"]
            or report["HTTPS_CHECK_STATUS"] in ("INVALID", "MISSING", "OK")
            or report["WEBSOCKET_CHECK_STATUS"] in ("INVALID", "MISSING")
            or report["WEBSOCKET_ENABLED"] is None):
        verdict = "CODEX_DOCTOR_INCONCLUSIVE"
    elif report["HTTPS_CHECK_STATUS"] not in ("REACHABLE", "REACHABLE_UNAUTHENTICATED"):
        verdict = "CODEX_HTTPS_REACHABILITY_BLOCKED"
    elif report["WEBSOCKET_CHECK_STATUS"] != "HANDSHAKE_OK":
        verdict = "CODEX_WEBSOCKET_REACHABILITY_BLOCKED"
    else:
        verdict = "CODEX_NON_INFERENCE_CONNECTIVITY_PASS"
    report["VERDICT"] = verdict
    return report


def run_once() -> dict[str, object]:
    """Run at most one doctor process through the exact production sandbox."""
    binary, env = preflight()
    preflight_text_runtime(binary, env)
    with tempfile.TemporaryDirectory(prefix="morgoth-codex-check-", dir="/tmp") as cwd:
        # Doctor checks configured MCP HTTP endpoints. Prove the exact isolated
        # config resolves to no servers before granting it any network access.
        mcp = cli._run([binary, "mcp", "list", "--json"], cwd, env, "", 10)
        if mcp.returncode != 0 or len(mcp.stdout) > 65_536:
            raise RuntimeError("PREFLIGHT_MCP_CONFIG")
        try:
            servers = json.loads(mcp.stdout)
        except (ValueError, TypeError):
            raise RuntimeError("PREFLIGHT_MCP_CONFIG") from None
        if servers not in ([], {}):
            raise RuntimeError("PREFLIGHT_MCP_CONFIG")
    with tempfile.TemporaryDirectory(prefix="morgoth-codex-check-", dir="/tmp") as cwd:
        # The host config and repository are not mounted. The only Codex home
        # entry is the read-only auth file, so no user MCP commands can load.
        result = cli._run([binary, "doctor", "--json"], cwd, env, "", 90)
    report = reduce_doctor(result.stdout, result.returncode)
    # Bubblewrap --clearenv means no proxy values or names reach doctor.
    # This reports the *child* environment, never the operator environment.
    return report


if __name__ == "__main__":
    try:
        print(json.dumps(run_once(), sort_keys=True))
    except (cli.CodexCliError, RuntimeError, OSError, subprocess.SubprocessError):
        report = _base(None)
        report["VERDICT"] = "CODEX_DOCTOR_INCONCLUSIVE"
        print(json.dumps(report, sort_keys=True))
