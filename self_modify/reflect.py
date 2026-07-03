"""Reflection job: Morgoth's propose channel.

Invoked ONLY from the CLI (``python -m self_modify.reflect``). Never
imported by ``core/brain.py`` — Morgoth's research cycles cannot reach
this path. The reflect job is a dedicated pass gated by
``can_self_modify`` in ``MORGOTH_PERMS.json`` and by a pending-queue cap.

Spec-from-LLM / code-from-template
----------------------------------
The 8B produces ONLY a small JSON spec (tool name, endpoint, digest
fields, description, rationale) or the literal ``NONE``. The reflect
job assembles the tool file DETERMINISTICALLY from a template that
mirrors ``tools/data_feeds/fear_greed.py``. The model never writes
Python; it names a gap to fill.

Gate sequence (each logged; refusal reasons are structured)
-----------------------------------------------------------
0. **CAP** — pending_approval + proposed_by='morgoth' >= 3 → refuse.
1. **PERMISSION** — can_self_modify false → refuse.
2. **CONTEXT** — deterministic pull of registered tools + last N
   objectives + last M active thesis subjects. Reused from the
   system-vault loaders.
3. **LLM SPEC** — one Ollama chat call. Strict output: JSON spec or the
   literal ``NONE``.
4. **PARSE** — defensive (fences, prose tolerance). Unparseable →
   NONE-equivalent (noisy 8B output is calibration data, not an error).
5. **SMOKE TEST** — api_base_url must be https; host must not be an IP
   literal, localhost, or a private range. GET base+path, 10s timeout;
   non-2xx or network failure → reject before submission.
6. **CODE ASSEMBLY** — render the tool file from ``TOOL_TEMPLATE``.
7. **PRE-SUBMISSION ZONE CHECK** — ``classify_proposal`` on
   ``tools/data_feeds/<name>.py`` must be green. Path traversal or
   anything else non-green → reject WITHOUT submitting.
8. **SUBMIT** — via ``run_pipeline`` with ``proposed_by='morgoth'``.
   The pipeline re-checks the zone and runs the sandbox pytest.

Apply is UNREACHABLE from this module. There is no ``import
self_modify.apply`` here; the guardrail test enforces the absence.
"""

from __future__ import annotations

import ipaddress
import json
import re
import socket
from typing import Any
from urllib.parse import urlparse

import httpx
from loguru import logger

from core.config import AppConfig
from core.llm_client import ChatMessage, OllamaLLMClient
from memory.persistent import PersistentMemory
from self_modify import gates
from self_modify import proposals as P
from self_modify.zones import classify_proposal


REFLECTION_PENDING_CAP: int = 3
SMOKE_TIMEOUT_SECS: float = 10.0
SNAKE_CASE_RE = re.compile(r"^[a-z][a-z0-9_]{2,49}$")
DIGEST_FIELD_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")


# ---------- code template ---------------------------------------------------
# Mirrors tools/data_feeds/fear_greed.py structure. The spec's tool_name,
# description, base URL, endpoint path, and digest fields are substituted
# in via str.format; nothing else in the file changes. Model output never
# reaches the code path — only the six spec fields do.

TOOL_TEMPLATE = '''"""{docstring_first_line}

Auto-generated from a Morgoth-authored spec via the reflect job.
See self_modify_proposals for the proposal row (proposed_by='morgoth').
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx

from core.config import AppConfig, PermissionDeniedError
from tools.base_tool import BaseTool


{const_name}_BASE_URL = "{base_url}"


class {class_name}(BaseTool):
    """{description}"""

    name = "{tool_name}"
    is_data_source = True
    description = (
        "{description}"
    )
    parameters = {{
        "type": "object",
        "properties": {{}},
    }}

    def __init__(
        self,
        config: AppConfig,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self._client = client or httpx.AsyncClient(timeout=30.0)

    async def close(self) -> None:
        await self._client.aclose()

    async def execute(self, **_kwargs: Any) -> dict[str, Any]:
        if not self._config.permissions.permissions.can_access_internet:
            raise PermissionDeniedError("Internet access is disabled by permissions")

        try:
            resp = await self._client.get(f"{{{const_name}_BASE_URL}}{endpoint_path}")
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            return self.failure(
                f"{source_label} request failed: {{exc}}",
                source="{source_label}",
            )

        data = resp.json()
        # Best-effort digest: pick the requested fields from a top-level dict
        # OR from the first entry of a top-level list (mirrors the fear_greed
        # pattern for {{"data": [...]}} shaped responses).
        record: dict[str, Any] = {{}}
        if isinstance(data, dict):
            for key in {digest_fields!r}:
                if key in data:
                    record[key] = data[key]
            if not record and isinstance(data.get("data"), list) and data["data"]:
                first = data["data"][0]
                if isinstance(first, dict):
                    for key in {digest_fields!r}:
                        if key in first:
                            record[key] = first[key]
        elif isinstance(data, list) and data and isinstance(data[0], dict):
            for key in {digest_fields!r}:
                if key in data[0]:
                    record[key] = data[0][key]
        if not record:
            return self.failure(
                "response did not contain any of the expected digest fields",
                source="{source_label}",
            )

        fetched_at = datetime.now(timezone.utc).isoformat()
        return self.success(record, source="{source_label}", fetched_at=fetched_at)
'''


