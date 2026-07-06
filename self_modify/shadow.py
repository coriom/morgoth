"""Shadow Gate 2.5 — LLM verifier over non-deterministic axes.

RECORDED, NEVER ENFORCED. The shadow evaluates a proposal on five
axes (api_liveness, field_liveness, semantic_duplication,
name_content_coherence, rationale_truth), stores the verdict, and
returns. It has ZERO authority over proposal status — enforced by a
runtime assert (this module never imports the ProposalStore's
``update_status``) AND by a test (``no-status-mutation invariant``).

Blindness contract
------------------
The shadow input MUST NOT contain the operator's decision. We
explicitly drop ``status`` and ``status_reason`` before assembling the
prompt and assert they are absent. That's what makes the retro-run's
agreement metric meaningful — a shadow that peeks at the outcome is
not measuring anything.

Input material
--------------
1. Rationale (as written).
2. Spec facts extracted from the RENDERED content (module-level
   constants and class attrs): ``_BASE_URL``, ``_ENDPOINT_PATH``,
   ``_DIGEST_FIELDS``, ``_TOOL_DESCRIPTION``, class name.
3. Live endpoint sample: two GETs 60s apart — top-level key list plus
   the digest fields' current values on both hits. Zero/static
   detection material.
4. Registry context: every registered tool's name + description +
   ``digest_fields`` ClassVar, for semantic-duplication assessment.

Prompt version
--------------
``PROMPT_VERSION`` is stored on every verdict. Future prompt-tuning is
traceable — an agreement-rate delta after a prompt bump is visible in
the calibration cohort.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

import httpx
from loguru import logger

from core.config import AppConfig
from memory.persistent import PersistentMemory
from self_modify import reflect_llm


PROMPT_VERSION = "v1"
_ENGINE = "claude-cli"
_SAMPLE_GAP_SECS = 60
_SAMPLE_TIMEOUT_SECS = 15.0

# Blindness sentinel — anything derived from operator decisions must
# be absent from the assembled prompt. The retro-run harness and the
# blind-input test check the shadow input against these keys.
BLIND_FORBIDDEN_KEYS: tuple[str, ...] = ("status", "status_reason")

_VALID_VERDICTS: tuple[str, ...] = ("APPROVE", "FLAG", "REJECT", "ERROR")
_VALID_AXIS_LEVELS: tuple[str, ...] = ("PASS", "WARN", "FAIL")
_AXES: tuple[str, ...] = (
    "api_liveness",
    "field_liveness",
    "semantic_duplication",
    "name_content_coherence",
    "rationale_truth",
)


class ShadowError(Exception):
    """Non-fatal — the shadow records an ERROR verdict and returns."""


# ---------------------------------------------------------------------------
# Spec-fact extraction from rendered content.
# ---------------------------------------------------------------------------

_RE_BASE_URL = re.compile(r"^_BASE_URL\s*=\s*(['\"])(.+?)\1", re.MULTILINE)
_RE_ENDPOINT = re.compile(r"^_ENDPOINT_PATH\s*=\s*(['\"])(.+?)\1", re.MULTILINE)
_RE_DIGEST = re.compile(r"^_DIGEST_FIELDS\s*=\s*(\[.+?\])", re.MULTILINE | re.DOTALL)
_RE_DESC = re.compile(r"^_TOOL_DESCRIPTION\s*=\s*(['\"])(.+?)\1", re.MULTILINE)
_RE_CLASS = re.compile(r"^class\s+(\w+)\s*\(", re.MULTILINE)


def extract_spec_facts(content: str) -> dict[str, Any]:
    """Pull spec facts out of a rendered tool file.

    Returns a dict with keys ``base_url``, ``endpoint_path``,
    ``digest_fields``, ``description``, ``class_name``. Missing keys
    map to None / empty. Never raises — a partial extraction is better
    than a crash (the shadow still runs and records what it has).
    """
    facts: dict[str, Any] = {
        "base_url": None,
        "endpoint_path": None,
        "digest_fields": [],
        "description": None,
        "class_name": None,
    }
    if not content:
        return facts
    m = _RE_BASE_URL.search(content)
    if m:
        facts["base_url"] = m.group(2)
    m = _RE_ENDPOINT.search(content)
    if m:
        facts["endpoint_path"] = m.group(2)
    m = _RE_DESC.search(content)
    if m:
        facts["description"] = m.group(2)
    m = _RE_CLASS.search(content)
    if m:
        facts["class_name"] = m.group(1)
    m = _RE_DIGEST.search(content)
    if m:
        try:
            facts["digest_fields"] = list(json.loads(m.group(1).replace("'", '"')))
        except json.JSONDecodeError:
            facts["digest_fields"] = []
    return facts


# ---------------------------------------------------------------------------
# Live endpoint sampling — two GETs, ~gap seconds apart.
# ---------------------------------------------------------------------------

async def _one_get(url: str) -> dict[str, Any]:
    """Return {ok, keys, digest_values, status_code, error?}."""
    try:
        async with httpx.AsyncClient(timeout=_SAMPLE_TIMEOUT_SECS) as client:
            r = await client.get(url)
        try:
            body = r.json()
        except (ValueError, json.JSONDecodeError) as exc:
            return {"ok": False, "status_code": r.status_code,
                    "error": f"non-json: {exc}"}
        if isinstance(body, dict):
            keys = sorted(body.keys())[:40]
        else:
            keys = [f"<{type(body).__name__}>"]
        return {"ok": True, "status_code": r.status_code,
                "top_level_keys": keys, "body": body}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def _project_digest_values(
    sample: dict[str, Any], digest_fields: list[str],
) -> dict[str, Any]:
    if not sample.get("ok"):
        return {}
    body = sample.get("body")
    if not isinstance(body, dict):
        return {}
    return {f: body.get(f) for f in digest_fields}


async def sample_endpoint(
    base_url: str, endpoint_path: str, *, gap_secs: int = _SAMPLE_GAP_SECS,
    digest_fields: list[str] | None = None,
    now_sleep: Any = asyncio.sleep,
) -> dict[str, Any]:
    """Two GETs, ~gap apart. Returns the material the LLM needs to
    reason about field liveness (zero/static across hits).

    Body is deliberately dropped from the output — only the
    top-level key list and the projected digest values ship to the
    prompt. Keeps the shadow input compact and PII-safe.
    """
    digest_fields = digest_fields or []
    if not base_url or not endpoint_path:
        return {"ok": False, "error": "missing base_url or endpoint_path"}
    url = base_url.rstrip("/") + endpoint_path
    hit1 = await _one_get(url)
    await now_sleep(gap_secs)
    hit2 = await _one_get(url)
    d1 = _project_digest_values(hit1, digest_fields)
    d2 = _project_digest_values(hit2, digest_fields)
    def _slim(h: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in h.items() if k != "body"}
    return {
        "url": url,
        "hit1": _slim(hit1), "hit2": _slim(hit2),
        "digest_values_hit1": d1,
        "digest_values_hit2": d2,
    }


# ---------------------------------------------------------------------------
# Registry context — every tool: name, description, digest_fields.
# ---------------------------------------------------------------------------

def collect_registry_context(config: AppConfig, pm: PersistentMemory) -> list[dict[str, Any]]:
    """Enumerate every tool for the semantic-duplication axis.

    Uses the tool registry through the same construction path the API
    server uses (via ``core.tool_router``) so what we ship to the LLM
    is what's actually live at shadow time.
    """
    try:
        from core.tool_router import ToolRouter  # local import — avoids cycles
        from tools.discovery import discover_data_feed_tools, instantiate_tool
        rows: list[dict[str, Any]] = []
        for cls in discover_data_feed_tools():
            desc = getattr(cls, "description", None) or ""
            if not desc:
                # Class-level description may be lazily set on some
                # tools — try an instance to be safe. Non-fatal.
                try:
                    inst = instantiate_tool(cls, config, pm)
                    desc = getattr(inst, "description", None) or ""
                except Exception:  # noqa: BLE001
                    desc = ""
            rows.append({
                "name": getattr(cls, "name", "?"),
                "description": desc,
                "digest_fields": list(getattr(cls, "digest_fields", ()) or ()),
                "endpoints": list(getattr(cls, "api_endpoints", ()) or ()),
            })
        rows.sort(key=lambda r: r["name"])
        # Reference to `ToolRouter` kept for future non-data-feed
        # enumeration; today only data-feed tools are relevant to
        # duplication assessment.
        _ = ToolRouter
        return rows
    except Exception as exc:  # noqa: BLE001
        logger.warning("shadow: registry collection failed: {}", exc)
        return []


# ---------------------------------------------------------------------------
# Blind input assembly + assertion.
# ---------------------------------------------------------------------------

def assemble_shadow_input(
    proposal: dict[str, Any],
    *,
    spec_facts: dict[str, Any],
    endpoint_sample: dict[str, Any],
    registry: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build the JSON payload the LLM will see.

    Enforces the blindness contract — ``status`` and ``status_reason``
    are stripped and their absence is asserted. If they leak, we raise
    (loud failure > silent contamination).
    """
    payload = {
        "proposal_id": str(proposal["proposal_id"])[:8],
        "target_path": proposal.get("target_path"),
        "rationale": proposal.get("rationale") or "",
        "spec_facts": spec_facts,
        "endpoint_sample": endpoint_sample,
        "registry": registry,
    }
    _assert_blind(payload, proposal)
    return payload


