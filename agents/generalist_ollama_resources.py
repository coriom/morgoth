"""Non-generative, bounded local Ollama resource preflight for browser lab."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit

import httpx


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
