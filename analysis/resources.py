"""Memory-pressure sampling + classification.

Sampled at cycle boundaries — cheap, in-process, no extra subprocess.
Reads /proc/meminfo + /proc/loadavg + /proc/stat (delta-based CPU idle).
Persisted compactly to resource_samples; pruned per LOG_RETENTION_DAYS.

EXPLICITLY NOT DOING (design contract, restated so a future reader
can't quietly cross the line):
  · NO auto-throttling of AUTONOMOUS_CYCLE_MINUTES.
  · NO auto-pausing of cycles.
  · NO auto-provider-switch on heartbeat failure — the call-time
    fallback ladder (core/llm/fallback.py) already covers a dead
    provider VISIBLY. A "helpful" auto-throttle would silently reduce
    throughput during the operator's short cycling windows.
This module observes; the operator decides.

Thresholds justified against the 2026-09-08 host incident:
  measured: 7.6 GB total RAM, swap 61 % used, LAV 77.9 on 12 threads,
            CPU utilisation ~0 %, gate/apply pytest timing out.
So THRASHING = LAV / cpu_count > 4 AND cpu_utilisation < 20 % AND
                swap_pct_used > 50 %. All three had to hold; any one
                alone can be legitimate (a fresh boot has 0 % CPU;
                a heavy build has high LAV without thrashing).
TIGHT = available RAM < 15 % OR swap_used > 0 (the leading edge —
        by the time swap is used, we're on the slope toward the incident).
OK    = neither TIGHT nor THRASHING.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

PressureClass = Literal["OK", "TIGHT", "THRASHING"]

# Thresholds — justified above; tuned to catch the recorded incident
# before it produced the observed 5x pytest variance.
TIGHT_AVAILABLE_PCT = 0.15
THRASH_LAV_RATIO = 4.0
THRASH_CPU_IDLE_MIN = 0.80  # <20% utilisation = >80% idle
THRASH_SWAP_PCT = 0.50


@dataclass
class ResourceSample:
    ts_iso: str
    ram_total_mb: int
    ram_available_mb: int
    swap_total_mb: int
    swap_used_mb: int
    load_avg_1: float
    cpu_count: int
    cpu_idle_pct: float
    classification: PressureClass
    reason: str = ""

    @property
    def swap_pct(self) -> float:
        return self.swap_used_mb / self.swap_total_mb if self.swap_total_mb else 0.0

    @property
    def ram_available_pct(self) -> float:
        return self.ram_available_mb / self.ram_total_mb if self.ram_total_mb else 1.0

    @property
    def lav_ratio(self) -> float:
        return self.load_avg_1 / self.cpu_count if self.cpu_count else 0.0


# Cached CPU snapshot for delta-based idle-percent computation between
# samples. First call returns 100 % idle (no prior baseline).
_CPU_LAST: dict[str, int] = {"total": 0, "idle": 0}


def _read_meminfo() -> dict[str, int]:
    """Return MemTotal / MemAvailable / SwapTotal / SwapFree in KB."""
    out: dict[str, int] = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                for key in ("MemTotal", "MemAvailable", "SwapTotal", "SwapFree"):
                    if line.startswith(f"{key}:"):
                        out[key] = int(line.split()[1])
    except OSError:
        pass
    return out


def _read_loadavg() -> float:
    try:
        with open("/proc/loadavg") as f:
            return float(f.read().split()[0])
    except (OSError, ValueError, IndexError):
        return 0.0


def _read_cpu_idle_pct() -> float:
    """Delta-based CPU-idle percentage between the last call and now.
    First call always returns 1.0 (no baseline — over-report idle is
    the safe direction; we'd rather MISS a spike than classify a
    fresh boot as loaded)."""
    try:
        with open("/proc/stat") as f:
            fields = f.readline().split()
        # cpu  user nice system idle iowait ...
        parts = [int(x) for x in fields[1:8]]
        total = sum(parts)
        idle = parts[3] + parts[4]  # idle + iowait
    except (OSError, ValueError, IndexError):
        return 1.0
    prev_total, prev_idle = _CPU_LAST.get("total", 0), _CPU_LAST.get("idle", 0)
    _CPU_LAST["total"], _CPU_LAST["idle"] = total, idle
    if prev_total == 0 or total <= prev_total:
        return 1.0
    delta_total = total - prev_total
    delta_idle = idle - prev_idle
    if delta_total == 0:
        return 1.0
    return max(0.0, min(1.0, delta_idle / delta_total))


def classify_sample(
    ram_available_pct: float,
    swap_pct: float,
    lav_ratio: float,
    cpu_idle_pct: float,
) -> tuple[PressureClass, str]:
    """Pure classifier — pulled out so tests can hit exact boundaries."""
    thrashing_conds = (
        lav_ratio > THRASH_LAV_RATIO,
        cpu_idle_pct > THRASH_CPU_IDLE_MIN,
        swap_pct > THRASH_SWAP_PCT,
    )
    if all(thrashing_conds):
        return "THRASHING", (
            f"LAV/cpu={lav_ratio:.1f} + cpu_idle={cpu_idle_pct*100:.0f}% + "
            f"swap={swap_pct*100:.0f}% (all three: swap-thrash signature)"
        )
    if ram_available_pct < TIGHT_AVAILABLE_PCT or swap_pct > 0:
        parts = []
        if ram_available_pct < TIGHT_AVAILABLE_PCT:
            parts.append(f"avail RAM {ram_available_pct*100:.0f}% < 15%")
        if swap_pct > 0:
            parts.append(f"swap in use ({swap_pct*100:.0f}%)")
        return "TIGHT", " + ".join(parts)
    return "OK", ""


def sample_now() -> ResourceSample:
    """One in-process sample. Never raises — an unreadable /proc file
    just yields conservative zeros (classification defaults to OK)."""
    from datetime import datetime, timezone
    mi = _read_meminfo()
    ram_total_kb = mi.get("MemTotal", 0)
    ram_avail_kb = mi.get("MemAvailable", 0)
    swap_total_kb = mi.get("SwapTotal", 0)
    swap_free_kb = mi.get("SwapFree", 0)
    swap_used_kb = max(0, swap_total_kb - swap_free_kb)
    lav = _read_loadavg()
    cpus = os.cpu_count() or 1
    idle_pct = _read_cpu_idle_pct()

    sample = ResourceSample(
        ts_iso=datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
        ram_total_mb=ram_total_kb // 1024,
        ram_available_mb=ram_avail_kb // 1024,
        swap_total_mb=swap_total_kb // 1024,
        swap_used_mb=swap_used_kb // 1024,
        load_avg_1=lav,
        cpu_count=cpus,
        cpu_idle_pct=idle_pct,
        classification="OK",
    )
    klass, reason = classify_sample(
        sample.ram_available_pct, sample.swap_pct, sample.lav_ratio, sample.cpu_idle_pct,
    )
    sample.classification = klass
    sample.reason = reason
    return sample


def summarize_window(samples: list[ResourceSample]) -> dict[str, object]:
    """Aggregate over a session window. Compact — one line per
    metric in `morgoth session-report`."""
    if not samples:
        return {"n": 0, "worst": "OK", "tight": 0, "thrashing": 0, "peak_swap_pct": 0.0}
    tight = sum(1 for s in samples if s.classification == "TIGHT")
    thrashing = sum(1 for s in samples if s.classification == "THRASHING")
    worst = "THRASHING" if thrashing else ("TIGHT" if tight else "OK")
    peak_swap = max(s.swap_pct for s in samples)
    return {
        "n": len(samples), "worst": worst,
        "tight": tight, "thrashing": thrashing,
        "peak_swap_pct": round(peak_swap * 100, 1),
    }
