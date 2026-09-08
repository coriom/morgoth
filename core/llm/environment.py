"""Environment-awareness: detect what LLM capabilities THIS machine has.

Contract: DETECT and RECOMMEND. The system never auto-writes .env,
never auto-selects a paid provider, never makes a paid API call just
to probe. The operator decides — this module just tells them what would
work.

All probes are non-fatal and bounded. A probe failure is a recorded
'unavailable' with a reason, not an exception into the caller. Wall-
clock budget: total detection < 3 s on a healthy box, < 8 s on a sick
one (Ollama tag-list is the tightest deadline; every other probe is
in-process).
"""

from __future__ import annotations

import asyncio
import os
import platform as _platform
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Literal


CapabilityStatus = Literal["ok", "unavailable", "degraded"]


@dataclass
class Capability:
    """One probe result. `detail` is a one-line human-readable reason."""
    status: CapabilityStatus
    detail: str
    facts: dict[str, object] = field(default_factory=dict)


@dataclass
class Environment:
    """Snapshot of the machine's LLM-relevant capabilities."""
    platform: str
    ollama: Capability
    hardware: Capability
    claude_cli: Capability
    api_key: Capability

    def to_lines(self) -> list[str]:
        lines = [f"PLATFORM: {self.platform}"]
        for name, cap in (
            ("ollama    ", self.ollama),
            ("hardware  ", self.hardware),
            ("claude-cli", self.claude_cli),
            ("api key   ", self.api_key),
        ):
            marker = {"ok": "[OK  ]", "degraded": "[WARN]", "unavailable": "[FAIL]"}[cap.status]
            lines.append(f"  {marker} {name}  {cap.detail}")
        return lines


# ─── individual probes ─────────────────────────────────────────────────

def _probe_platform() -> str:
    sys = _platform.system()
    rel = _platform.release()
    if "microsoft" in rel.lower() or "wsl" in rel.lower():
        return f"WSL ({rel})"
    return f"{sys} ({rel})"