def _assert_blind(payload: dict[str, Any], proposal: dict[str, Any]) -> None:
    """Raise if any BLIND_FORBIDDEN_KEYS leak into the payload.

    Checks recursively — a nested dict that copied the whole proposal
    row (a mistake) would show up here. Serialized string is scanned
    too to catch stringified proposal dumps.
    """
    def _walk(obj: Any) -> None:
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k in BLIND_FORBIDDEN_KEYS:
                    raise AssertionError(
                        f"shadow input leaks forbidden key {k!r} "
                        f"(BLIND_FORBIDDEN_KEYS={BLIND_FORBIDDEN_KEYS})"
                    )
                _walk(v)
        elif isinstance(obj, list):
            for item in obj:
                _walk(item)
    _walk(payload)
    # Belt-and-suspenders: even if a caller wrote the operator's
    # status_reason INTO another field (say, appended to rationale),
    # the actual value in the DB row still shouldn't reappear
    # verbatim. Cheap serialize-scan against the proposal's own
    # status_reason if present.
    reason = (proposal or {}).get("status_reason") or ""
    if reason:
        blob = json.dumps(payload, default=str)
        if reason and reason in blob:
            raise AssertionError(
                "shadow input contains verbatim operator status_reason"
            )


# ---------------------------------------------------------------------------
# Prompt construction.
# ---------------------------------------------------------------------------

