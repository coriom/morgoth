"""Non-generative, bounded local Ollama resource preflight for browser lab."""

from __future__ import annotations

from pathlib import Path
import asyncio
import json
import os
import re
import time
from urllib.parse import urlsplit

import httpx


MAX_OLLAMA_METADATA_BYTES = 65536
METADATA_TOTAL_SECONDS = 12


class MetadataRefusal(ValueError):
    """Fixed non-disclosing metadata failure category."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _metadata_failure(exc: Exception) -> str:
    """Map local transport failures to fixed codes without exception text."""
    if isinstance(exc, MetadataRefusal):
        return exc.code
    if isinstance(exc, httpx.TimeoutException):
        return "TIMEOUT"
    if isinstance(exc, httpx.ConnectError):
        return "CONNECTION_FAILED"
    if isinstance(exc, httpx.HTTPError):
        return "TRANSPORT_FAILED"
    if isinstance(exc, (ValueError, TypeError, UnicodeError)):
        return "INVALID_JSON_OR_SCHEMA"
    return "UNAVAILABLE"


def _loopback_base(base_url: str) -> bool:
    """Accept only the local endpoint shape already used by operator probes."""
    try:
        parsed = urlsplit(base_url)
        return (parsed.scheme == "http" and
                parsed.hostname in {"127.0.0.1", "localhost", "::1"} and
                parsed.port is not None and parsed.username is None and
                parsed.password is None and parsed.path in {"", "/"} and
                not parsed.query and not parsed.fragment)
    except ValueError:
        return False


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Refuse duplicate keys in local service metadata."""
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate metadata key")
        value[key] = item
    return value


async def _bounded_metadata_get(client: httpx.AsyncClient, path: str) -> tuple[object, int]:
    """Read one fixed Ollama metadata endpoint with a response byte cap."""
    started = time.monotonic()
    async with client.stream("GET", path) as response:
        if response.is_redirect:
            raise MetadataRefusal("REDIRECT_REFUSED")
        if response.status_code != 200:
            raise MetadataRefusal("HTTP_4XX" if 400 <= response.status_code < 500 else
                                  "HTTP_5XX" if 500 <= response.status_code < 600 else
                                  "HTTP_UNEXPECTED")
        length = response.headers.get("content-length", "")
        if length.isdigit() and int(length) > MAX_OLLAMA_METADATA_BYTES:
            raise MetadataRefusal("RESPONSE_OVERSIZE")
        body = bytearray()
        async for part in response.aiter_bytes():
            body.extend(part)
            if len(body) > MAX_OLLAMA_METADATA_BYTES:
                raise MetadataRefusal("RESPONSE_OVERSIZE")
    elapsed_ms = min(30000, max(0, round((time.monotonic() - started) * 1000)))
    return json.loads(body, object_pairs_hook=_unique_pairs), elapsed_ms