def _probe_hardware() -> Capability:
    """RAM/CPU from /proc, GPU via nvidia-smi (both optional)."""
    facts: dict[str, object] = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    facts["ram_total_gb"] = round(int(line.split()[1]) / (1024 * 1024), 1)
                elif line.startswith("MemAvailable:"):
                    facts["ram_available_gb"] = round(int(line.split()[1]) / (1024 * 1024), 1)
    except OSError:
        pass
    facts["cpu_cores"] = os.cpu_count() or 1

    # GPU probe — nvidia-smi is the only ubiquitous free path. A missing
    # binary is CPU-only; a crashing one is 'gpu probe failed', treated
    # as CPU-only for routing purposes.
    if shutil.which("nvidia-smi"):
        try:
            out = subprocess.run(
                ["nvidia-smi", "--query-gpu=name,memory.total,memory.free",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=3,
            )
            if out.returncode == 0 and out.stdout.strip():
                first = out.stdout.strip().splitlines()[0]
                parts = [p.strip() for p in first.split(",")]
                if len(parts) >= 3:
                    facts["gpu_name"] = parts[0]
                    facts["vram_total_mb"] = int(parts[1])
                    facts["vram_free_mb"] = int(parts[2])
        except (subprocess.TimeoutExpired, OSError, ValueError):
            pass
    detail_parts = [
        f"{facts.get('ram_total_gb', '?')} GB RAM",
        f"{facts.get('cpu_cores', '?')} cores",
    ]
    if "vram_total_mb" in facts:
        detail_parts.append(f"GPU {facts['gpu_name']} ({facts['vram_total_mb']} MB)")
    else:
        detail_parts.append("CPU-only")
    return Capability(status="ok", detail=" · ".join(detail_parts), facts=facts)


async def _probe_ollama() -> Capability:
    """Reachable? Which models are pulled? Uses the OLLAMA_HOST env
    same way OllamaLLMClient does. Non-blocking, 2 s hard budget."""
    from core.config import load_config
    try:
        cfg = await load_config()
    except Exception as exc:
        return Capability("unavailable", f"config load failed: {type(exc).__name__}: {exc}")
    host = getattr(cfg, "ollama_host", None) or "http://localhost:11434"
    try:
        import httpx
        async with httpx.AsyncClient(timeout=2.0) as c:
            r = await c.get(f"{host.rstrip('/')}/api/tags")
        if r.status_code != 200:
            return Capability("unavailable", f"HTTP {r.status_code} at {host}/api/tags")
        payload = r.json()
        tags = [m.get("name") or m.get("model") for m in payload.get("models", [])]
        tags = [t for t in tags if t]
        primary = getattr(cfg, "primary_model", "") or ""
        has_primary = any(primary in t for t in tags)
        detail = f"{host} · {len(tags)} model(s) pulled"
        if primary:
            detail += f" · primary={primary} " + ("[PRESENT]" if has_primary else "[MISSING]")
        return Capability(
            "ok" if has_primary else "degraded",
            detail,
            facts={"host": host, "tags": tags, "primary_present": has_primary,
                   "primary": primary},
        )
    except Exception as exc:
        return Capability("unavailable", f"{type(exc).__name__} at {host}")


def _probe_claude_cli() -> Capability:
    """`claude` on PATH? Runnable? Non-network probe — just --version."""
    bin_path = shutil.which("claude")
    if not bin_path:
        return Capability("unavailable", "not on PATH (install Claude Code)")
    try:
        out = subprocess.run(
            [bin_path, "--version"], capture_output=True, text=True, timeout=3,
        )
        if out.returncode == 0:
            ver = out.stdout.strip().splitlines()[0] if out.stdout else "unknown"
            return Capability("ok", f"{bin_path} · {ver}",
                              facts={"binary": bin_path, "version": ver})
        return Capability("degraded", f"{bin_path} exit={out.returncode}")
    except (subprocess.TimeoutExpired, OSError) as exc:
        return Capability("degraded", f"probe failed: {type(exc).__name__}")


def _probe_api_key() -> Capability:
    """Presence-only. NEVER reads the value beyond bool(); NEVER makes a
    paid call to test. A key can be malformed or exhausted and we won't
    know until an actual call — that's the operator's cost trade to make."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        return Capability(
            "ok",
            "ANTHROPIC_API_KEY present (paid provider — operator opt-in required)",
        )
    return Capability("unavailable", "ANTHROPIC_API_KEY not set")


async def detect_environment() -> Environment:
    """Full snapshot. Every sub-probe is non-fatal; a hard failure records
    an 'unavailable' with the exception type, never raises. Ollama probe
    runs concurrently with the synchronous ones (which are trivial)."""
    ollama_task = asyncio.create_task(_probe_ollama())
    hw = _probe_hardware()
    cli = _probe_claude_cli()
    key = _probe_api_key()
    ollama = await ollama_task
    return Environment(
        platform=_probe_platform(),
        ollama=ollama, hardware=hw, claude_cli=cli, api_key=key,
    )


# ─── recommendation (rules-based, never paid-by-default) ────────────────

# Rough VRAM cost of the small open models we might see.
_MODEL_VRAM_MB: dict[str, int] = {
    "llama3.1:8b": 5500, "llama3.2:3b": 2200, "llama3.2:1b": 900,
    "qwen2.5:7b": 5000, "qwen2.5:14b": 9000, "mistral:7b": 4700,
}


def _recommend_ollama_model(env: Environment) -> str | None:
    """Given the pulled tags + free VRAM, pick the largest model that fits.
    Returns None if Ollama isn't reachable or no pulled model is known.
    'default' when we can't reason about size."""
    if env.ollama.status == "unavailable":
        return None
    tags = env.ollama.facts.get("tags") or []
    if not isinstance(tags, list):
        return None
    vram_free = int(env.hardware.facts.get("vram_free_mb") or 0)
    fits: list[tuple[str, int]] = []
    for tag in tags:
        for known, mb in _MODEL_VRAM_MB.items():
            if known in str(tag):
                if vram_free == 0 or mb <= vram_free * 0.9:
                    fits.append((str(tag), mb))
    if not fits:
        # Fall back to the primary if it's pulled, else "default".
        primary = env.ollama.facts.get("primary")
        if isinstance(primary, str) and env.ollama.facts.get("primary_present"):
            return primary
        return "default"
    # Prefer the largest model that fits — better reasoning at the cost of latency.
    fits.sort(key=lambda kv: -kv[1])
    return fits[0][0]


@dataclass
class TaskRecommendation:
    task: str
    provider: str
    model: str
    reason: str


def suggest_routing(env: Environment) -> list[TaskRecommendation]:
    """Per-task provider recommendation. NEVER recommends 'api' as a
    default (paid → operator opt-in only). Rules are explicit strings so
    they're inspectable at review time."""
    ollama_ok = env.ollama.status in ("ok", "degraded")
    cli_ok = env.claude_cli.status == "ok"
    picked = _recommend_ollama_model(env) or "default"
    out: list[TaskRecommendation] = []
    # LOCAL tasks: thesis, synthesis, chat — prefer ollama; fall to
    # claude-cli only if ollama is truly unreachable.
    for task in ("thesis", "synthesis", "chat"):
        if ollama_ok:
            out.append(TaskRecommendation(
                task, "ollama", picked,
                f"local reasoning; ollama reachable ({env.ollama.detail})",
            ))
        elif cli_ok:
            out.append(TaskRecommendation(
                task, "claude-cli", "default",
                "ollama unavailable; claude-cli present — fallback for local task",
            ))
        else:
            out.append(TaskRecommendation(
                task, "ollama", "default",
                "no reachable provider — task will fail; install Ollama or Claude Code",
            ))
    # SELF-MOD tasks: reflect, shadow, scout — designed for claude-cli.
    for task in ("reflect", "shadow", "scout"):
        if cli_ok:
            out.append(TaskRecommendation(
                task, "claude-cli", "default",
                "claude-cli present — the designed provider for self-modification",
            ))
        elif ollama_ok:
            out.append(TaskRecommendation(
                task, "ollama", picked,
                "claude-cli unavailable; local fallback — self-mod quality degraded",
            ))
        else:
            out.append(TaskRecommendation(
                task, "claude-cli", "default",
                "UNAVAILABLE — install Claude Code; self-modification disabled",
            ))
    return out