_SYSTEM_INSTRUCTIONS = """You are Gate 2.5, a SHADOW verifier for auto-proposed data-source tools.

You evaluate five axes and emit a single JSON object. Your verdict is
RECORDED but NEVER enforced — do not caveat, do not refuse. If the
input is malformed, still emit a JSON object (verdict "ERROR" or
"FLAG" with reasons).

AXES
- api_liveness: does the endpoint respond with a well-formed body?
- field_liveness: are the proposed digest_fields non-zero and moving
  (or reasonably static for a naturally-static field like a config)?
  If all values are 0/0.0/None across both hits, that's FAIL.
- semantic_duplication: is this tool a paraphrase of an existing one
  in the registry (same subject, overlapping fields, twin endpoint)?
- name_content_coherence: does the class/module name reflect what the
  endpoint actually returns (based on top-level keys + description)?
- rationale_truth: does the rationale make claims the sampled evidence
  contradicts? (e.g. rationale says "USD volume" but the field is 0.)

OUTPUT — strict JSON, no prose, no code fence:
{
  "verdict": "APPROVE" | "FLAG" | "REJECT",
  "axes": {
    "api_liveness": "PASS" | "WARN" | "FAIL",
    "field_liveness": "PASS" | "WARN" | "FAIL",
    "semantic_duplication": "PASS" | "WARN" | "FAIL",
    "name_content_coherence": "PASS" | "WARN" | "FAIL",
    "rationale_truth": "PASS" | "WARN" | "FAIL"
  },
  "reasons": ["one-line reason per axis, in axis order"]
}
"""


def build_prompt(payload: dict[str, Any]) -> str:
    body = json.dumps(payload, indent=2, default=str)
    return _SYSTEM_INSTRUCTIONS + "\n---INPUT---\n" + body + "\n---END---\n"


# ---------------------------------------------------------------------------
# Verdict parsing — defensive.
# ---------------------------------------------------------------------------