async def _local_ollama_metadata_inner(base_url: str, model: str) -> dict[str, object]:
    """Read fixed local metadata endpoints with per-phase limits."""
    result: dict[str, object] = {
        "OLLAMA_TAGS_RESPONSIVE": False, "OLLAMA_PS_RESPONSIVE": False,
        "OLLAMA_TAGS_STATUS": "UNKNOWN", "OLLAMA_PS_STATUS": "UNKNOWN",
        "OLLAMA_TAGS_ELAPSED_MS": None, "OLLAMA_PS_ELAPSED_MS": None,
        "MODEL_INSTALLED": None, "MODEL_SIZE_MIB": None,
        "MODEL_RESIDENCY": "UNKNOWN", "MODEL_REPORTED_VRAM_MIB": None,
        "OTHER_LOADED_MODEL_COUNT": None,
        "COMPETING_INFERENCE_REQUESTS": "UNKNOWN", "CPU_GPU_PLACEMENT": "UNKNOWN",
    }
    if not _loopback_base(base_url) or not re.fullmatch(r"[A-Za-z0-9_.:/-]{1,80}", model):
        return result
    try:
        async with httpx.AsyncClient(base_url=base_url, timeout=httpx.Timeout(5, connect=2),
                                     trust_env=False, follow_redirects=False) as client:
            tags_started = time.monotonic()
            try:
                tags, tags_ms = await _bounded_metadata_get(client, "/api/tags")
                if (type(tags) is not dict or type(tags.get("models")) is not list or
                        len(tags["models"]) > 64 or
                        any(type(item) is not dict or type(item.get("name")) is not str or
                            type(item.get("model", "")) is not str
                            for item in tags["models"])):
                    raise MetadataRefusal("INVALID_SCHEMA")
            except (httpx.HTTPError, OSError, ValueError, TypeError, UnicodeError) as exc:
                result["OLLAMA_TAGS_STATUS"] = _metadata_failure(exc)
                result["OLLAMA_TAGS_ELAPSED_MS"] = min(30000, max(0, round(
                    (time.monotonic() - tags_started) * 1000)))
                return result
            result["OLLAMA_TAGS_RESPONSIVE"] = True
            result["OLLAMA_TAGS_STATUS"] = "OK"
            result["OLLAMA_TAGS_ELAPSED_MS"] = tags_ms
            names = {model, model + ":latest"}
            installed = next((item for item in tags["models"] if
                              item.get("name") in names or item.get("model") in names), None)
            result["MODEL_INSTALLED"] = installed is not None
            if installed is not None:
                result["MODEL_SIZE_MIB"] = _mib(installed.get("size"))
            ps_started = time.monotonic()
            try:
                ps, ps_ms = await _bounded_metadata_get(client, "/api/ps")
                if (type(ps) is not dict or type(ps.get("models")) is not list or
                        len(ps["models"]) > 64 or
                        any(type(item) is not dict or type(item.get("name")) is not str or
                            type(item.get("model", "")) is not str
                            for item in ps["models"])):
                    raise MetadataRefusal("INVALID_SCHEMA")
            except (httpx.HTTPError, OSError, ValueError, TypeError, UnicodeError) as exc:
                result["OLLAMA_PS_STATUS"] = _metadata_failure(exc)
                result["OLLAMA_PS_ELAPSED_MS"] = min(30000, max(0, round(
                    (time.monotonic() - ps_started) * 1000)))
                return result
            result["OLLAMA_PS_RESPONSIVE"] = True
            result["OLLAMA_PS_STATUS"] = "OK"
            result["OLLAMA_PS_ELAPSED_MS"] = ps_ms
            loaded = next((item for item in ps["models"] if
                           item.get("name") in names or item.get("model") in names), None)
            result["MODEL_RESIDENCY"] = "LOADED" if loaded is not None else "NOT_LOADED"
            result["MODEL_REPORTED_VRAM_MIB"] = (
                _mib(loaded.get("size_vram")) if loaded is not None else None)
            result["OTHER_LOADED_MODEL_COUNT"] = min(64, len(ps["models"]) -
                                                      (1 if loaded is not None else 0))
    except (httpx.HTTPError, OSError, ValueError, UnicodeError, TypeError) as exc:
        if result["OLLAMA_TAGS_STATUS"] == "UNKNOWN":
            result["OLLAMA_TAGS_STATUS"] = _metadata_failure(exc)
    return result


async def local_ollama_metadata(base_url: str, model: str) -> dict[str, object]:
    """Return bounded metadata or a fixed total-deadline failure."""
    try:
        return await asyncio.wait_for(_local_ollama_metadata_inner(base_url, model),
                                      timeout=METADATA_TOTAL_SECONDS)
    except TimeoutError:
        return {
            "OLLAMA_TAGS_RESPONSIVE": False, "OLLAMA_PS_RESPONSIVE": False,
            "OLLAMA_TAGS_STATUS": "TOTAL_TIMEOUT", "OLLAMA_PS_STATUS": "UNKNOWN",
            "OLLAMA_TAGS_ELAPSED_MS": None, "OLLAMA_PS_ELAPSED_MS": None,
            "MODEL_INSTALLED": None, "MODEL_SIZE_MIB": None,
            "MODEL_RESIDENCY": "UNKNOWN", "MODEL_REPORTED_VRAM_MIB": None,
            "OTHER_LOADED_MODEL_COUNT": None,
            "COMPETING_INFERENCE_REQUESTS": "UNKNOWN", "CPU_GPU_PLACEMENT": "UNKNOWN",
        }


