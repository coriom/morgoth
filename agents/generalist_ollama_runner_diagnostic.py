"""Bounded, non-disclosing Ollama runner diagnostics and optional consented probe.

No journal message, command output, model prompt, or response is emitted. The
default mode makes only read-only local metadata requests and system queries.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import selectors
import subprocess
import sys
import time

from loguru import logger

from agents import generalist_ollama_reliability as reliability


BRANCH = "morgoth-generalist-ollama-runner-diagnostics-v1"
MAX_JOURNAL_BYTES = 2_000_000
MAX_JOURNAL_RECORDS = 1000
MAX_JOURNAL_SECONDS = 8
HISTORICAL_WINDOWS = (
    ("2026-10-22 10:04:30", "2026-10-22 10:07:30"),
    ("2026-10-22 10:55:30", "2026-10-22 10:58:00"),
)


def classify_message(message: object) -> list[str]:
    """Return fixed evidence classes, never a substring of the journal text."""
    if type(message) is not str or len(message) > 16384:
        return []
    lower = message.lower()
    classes: list[str] = []
    if re.search(r"(?i)\bCUDA_ERROR_[A-Z0-9_]+\b", message):
        classes.append("CUDA_ERROR_CODE_REPORTED")
    elif "cuda" in lower and ("error" in lower or "fail" in lower):
        classes.append("CUDA_MENTION_IN_FAILURE")
    if "load" in lower and ("error" in lower or "fail" in lower):
        classes.append("MODEL_LOAD_FAILURE_REPORTED")
    if "cancel" in lower:
        classes.append("CANCELLATION_REPORTED")
    if "timeout" in lower or "timed out" in lower or "deadline exceeded" in lower:
        classes.append("TIMEOUT_REPORTED")
    if "runner" in lower and ("error" in lower or "fail" in lower):
        classes.append("RUNNER_FAILURE_REPORTED")
    if "runner" in lower and ("start" in lower or "launch" in lower):
        classes.append("RUNNER_START_PHASE_REPORTED")
    if "runner" in lower and "load" in lower and not ("error" in lower or "fail" in lower):
        classes.append("RUNNER_LOAD_PHASE_REPORTED")
    if ("runner" in lower and re.search(r"\bready\b", lower) and
            not any(marker in lower for marker in
                    ("not ready", "waiting", "error", "fail", "timeout", "cancel"))):
        classes.append("RUNNER_READY_REPORTED")
    if ("out of memory" in lower or "oom.kill" in lower or
            "cuda_error_out_of_memory" in lower):
        classes.append("EXPLICIT_OOM_REPORTED")
    return classes


def _limited_command(args: list[str], *, timeout: int = 5,
                     limit: int = 8192) -> bytes | None:
    """Run a fixed read-only command and discard nonconforming output."""
    try:
        result = subprocess.run(args, capture_output=True, timeout=timeout,
                                check=False)
        if result.returncode or len(result.stdout) > limit:
            return None
        return result.stdout
    except (OSError, subprocess.SubprocessError):
        return None


def service_evidence() -> dict[str, object]:
    """Read service lifecycle properties without service configuration or logs."""
    result: dict[str, object] = {"SERVICE_STATE": "UNKNOWN", "SERVICE_RESTARTS": None,
                                 "SERVICE_MEMORY_CURRENT_MIB": None,
                                 "SERVICE_MEMORY_PEAK_MIB": None,
                                 "SERVICE_CGROUP_OOM_COUNT": None,
                                 "SERVICE_CGROUP_OOM_KILL_COUNT": None,
                                 "OLLAMA_VERSION": "UNKNOWN", "CUDA_BACKEND_PRESENT": None}
    data = _limited_command(["systemctl", "show", "ollama.service", "--no-pager",
                             "-p", "ActiveState", "-p", "NRestarts", "-p",
                             "MemoryCurrent", "-p", "MemoryPeak"])
    if data is not None:
        fields = dict(line.split("=", 1) for line in data.decode("ascii", "ignore").splitlines()
                      if "=" in line)
        if fields.get("ActiveState") in {"active", "inactive", "failed"}:
            result["SERVICE_STATE"] = fields["ActiveState"].upper()
        if fields.get("NRestarts", "").isdigit():
            result["SERVICE_RESTARTS"] = min(1000000, int(fields["NRestarts"]))
        for field, key in (("MemoryCurrent", "SERVICE_MEMORY_CURRENT_MIB"),
                           ("MemoryPeak", "SERVICE_MEMORY_PEAK_MIB")):
            if fields.get(field, "").isdigit():
                result[key] = min(1000000, int(fields[field]) // 1048576)
    version = _limited_command(["ollama", "--version"], timeout=3, limit=128)
    if version is not None:
        match = re.fullmatch(rb"ollama version is (\d{1,3}\.\d{1,3}\.\d{1,3})\s*", version)
        if match:
            result["OLLAMA_VERSION"] = match.group(1).decode("ascii")
    if (Path("/usr/local/lib/ollama/cuda_v12/libggml-cuda.so").is_file() or
            Path("/usr/local/lib/ollama/cuda_v13/libggml-cuda.so").is_file()):
        result["CUDA_BACKEND_PRESENT"] = True
    try:
        events = Path("/sys/fs/cgroup/system.slice/ollama.service/memory.events").read_text()[:512]
        for line in events.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1].isdigit() and parts[0] in {"oom", "oom_kill"}:
                key = ("SERVICE_CGROUP_OOM_COUNT" if parts[0] == "oom" else
                       "SERVICE_CGROUP_OOM_KILL_COUNT")
                result[key] = min(1000000, int(parts[1]))
    except OSError:
        pass
    return result


def gpu_evidence() -> dict[str, object]:
    """Read only bounded aggregate GPU memory; placement remains unproved."""
    result: dict[str, object] = {"GPU_MEMORY_TOTAL_MIB": None,
                                 "GPU_MEMORY_USED_MIB": None,
                                 "GPU_MEMORY_FREE_MIB": None,
                                 "CPU_GPU_PLACEMENT": "UNKNOWN"}
    data = _limited_command(["/usr/lib/wsl/lib/nvidia-smi",
                             "--query-gpu=memory.total,memory.used,memory.free",
                             "--format=csv,noheader,nounits"], timeout=4, limit=128)
    if data is None:
        return result
    match = re.fullmatch(rb"\s*(\d{1,6}),\s*(\d{1,6}),\s*(\d{1,6})\s*", data)
    if match:
        result.update(zip(("GPU_MEMORY_TOTAL_MIB", "GPU_MEMORY_USED_MIB",
                           "GPU_MEMORY_FREE_MIB"), (int(item) for item in match.groups())))
    return result


def _journal_bytes(start: str, end: str) -> tuple[bytes | None, str]:
    """Read a fixed unit's JSON records with a hard byte and wall-time cap."""
    args = ["journalctl", "--unit=ollama.service", f"--since={start}",
            f"--until={end}", "--output=json", "--no-pager", "-n",
            str(MAX_JOURNAL_RECORDS)]
    try:
        process = subprocess.Popen(args, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL)
    except OSError:
        return None, "UNAVAILABLE"
    chunks = bytearray()
    deadline = time.monotonic() + MAX_JOURNAL_SECONDS
    try:
        assert process.stdout is not None
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None, "TIMEOUT"
                ready = selector.select(remaining)
                if not ready:
                    return None, "TIMEOUT"
                block = os.read(process.stdout.fileno(), 4096)
                if not block:
                    break
                chunks.extend(block)
                if len(chunks) > MAX_JOURNAL_BYTES:
                    return None, "OVERSIZE"
        if process.wait(timeout=max(0.01, deadline - time.monotonic())) != 0:
            return None, "UNAVAILABLE"
        return bytes(chunks), "OK"
    except (OSError, subprocess.SubprocessError):
        return None, "UNAVAILABLE"
    finally:
        if process.poll() is None:
            try:
                process.kill()
                process.wait(timeout=2)
            except (OSError, subprocess.SubprocessError):
                pass
        if process.stdout is not None:
            process.stdout.close()