# ---------- utilities -------------------------------------------------------

def _snake_to_class_name(snake: str) -> str:
    """``get_fear_greed_index`` -> ``GetFearGreedIndexTool``."""
    return "".join(part.capitalize() for part in snake.split("_")) + "Tool"


def _parse_spec(text: str) -> dict[str, Any] | None:
    """Defensive JSON parser. Returns None for NONE or unparseable output.

    Follows the ``_parse_thesis_json`` pattern: strip code fences, slice
    the first ``{...}`` block, return None instead of raising on any
    failure so bad LLM output is calibration data, not an error.
    """
    if not text or not isinstance(text, str):
        return None
    stripped = text.strip()
    if stripped.upper().startswith("NONE"):
        return None
    if stripped.startswith("```"):
        lines = stripped.split("\n")
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        obj = json.loads(stripped[start : end + 1])
    except (ValueError, TypeError):
        return None
    if not isinstance(obj, dict):
        return None
    return obj


def _spec_is_well_formed(spec: dict[str, Any]) -> str | None:
    """Return an error string if the spec is malformed; None if OK."""
    tool_name = spec.get("tool_name")
    if not isinstance(tool_name, str) or not SNAKE_CASE_RE.match(tool_name):
        return f"tool_name must match {SNAKE_CASE_RE.pattern}"
    url = spec.get("api_base_url")
    if not isinstance(url, str) or not url:
        return "api_base_url missing"
    path = spec.get("endpoint_path")
    if not isinstance(path, str):
        return "endpoint_path missing"
    digest = spec.get("digest_fields")
    if not (isinstance(digest, list) and 3 <= len(digest) <= 6):
        return "digest_fields must be a list of 3-6 items"
    for f in digest:
        if not (isinstance(f, str) and DIGEST_FIELD_RE.match(f)):
            return f"digest field {f!r} is not a snake-case identifier"
    desc = spec.get("description")
    if not isinstance(desc, str) or len(desc.strip()) < 10:
        return "description too short"
    if not isinstance(spec.get("rationale"), str):
        return "rationale missing"
    return None


def _host_is_public(host: str) -> bool:
    """Reject localhost, IP literals, private/reserved ranges."""
    if not host or host.lower() in {"localhost"}:
        return False
    # IP literal? Reject — the smoke gate accepts DNS hostnames only.
    try:
        ipaddress.ip_address(host)
        return False
    except ValueError:
        pass
    # Resolve and check each address falls in a public range.
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            return False
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return False
    return True


def _url_passes_gate(url: str) -> str | None:
    """Return an error string if the URL fails the pre-smoke checks."""
    parsed = urlparse(url)
    if parsed.scheme != "https":
        return f"scheme must be https, got {parsed.scheme!r}"
    if not parsed.hostname:
        return "no hostname"
    if not _host_is_public(parsed.hostname):
        return f"host {parsed.hostname!r} is not a public DNS host"
    return None


async def _smoke_get(url: str) -> str | None:
    """Return None on 2xx; error string otherwise."""
    try:
        async with httpx.AsyncClient(timeout=SMOKE_TIMEOUT_SECS) as client:
            resp = await client.get(url)
    except httpx.HTTPError as exc:
        return f"network error: {type(exc).__name__}: {exc}"
    if not 200 <= resp.status_code < 300:
        return f"non-2xx status {resp.status_code}"
    return None


# ---------- context ---------------------------------------------------------