def _psi_avg10(raw: str, label: str) -> float | None:
    """Parse only a bounded PSI average; missing or malformed means unknown."""
    if label not in {"some", "full"}:
        return None
    match = re.search(rf"^{label} avg10=(\d+(?:\.\d+)?)", raw[:256], re.M)
    if match is None:
        return None
    value = float(match.group(1))
    return round(value, 2) if 0 <= value <= 100 else None


def local_resource_evidence() -> dict[str, object]:
    """Read only bounded Linux memory and fixed-name process metadata."""
    result: dict[str, object] = {
        "HOST_MEMORY_AVAILABLE_MIB": None, "HOST_SWAP_TOTAL_MIB": None,
        "HOST_SWAP_FREE_MIB": None, "MEMORY_PRESSURE_AVG10_PCT": None,
        "MEMORY_PRESSURE_FULL_AVG10_PCT": None,
        "CGROUP_MEMORY_PRESSURE_SOME_AVG10_PCT": None,
        "CGROUP_MEMORY_PRESSURE_FULL_AVG10_PCT": None,
        "CGROUP_MEMORY_LIMIT_MIB": None, "CGROUP_MEMORY_CURRENT_MIB": None,
        "CGROUP_MEMORY_HEADROOM_MIB": None, "CGROUP_MEMORY_LIMIT_KIND": "UNKNOWN",
        "CGROUP_SWAP_LIMIT_MIB": None, "CGROUP_SWAP_CURRENT_MIB": None,
        "OLLAMA_PROCESS_COUNT": None, "MODEL_RUNNER_PROCESS_COUNT": None,
        "PROCESS_SCAN_COMPLETE": False,
    }
    try:
        with Path("/proc/meminfo").open() as stream:
            lines = stream.read(4096).splitlines()
        for field, output in (("MemAvailable", "HOST_MEMORY_AVAILABLE_MIB"),
                              ("SwapTotal", "HOST_SWAP_TOTAL_MIB"),
                              ("SwapFree", "HOST_SWAP_FREE_MIB")):
            match = next((re.fullmatch(rf"{field}:\s+(\d+) kB", line)
                          for line in lines if line.startswith(field + ":")), None)
            if match is not None:
                result[output] = _mib(int(match.group(1)) * 1024)
    except (OSError, ValueError):
        pass
    try:
        pressure = Path("/proc/pressure/memory").read_text()[:256]
        for label, field in (("some", "MEMORY_PRESSURE_AVG10_PCT"),
                             ("full", "MEMORY_PRESSURE_FULL_AVG10_PCT")):
            result[field] = _psi_avg10(pressure, label)
    except (OSError, ValueError):
        pass
    try:
        cgroup = next((row.split(":", 2)[2] for row in
                       Path("/proc/self/cgroup").read_text()[:4096].splitlines()
                       if row.startswith("0::")), "")
        if not cgroup.startswith("/") or ".." in Path(cgroup).parts:
            raise ValueError("invalid cgroup")
        scope = Path("/sys/fs/cgroup").joinpath(*Path(cgroup).parts[1:])
        try:
            local_pressure = (scope / "memory.pressure").read_text()[:256]
            for label, field in (("some", "CGROUP_MEMORY_PRESSURE_SOME_AVG10_PCT"),
                                 ("full", "CGROUP_MEMORY_PRESSURE_FULL_AVG10_PCT")):
                result[field] = _psi_avg10(local_pressure, label)
        except (OSError, ValueError):
            pass
        limit = (scope / "memory.max").read_text()[:32].strip()
        current = int((scope / "memory.current").read_text()[:32].strip())
        result["CGROUP_MEMORY_CURRENT_MIB"] = _mib(current)
        if limit == "max":
            result["CGROUP_MEMORY_LIMIT_KIND"] = "UNLIMITED"
        else:
            ceiling = int(limit)
            result["CGROUP_MEMORY_LIMIT_KIND"] = "FINITE"
            result["CGROUP_MEMORY_LIMIT_MIB"] = _mib(ceiling)
            result["CGROUP_MEMORY_HEADROOM_MIB"] = _mib(max(0, ceiling - current))
        swap_limit = (scope / "memory.swap.max").read_text()[:32].strip()
        result["CGROUP_SWAP_CURRENT_MIB"] = _mib(int((scope / "memory.swap.current").read_text()[:32].strip()))
        if swap_limit != "max":
            result["CGROUP_SWAP_LIMIT_MIB"] = _mib(int(swap_limit))
    except (OSError, ValueError):
        pass
    try:
        ollama = runner = 0
        scanned = 0
        with os.scandir("/proc") as entries:
            for entry in entries:
                if not entry.name.isdigit():
                    continue
                scanned += 1
                if scanned > 4096:
                    return result
                try:
                    with open(f"/proc/{entry.name}/comm", "rb") as stream:
                        name = stream.read(32).strip()
                except OSError:
                    continue
                ollama += name == b"ollama"
                runner += name in {b"ollama-runner", b"llama-server"}
        result["OLLAMA_PROCESS_COUNT"] = min(4096, ollama)
        result["MODEL_RUNNER_PROCESS_COUNT"] = min(4096, runner)
        result["PROCESS_SCAN_COMPLETE"] = True
    except OSError:
        pass
    return result