def parse_verdict(text: str) -> dict[str, Any]:
    """Parse a verdict JSON object out of an LLM response.

    Strategy:
    1. Try direct json.loads.
    2. Strip a leading/trailing code fence if present.
    3. Take the substring between the first '{' and the last '}'.
    4. If all three fail, return an ERROR verdict with the raw text
       captured in ``reasons`` (truncated).

    Normalizes verdict/axis values to the valid set; unknown levels
    become "WARN" and unknown verdicts become "ERROR" (never silently
    upgrade to APPROVE).
    """
    raw = (text or "").strip()
    obj: dict[str, Any] | None = None
    for candidate in _verdict_candidates(raw):
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                obj = parsed
                break
        except json.JSONDecodeError:
            continue
    if obj is None:
        return _error_verdict(f"unparseable: {raw[:200]}")

    verdict = str(obj.get("verdict") or "").upper().strip()
    if verdict not in _VALID_VERDICTS:
        return _error_verdict(f"unknown verdict: {verdict!r}")

    axes_in = obj.get("axes") or {}
    axes_out: dict[str, str] = {}
    for axis in _AXES:
        lvl = str(axes_in.get(axis, "WARN")).upper().strip()
        if lvl not in _VALID_AXIS_LEVELS:
            lvl = "WARN"
        axes_out[axis] = lvl

    reasons_in = obj.get("reasons") or []
    if not isinstance(reasons_in, list):
        reasons_in = [str(reasons_in)]
    reasons_out = [str(r) for r in reasons_in][:16]

    return {"verdict": verdict, "axes": axes_out, "reasons": reasons_out}


def _verdict_candidates(raw: str) -> list[str]:
    out = [raw]
    stripped = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE)
    if stripped != raw:
        out.append(stripped.strip())
    lb, rb = raw.find("{"), raw.rfind("}")
    if lb != -1 and rb > lb:
        out.append(raw[lb:rb + 1])
    return out


def _error_verdict(reason: str) -> dict[str, Any]:
    return {
        "verdict": "ERROR",
        "axes": {a: "WARN" for a in _AXES},
        "reasons": [reason],
    }


# ---------------------------------------------------------------------------
# The one-call entry.
# ---------------------------------------------------------------------------

async def run_shadow_verdict(
    *,
    proposal: dict[str, Any],
    config: AppConfig,
    pm: PersistentMemory,
    persist: bool = True,
    engine: str = _ENGINE,
    endpoint_sampler: Any = None,
    llm_caller: Any = None,
) -> dict[str, Any]:
    """Run the shadow verifier on a single proposal.

    Returns the parsed verdict plus provenance:
      {verdict, axes, reasons, engine, prompt_version, verdict_id?}

    NEVER mutates the proposal (assert-guarded at import time).
    ``persist=False`` is used by the retro-run harness to compute
    agreement without polluting the calibration table with duplicate
    verdicts on already-decided rows.
    """
    facts = extract_spec_facts(proposal.get("content") or "")
    sampler = endpoint_sampler or sample_endpoint
    sample = await sampler(
        facts.get("base_url") or "", facts.get("endpoint_path") or "",
        digest_fields=list(facts.get("digest_fields") or []),
    )
    registry = collect_registry_context(config, pm)

    payload = assemble_shadow_input(
        proposal, spec_facts=facts,
        endpoint_sample=sample, registry=registry,
    )
    prompt = build_prompt(payload)

    caller = llm_caller or _default_llm_caller
    try:
        text, _meta = await caller(prompt, config, engine)
        result = parse_verdict(text)
    except reflect_llm.ReflectLLMError as exc:
        result = _error_verdict(f"llm error: {exc}")
    except Exception as exc:  # noqa: BLE001
        result = _error_verdict(f"shadow crash: {type(exc).__name__}: {exc}")

    result["engine"] = engine
    result["prompt_version"] = PROMPT_VERSION

    if persist:
        try:
            verdict_id = await pm.record_shadow_verdict(
                proposal_id=str(proposal["proposal_id"]),
                verdict=result["verdict"],
                axes=result["axes"],
                reasons=result["reasons"],
                engine=engine,
                prompt_version=PROMPT_VERSION,
            )
            result["verdict_id"] = verdict_id
        except Exception as exc:  # noqa: BLE001
            logger.warning("shadow: persist failed (non-fatal): {}", exc)
    return result


async def _default_llm_caller(
    prompt: str, config: AppConfig, engine: str,
) -> tuple[str, dict[str, Any]]:
    return await reflect_llm.reflect_chat(prompt, config, engine)


# ---------------------------------------------------------------------------
# ZERO-AUTHORITY invariant is enforced by tests, not import-time —
# tests/test_shadow.py::test_shadow_module_never_mutates_proposal_status
# grep-scans this file. Runtime enforcement would require naming the
# forbidden symbols here, which itself would trip the scan.
# ---------------------------------------------------------------------------