async def _build_context(pm: PersistentMemory, config: AppConfig) -> dict[str, Any]:
    """Registered tools + last objective topics + last active thesis subjects.

    Reuses the system-vault loaders (compile_wiki._registered_tools_offline
    and _load_tool_usage), same code path as the vault build.
    """
    from scripts.compile_wiki import _load_tool_usage, _registered_tools_offline

    tools = _registered_tools_offline(config, pm)
    objectives_count, _theses_fed = await _load_tool_usage(pm)
    tool_lines: list[str] = []
    for t in sorted(tools, key=lambda x: x.name):
        line = (
            f"- {t.name} "
            f"({'data_source' if getattr(t, 'is_data_source', False) else 'chat/util'}) "
            f"— objectives_using={objectives_count.get(t.name, 0)}: "
            f"{(getattr(t, 'description', '') or '').strip()[:120]}"
        )
        tool_lines.append(line)

    obj_rows = await pm.get_objectives(limit=10)
    obj_lines = [f"- {row.get('title', '(untitled)')}" for row in obj_rows[:10]]

    theses = await pm.get_theses(status="active", limit=25)
    subjects_seen: set[str] = set()
    thesis_lines: list[str] = []
    for t in theses:
        subject = (t.get("subject") or "").strip()
        if subject and subject not in subjects_seen:
            subjects_seen.add(subject)
            thesis_lines.append(f"- {subject}")
        if len(thesis_lines) >= 15:
            break

    return {
        "tools_block": "\n".join(tool_lines) if tool_lines else "(none)",
        "objectives_block": "\n".join(obj_lines) if obj_lines else "(none)",
        "theses_block": "\n".join(thesis_lines) if thesis_lines else "(none)",
    }


def _reflection_prompt(ctx: dict[str, Any]) -> str:
    """The single prompt sent to the 8B."""
    return f"""You are proposing ONE new data-feed tool for Morgoth to add.

CURRENT TOOLS (name, kind, usage, description):
{ctx['tools_block']}

RECENT OBJECTIVE TOPICS (newest first):
{ctx['objectives_block']}

RECENT ACTIVE THESIS SUBJECTS:
{ctx['theses_block']}

TASK: Suggest EXACTLY ONE new tool under tools/data_feeds/ that fills a
gap the context above shows. The API must be FREE, require NO API key,
and speak HTTPS. If no clear gap justifies a new tool, respond with the
literal word NONE — that is a fully acceptable answer.

OUTPUT FORMAT — a single JSON object OR the word NONE. Nothing else.
{{
  "tool_name": "<snake_case>",
  "api_base_url": "https://<host>",
  "endpoint_path": "<path>",
  "digest_fields": ["<snake_case_field>", ...],  // 3-6 items expected in the JSON response
  "description": "<one sentence, what the tool fetches and why it matters>",
  "rationale": "<one sentence, what gap in the current tools this fills>"
}}"""


# ---------- main entry point -----------------------------------------------

def _short(msg: str, n: int = 200) -> str:
    return msg if len(msg) <= n else msg[:n] + "…"