def _mib(value: object) -> int | None:
    """Convert a safe byte count into a bounded MiB count."""
    return value // 1048576 if type(value) is int and 0 <= value <= 10**12 else None


def _memory_evidence() -> dict[str, int | None]:
    """Read resource headroom numbers only; no process or environment data."""
    available = None
    headroom = None
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                parts = line.split()
                if len(parts) == 3 and parts[2] == "kB":
                    available = _mib(int(parts[1]) * 1024)
                break
    except (OSError, ValueError):
        pass
    try:
        cgroup = next((row.split(":", 2)[2] for row in
                       Path("/proc/self/cgroup").read_text().splitlines()
                       if row.startswith("0::")), "")
        parts = Path(cgroup).parts
        if not cgroup.startswith("/") or ".." in parts:
            raise ValueError("invalid cgroup location")
        scope = Path("/sys/fs/cgroup").joinpath(*parts[1:])
        limit = (scope / "memory.max").read_text().strip()
        used = int((scope / "memory.current").read_text().strip())
        if limit != "max":
            headroom = _mib(max(0, int(limit) - used))
    except (OSError, ValueError):
        pass
    return {"HOST_MEMORY_AVAILABLE_MIB": available,
            "CGROUP_MEMORY_HEADROOM_MIB": headroom}


async def loaded_model_evidence(base_url: str, model: str) -> dict[str, object]:
    """Inspect /api/ps on the already validated loopback endpoint only."""
    result: dict[str, object] = {"MODEL_RESIDENCY": "UNKNOWN",
                                 "MODEL_REPORTED_VRAM_MIB": None}
    try:
        parsed = urlsplit(base_url)
    except ValueError:
        return result
    if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.username or parsed.password or parsed.path not in {"", "/"}
            or parsed.query or parsed.fragment):
        return result
    try:
        async with httpx.AsyncClient(base_url=base_url, timeout=5,
                                     trust_env=False, follow_redirects=False) as client:
            async with client.stream("GET", "/api/ps") as response:
                length = response.headers.get("content-length", "0")
                if (response.status_code != 200 or
                        (length.isdigit() and int(length) > 65536)):
                    return result
                body = bytearray()
                async for part in response.aiter_bytes():
                    body.extend(part)
                    if len(body) > 65536:
                        return result
        import json
        payload = json.loads(body)
        if type(payload) is not dict or type(payload.get("models")) is not list or len(payload["models"]) > 64:
            return result
        listed = payload["models"]
        for item in listed:
            if type(item) is not dict:
                return result
        selected = next((item for item in listed if item.get("name") in {model, model + ":latest"} or item.get("model") in {model, model + ":latest"}), None)
        result["MODEL_RESIDENCY"] = "LOADED" if selected is not None else "NOT_LOADED"
        if selected is not None:
            result["MODEL_REPORTED_VRAM_MIB"] = _mib(selected.get("size_vram"))
    except (httpx.HTTPError, OSError, ValueError, UnicodeError):
        pass
    return result