def journal_evidence(start: str, end: str) -> dict[str, object]:
    """Classify a bounded time window, retaining only UTC timestamps and codes."""
    data, status = _journal_bytes(start, end)
    result: dict[str, object] = {"STATUS": status, "RECORDS_SCANNED": 0,
                                 "EVENTS": [], "TRUNCATED_BY_RECORD_LIMIT": False}
    if data is None:
        return result
    rows = data.splitlines()
    result["RECORDS_SCANNED"] = len(rows)
    result["TRUNCATED_BY_RECORD_LIMIT"] = len(rows) >= MAX_JOURNAL_RECORDS
    events: list[dict[str, object]] = []
    for row in rows:
        try:
            record = json.loads(row)
            if type(record) is not dict:
                continue
            classes = classify_message(record.get("MESSAGE"))
            micros = record.get("__REALTIME_TIMESTAMP")
            if not classes or type(micros) is not str or not micros.isdigit():
                continue
            timestamp = datetime.fromtimestamp(int(micros) / 1_000_000,
                                              timezone.utc).isoformat(timespec="milliseconds")
            events.append({"UTC": timestamp, "CLASSES": classes})
        except (ValueError, OverflowError, TypeError):
            continue
        if len(events) >= 64:
            break
    result["EVENTS"] = events
    return result