async def run_reflection(
    config: AppConfig,
    pm: PersistentMemory,
    llm: OllamaLLMClient,
) -> dict[str, Any]:
    """Attempt one proposal cycle. Returns a structured result.

    Result shape:
      {'outcome': 'refused_flag' | 'refused_cap' | 'none' | 'unparseable' |
                  'malformed' | 'rejected_url' | 'rejected_smoke' |
                  'rejected_zone' | 'submitted',
       'reason': str,                    # human-readable
       'proposal_id': str | None,
       'pipeline_status': str | None,    # only when outcome == 'submitted'
       'spec': dict | None}
    """
    log = lambda msg: logger.info("reflect: {}", msg)  # noqa: E731

    # Gate 1: permission
    if not config.permissions.permissions.can_self_modify:
        log("refused — can_self_modify=false")
        return {"outcome": "refused_flag", "reason": "can_self_modify is false",
                "proposal_id": None, "pipeline_status": None, "spec": None}

    store = P.ProposalStore(pm)

    # Gate 0: cap
    n_pending = await store.count_by_status_and_author(
        status=P.STATUS_PENDING_APPROVAL, proposed_by="morgoth"
    )
    if n_pending >= REFLECTION_PENDING_CAP:
        reason = (
            f"pending queue full ({n_pending}/{REFLECTION_PENDING_CAP} "
            "morgoth-authored pending); review existing proposals first"
        )
        log(f"refused — {reason}")
        return {"outcome": "refused_cap", "reason": reason,
                "proposal_id": None, "pipeline_status": None, "spec": None}

    # Gate 2: context
    ctx = await _build_context(pm, config)
    prompt = _reflection_prompt(ctx)
    log(f"context built: {len(ctx['tools_block'].splitlines())} tools, "
        f"{len(ctx['objectives_block'].splitlines())} objectives, "
        f"{len(ctx['theses_block'].splitlines())} thesis subjects")

    # Gate 3: single LLM call
    response = await llm.chat([ChatMessage(role="user", content=prompt)])
    raw = (response.message.content or "").strip()
    log(f"raw spec (first 400 chars): {_short(raw, 400)}")

    # Gate 4: parse
    spec = _parse_spec(raw)
    if spec is None:
        # Distinguish NONE from garbage — both stop here cleanly.
        if raw.upper().startswith("NONE") or raw.upper() == "":
            log("outcome: NONE (model declined)")
            return {"outcome": "none", "reason": "model declined (NONE)",
                    "proposal_id": None, "pipeline_status": None, "spec": None}
        log("outcome: unparseable")
        return {"outcome": "unparseable",
                "reason": f"could not parse spec: {_short(raw, 300)}",
                "proposal_id": None, "pipeline_status": None, "spec": None}

    err = _spec_is_well_formed(spec)
    if err:
        log(f"outcome: malformed — {err}")
        return {"outcome": "malformed", "reason": err,
                "proposal_id": None, "pipeline_status": None, "spec": spec}

    # Gate 5: URL + smoke
    smoke_target = spec["api_base_url"].rstrip("/") + spec["endpoint_path"]
    url_err = _url_passes_gate(spec["api_base_url"])
    if url_err:
        log(f"outcome: rejected_url — {url_err}")
        return {"outcome": "rejected_url", "reason": url_err,
                "proposal_id": None, "pipeline_status": None, "spec": spec}
    smoke_err = await _smoke_get(smoke_target)
    if smoke_err:
        log(f"outcome: rejected_smoke — {smoke_err}")
        return {"outcome": "rejected_smoke",
                "reason": f"smoke test on {smoke_target}: {smoke_err}",
                "proposal_id": None, "pipeline_status": None, "spec": spec}

    # Gate 6: code assembly (deterministic template)
    tool_name = spec["tool_name"]
    class_name = _snake_to_class_name(tool_name)
    const_name = tool_name.upper()
    source_label = spec["api_base_url"].replace("https://", "").split("/")[0]
    docstring_first_line = spec["description"].strip().split("\n")[0][:200]
    content = TOOL_TEMPLATE.format(
        tool_name=tool_name,
        class_name=class_name,
        const_name=const_name,
        base_url=spec["api_base_url"],
        endpoint_path=spec["endpoint_path"],
        digest_fields=list(spec["digest_fields"]),
        description=spec["description"].replace('"', "'"),
        docstring_first_line=docstring_first_line,
        source_label=source_label,
    )
    log(f"code assembled: {len(content)} bytes")

    # Gate 7: pre-submission zone check (enforcement level 1)
    target_path = f"tools/data_feeds/{tool_name}.py"
    zone = classify_proposal(target_path, "new_file")
    if zone != "green":
        log(f"outcome: rejected_zone — classify_proposal={zone!r}")
        return {"outcome": "rejected_zone",
                "reason": f"pre-submission zone check: {zone}",
                "proposal_id": None, "pipeline_status": None, "spec": spec}

    # Gate 8: submit
    proposal_id = await store.submit(
        target_path=target_path,
        change_type="new_file",
        content=content,
        rationale=spec["rationale"],
        proposed_by="morgoth",
    )
    row = await store.get(proposal_id)
    final_status = await gates.run_pipeline(store, row)
    log(f"outcome: submitted — id={proposal_id[:8]} status={final_status}")
    return {"outcome": "submitted", "reason": final_status,
            "proposal_id": proposal_id, "pipeline_status": final_status,
            "spec": spec}


# ---------- CLI entry point ------------------------------------------------

async def _main_async() -> int:
    from core.config import load_config

    config = await load_config()
    pm = PersistentMemory(config)
    await pm.initialize()
    llm = OllamaLLMClient(config)
    try:
        result = await run_reflection(config, pm, llm)
    finally:
        await pm.close()
        await llm.close()

    print(f"outcome:         {result['outcome']}")
    print(f"reason:          {result['reason']}")
    if result.get("spec"):
        print("spec:")
        for key, value in result["spec"].items():
            print(f"  {key:16}: {value}")
    if result.get("proposal_id"):
        print(f"proposal_id:     {result['proposal_id']}")
        print(f"pipeline_status: {result['pipeline_status']}")
    return 0 if result["outcome"] in {"submitted", "none"} else 1


def main() -> None:
    import asyncio
    import sys

    sys.exit(asyncio.run(_main_async()))


if __name__ == "__main__":
    main()