async def evaluate(probe: bool = False) -> dict[str, object]:
    """Gather non-generative evidence; optionally run one foreground-consented call."""
    logger.remove()
    report, config = await reliability._preflight(BRANCH)
    report.update(service_evidence())
    report.update(gpu_evidence())
    report["HISTORICAL_JOURNAL"] = [
        {"WINDOW_LOCAL": [start, end], **journal_evidence(start, end)}
        for start, end in HISTORICAL_WINDOWS]
    if probe and config is not None:
        report = await reliability._probe(report, config)
        post_gpu = gpu_evidence()
        report["GPU_MEMORY_USED_MIB_AFTER"] = post_gpu.get("GPU_MEMORY_USED_MIB")
        report["GPU_MEMORY_FREE_MIB_AFTER"] = post_gpu.get("GPU_MEMORY_FREE_MIB")
        post_service = service_evidence()
        report["SERVICE_CGROUP_OOM_COUNT_AFTER"] = post_service.get("SERVICE_CGROUP_OOM_COUNT")
        report["SERVICE_CGROUP_OOM_KILL_COUNT_AFTER"] = post_service.get(
            "SERVICE_CGROUP_OOM_KILL_COUNT")
        start = report.get("MODEL_REQUEST_STARTED_AT_UTC")
        end = report.get("MODEL_REQUEST_ENDED_AT_UTC")
        if type(start) is str and type(end) is str:
            try:
                first = datetime.fromisoformat(start) - timedelta(seconds=2)
                last = datetime.fromisoformat(end) + timedelta(seconds=2)
                if timedelta(0) <= last - first <= timedelta(minutes=3):
                    report["PROBE_JOURNAL"] = journal_evidence(
                        first.astimezone().strftime("%Y-%m-%d %H:%M:%S"),
                        last.astimezone().strftime("%Y-%m-%d %H:%M:%S"))
            except ValueError:
                pass
    report["PRODUCTION_QUALIFIED"] = False
    return report


def main() -> None:
    """Emit fixed, non-sensitive JSON without inferring a root cause."""
    if sys.argv[1:] not in ([], ["--probe"]):
        report: dict[str, object] = {"VERDICT": "INVALID_INVOCATION",
                                     "PRODUCTION_QUALIFIED": False}
    else:
        report = asyncio.run(evaluate(probe=sys.argv[1:] == ["--probe"]))
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
