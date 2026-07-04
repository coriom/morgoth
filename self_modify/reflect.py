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

import html
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
from self_modify.reflect_llm import ReflectLLMError, reflect_chat, resolve_provider
from self_modify.zones import classify_proposal


REFLECTION_PENDING_CAP: int = 3
SMOKE_TIMEOUT_SECS: float = 10.0
# Negative list — how many recent rejections to render in the reflect
# prompt so the model stops re-proposing already-judged specs.
NEGATIVE_LIST_LIMIT: int = 8
# Truncate each rejection's reason at this many chars in the prompt.
# The full reason lives on the DB row for the operator to inspect.
NEGATIVE_LIST_REASON_CHARS: int = 100
# Retry-with-feedback: after a pre-submit reject, this many corrective
# attempts run before the reflect run terminates. Held at 1 to bound
# billing on the paid engines (each attempt is one full LLM call).
CORRECTIVE_RETRIES: int = 1
# Pre-submit outcomes that are eligible for a corrective retry AND get
# persisted to the DB (so the negative list can render them back).
_RETRY_ELIGIBLE_OUTCOMES: tuple[str, ...] = (
    "malformed", "rejected_smoke", "rejected_shape",
)
_OUTCOME_TO_STATUS: dict[str, str] = {
    "malformed": P.STATUS_MALFORMED,
    "rejected_smoke": P.STATUS_REJECTED_SMOKE,
    "rejected_shape": P.STATUS_REJECTED_SHAPE,
}

# Identifier regexes for fields that are inserted RAW into the template
# (only tool_name and derivatives — class_name / const_name).
#
# tool_name STAYS strict snake_case — it becomes a Python class name
# (via ``_snake_to_class_name``) and a file name; the file path is used
# by the zone classifier. Anything outside ``[a-z][a-z0-9_]`` here would
# either produce invalid Python or land on a path the classifier can't
# reason about.
#
# DIGEST_FIELD_RE was relaxed to accept camelCase because real JSON APIs
# routinely return keys like ``poolId``, ``blockCount``, ``avgMatchRate``.
# The prior snake-case-only rule was refusing legitimate specs by
# construction — a spec-format tension, not a real security constraint:
# digest_fields are inserted via ``repr()`` into a list literal (see
# ``TOOL_TEMPLATE``), so the charset stays alphanumeric+underscore and
# no quote/space/metachar can slip past this regex. Injection audit
# lives in tests/test_reflect_injection.py.
SNAKE_CASE_RE = re.compile(r"^[a-z][a-z0-9_]{2,49}$")
DIGEST_FIELD_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]{0,39}$")

# RFC 3986 unreserved + reserved + pct-encoded characters. Anything
# outside this set (quotes, whitespace, backslash, newline, backtick,
# angle brackets, ...) is rejected in URL fields BEFORE render — even
# though the render itself is now via repr() and cannot break out of a
# string literal, a URL with a stray quote or newline is malformed
# input that should never have reached this stage.
URL_ALLOWED_CHARS_RE = re.compile(r"^[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+$")


# ---------- code template ---------------------------------------------------
# Mirrors tools/data_feeds/fear_greed.py structure. The spec's tool_name,
# description, base URL, endpoint path, and digest fields are substituted
# in via str.format; nothing else in the file changes. Model output never
# reaches the code path — only the six spec fields do.

# Every non-identifier substitution goes through Python's repr(), which
# produces a valid string literal regardless of the input. That makes
# quote-breakout and newline-injection STRUCTURALLY IMPOSSIBLE: a `"""`
# in a description becomes '\'\'\'' (or with double-quotes escaped),
# never closing a docstring; a `"` in a URL becomes `\"`, never
# breaking a string boundary. The only fields inserted RAW are
# tool_name and class_name (both validated by SNAKE_CASE_RE upstream);
# every other user-influenced string lands as a Python literal.
TOOL_TEMPLATE = '''"""Morgoth-authored data-feed tool: {tool_name}.

Auto-generated from a spec via self_modify.reflect. See
self_modify_proposals for the proposal row (proposed_by='morgoth').
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx

from core.config import AppConfig, PermissionDeniedError
from tools.base_tool import BaseTool


_BASE_URL = {base_url_repr}
_ENDPOINT_PATH = {endpoint_path_repr}
_SOURCE_LABEL = {source_label_repr}
_TOOL_DESCRIPTION = {description_repr}
_DIGEST_FIELDS = {digest_fields_repr}


class {class_name}(BaseTool):
    __doc__ = _TOOL_DESCRIPTION

    name = {tool_name_repr}
    is_data_source = True
    description = _TOOL_DESCRIPTION
    parameters = {{"type": "object", "properties": {{}}}}

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
            resp = await self._client.get(_BASE_URL + _ENDPOINT_PATH)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            return self.failure(
                f"{{_SOURCE_LABEL}} request failed: {{exc}}",
                source=_SOURCE_LABEL,
            )

        data = resp.json()
        # Best-effort digest: pick the requested fields from a top-level dict
        # OR from the first entry of a top-level list (mirrors the fear_greed
        # pattern for {{"data": [...]}} shaped responses).
        record: dict[str, Any] = {{}}
        if isinstance(data, dict):
            for key in _DIGEST_FIELDS:
                if key in data:
                    record[key] = data[key]
            if not record and isinstance(data.get("data"), list) and data["data"]:
                first = data["data"][0]
                if isinstance(first, dict):
                    for key in _DIGEST_FIELDS:
                        if key in first:
                            record[key] = first[key]
        elif isinstance(data, list) and data and isinstance(data[0], dict):
            for key in _DIGEST_FIELDS:
                if key in data[0]:
                    record[key] = data[0][key]
        if not record:
            return self.failure(
                "response did not contain any of the expected digest fields",
                source=_SOURCE_LABEL,
            )

        fetched_at = datetime.now(timezone.utc).isoformat()
        return self.success(record, source=_SOURCE_LABEL, fetched_at=fetched_at)
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


def _html_normalize(s: str) -> str:
    """Decode HTML entities that the 8B sometimes emits (&amp; → &, etc.).

    Applied to URL fields BEFORE the smoke test. First live reflection
    failed partly on `&amp;` in the query string — normalization
    recovers legitimately-intended URLs without weakening any gate: the
    identifier regex on digest_fields still applies BEFORE render, and
    the URL charset check runs on the normalized string.
    """
    return html.unescape(s) if isinstance(s, str) else s


def _spec_is_well_formed(spec: dict[str, Any]) -> str | None:
    """Return an error string if the spec is malformed; None if OK.

    Rejects at spec-validation level (before render): non-identifier
    tool_name / digest_fields, disallowed URL characters, duplicate
    digest_fields, missing/short description or rationale. The template
    itself now inserts every non-identifier via repr(), so a payload
    that slips past validation still cannot break out of a string
    literal — this is defense in depth.
    """
    tool_name = spec.get("tool_name")
    if not isinstance(tool_name, str) or not SNAKE_CASE_RE.match(tool_name):
        return f"tool_name must match {SNAKE_CASE_RE.pattern}"
    url = spec.get("api_base_url")
    if not isinstance(url, str) or not url:
        return "api_base_url missing"
    if not URL_ALLOWED_CHARS_RE.match(_html_normalize(url)):
        return "api_base_url contains characters outside the RFC 3986 URL set"
    path = spec.get("endpoint_path")
    if not isinstance(path, str):
        return "endpoint_path missing"
    if path and not URL_ALLOWED_CHARS_RE.match(_html_normalize(path)):
        return "endpoint_path contains characters outside the RFC 3986 URL set"
    digest = spec.get("digest_fields")
    if not (isinstance(digest, list) and 3 <= len(digest) <= 6):
        return "digest_fields must be a list of 3-6 items"
    for f in digest:
        if not (isinstance(f, str) and DIGEST_FIELD_RE.match(f)):
            return f"digest field {f!r} is not a snake-case identifier"
    if len(digest) != len(set(digest)):
        return "digest_fields must be unique"
    desc = spec.get("description")
    if not isinstance(desc, str) or len(desc.strip()) < 10:
        return "description too short"
    if not isinstance(spec.get("rationale"), str):
        return "rationale missing"
    return None


def tool_name_collides(
    tool_name: str,
    config: AppConfig,
    pm: PersistentMemory,
) -> bool:
    """True iff ``tool_name`` matches an already-registered tool.

    Uses the same offline discovery path the system-vault compiler
    relies on — instantiates tools with SimpleNamespace stand-ins for
    non-DB collaborators. Collisions must reject the proposal BEFORE
    submission (the pipeline's zone check would also refuse a target
    path where a file already exists, but that's a different failure
    mode; catching it at name-level gives a cleaner error).
    """
    from scripts.compile_wiki import _registered_tools_offline

    existing = {t.name for t in _registered_tools_offline(config, pm)}
    return tool_name in existing


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


async def _smoke_get(url: str) -> tuple[str | None, Any]:
    """Return ``(error, body)`` — body is the JSON-parsed response on
    2xx, otherwise ``None``.

    The old smoke gate stopped at 2xx and never looked at the body.
    Two proposals (cec526ec fee_histogram array, ccb623d1 pools-list
    top-level) passed 2xx while being structurally unusable at the
    template's extraction site. The shape gate (``_shape_check``)
    consumes the returned body; keeping the fetch here means one HTTP
    call, not two.
    """
    try:
        async with httpx.AsyncClient(timeout=SMOKE_TIMEOUT_SECS) as client:
            resp = await client.get(url)
    except httpx.HTTPError as exc:
        return f"network error: {type(exc).__name__}: {exc}", None
    if not 200 <= resp.status_code < 300:
        return f"non-2xx status {resp.status_code}", None
    try:
        body = resp.json()
    except (ValueError, json.JSONDecodeError):
        return "response is not JSON", None
    return None, body


# ---------- shape gate -----------------------------------------------------

# How many response keys to include in a rejected_shape reason. The
# operator needs enough to see "oh, the API returns pools not data",
# but not so many that the log becomes noise.
_SHAPE_KEYS_SHOWN = 15


def _template_extraction_site(
    body: Any, digest_fields: list[str],
) -> tuple[dict[str, Any] | None, str]:
    """Mirror the template's extraction logic — return the dict the
    template will read fields from, plus a human-readable site label.

    Returns ``(None, reason)`` if the body's overall shape is
    unusable (neither a dict with matching top-level keys nor a
    ``{"data": [...]}`` fallback nor a top-level list-of-dicts).

    Kept BYTE-ALIGNED with ``TOOL_TEMPLATE``'s ``execute`` method:
    top-level dict first; if no field matches, ``data[0]`` fallback;
    else top-level list first element. Any drift between the two
    will surface as a false accept or false reject — the injection
    audit tests both extraction sites.
    """
    if isinstance(body, dict):
        if any(f in body for f in digest_fields):
            return body, "top-level dict"
        data = body.get("data")
        if isinstance(data, list) and data and isinstance(data[0], dict):
            return data[0], "data[0] (nested)"
        keys = list(body.keys())
        keys_shown = keys[:_SHAPE_KEYS_SHOWN]
        more = f", …(+{len(keys) - _SHAPE_KEYS_SHOWN} more)" if len(keys) > _SHAPE_KEYS_SHOWN else ""
        return None, (
            f"missing at top-level: {digest_fields} — response is a dict "
            f"but no digest field matches and there is no `data`: "
            f"[<dict>] fallback; real keys: [{', '.join(keys_shown)}{more}]"
        )
    if isinstance(body, list):
        if not body:
            return None, "response is an empty list"
        if not isinstance(body[0], dict):
            return None, (
                f"response is a list but its first element is a "
                f"{type(body[0]).__name__}, not a dict"
            )
        return body[0], "list[0] (top-level list)"
    return None, f"response is neither dict nor list (got {type(body).__name__})"


def _shape_check(body: Any, digest_fields: list[str]) -> str | None:
    """Return None if every ``digest_field`` is extractable AND scalar
    at the template's chosen site; otherwise return a reason string.

    Contract enforced (documents the rejects):

    1. **Extractability**: field must exist at the extraction site the
       template will actually use. Missing → rejected.
    2. **Scalarity**: extracted value must not be ``list`` or ``dict``.
       Array-valued fields (e.g. ``fee_histogram: [[...], [...]]``)
       ARE extractable but break the digest's compact-findings
       contract — the tool's ``findings`` string is truncated by
       downstream consumers, and dumping a nested list makes the
       digest useless. Reject rather than silently produce garbage.
    """
    site, desc = _template_extraction_site(body, digest_fields)
    if site is None:
        return desc
    missing = [f for f in digest_fields if f not in site]
    non_scalar = [
        f for f in digest_fields
        if f in site and isinstance(site[f], (list, dict))
    ]
    problems: list[str] = []
    if missing:
        problems.append(f"missing at {desc}: {missing}")
    if non_scalar:
        problems.append(
            f"array/dict-valued at {desc} (breaks compact digest): {non_scalar}"
        )
    if not problems:
        return None
    keys = list(site.keys())
    keys_shown = keys[:_SHAPE_KEYS_SHOWN]
    more = f", …(+{len(keys) - _SHAPE_KEYS_SHOWN} more)" if len(keys) > _SHAPE_KEYS_SHOWN else ""
    return (
        f"{'; '.join(problems)} — real keys at {desc}: "
        f"[{', '.join(keys_shown)}{more}]"
    )


# ---------- context ---------------------------------------------------------

def _endpoint_from_row(row: dict[str, Any]) -> str | None:
    """Best-effort recover ``endpoint_path`` from a stored rejection row.

    Pre-submit rejections persist the spec as JSON in ``content``.
    Gate-3 rejects (STATUS_REJECTED) instead hold assembled Python; we
    can find ``_ENDPOINT_PATH = <literal>`` there. Silent on anything
    unrecognized — the negative list still renders with just the name.
    """
    content = row.get("content") or ""
    if not isinstance(content, str) or not content:
        return None
    stripped = content.lstrip()
    if stripped.startswith("{"):
        try:
            spec = json.loads(content)
        except (ValueError, TypeError):
            return None
        ep = spec.get("endpoint_path") if isinstance(spec, dict) else None
        return ep if isinstance(ep, str) else None
    m = re.search(r"^_ENDPOINT_PATH\s*=\s*(['\"])(.*?)\1", content, re.M)
    return m.group(2) if m else None


def _rejections_block(rows: list[dict[str, Any]]) -> str:
    """Render recent rejections as a negative-list prompt block.

    Each line: ``- <tool_name>[ (<endpoint_path>)]: <reason>``. Reason
    is truncated at NEGATIVE_LIST_REASON_CHARS so a single verbose
    shape reject doesn't dominate the block. Rendering intentionally
    omits the block header — the caller decides whether to render at
    all (empty history keeps the prompt byte-identical to the pre-
    negative-list version).
    """
    lines: list[str] = []
    for row in rows:
        target = row.get("target_path") or ""
        tool_name = (
            target.rsplit("/", 1)[-1].removesuffix(".py") if target else "unknown"
        )
        endpoint = _endpoint_from_row(row)
        reason = (row.get("status_reason") or "").strip()
        if not reason:
            reason = f"(status={row.get('status')})"
        if len(reason) > NEGATIVE_LIST_REASON_CHARS:
            reason = reason[: NEGATIVE_LIST_REASON_CHARS] + "…"
        head = f"- {tool_name}"
        if endpoint:
            head += f" ({endpoint})"
        lines.append(f"{head}: {reason}")
    return "\n".join(lines)


async def _build_context(pm: PersistentMemory, config: AppConfig) -> dict[str, Any]:
    """Registered tools + objectives + thesis subjects + rejections.

    Reuses the system-vault loaders (compile_wiki._registered_tools_offline
    and _load_tool_usage), same code path as the vault build. Rejections
    come from ``ProposalStore.list_recent_rejections`` — the negative
    list injected to stop the model re-proposing already-judged specs.
    """
    from core.brain import DATA_SOURCE_TOOLS
    from scripts.compile_wiki import _load_tool_usage, _registered_tools_offline

    tools = _registered_tools_offline(config, pm)
    objectives_count, _theses_fed = await _load_tool_usage(pm)
    tool_lines: list[str] = []
    for t in sorted(tools, key=lambda x: x.name):
        # Label from runtime membership, not the class flag. Static-set
        # tools (web_search, historically reddit_search) live outside
        # tools/data_feeds/ and their classes did not carry
        # is_data_source=True — reading the class flag misled the 8B into
        # believing "no social/search data source exists" when both were
        # in the rail.
        kind = "data_source" if t.name in DATA_SOURCE_TOOLS else "chat/util"
        # Full description — no truncation. Measured max across the 18
        # registered tools is 285 chars (see tests/test_reflect_context.py);
        # cutting at 120 was causing the reflect model to propose duplicate
        # coverage because a description's tail ("...and mempool size")
        # never reached the prompt. Total tools_block ≈ 2 KB, well within
        # any ollama num_ctx budget.
        line = (
            f"- {t.name} "
            f"({kind}) "
            f"— objectives_using={objectives_count.get(t.name, 0)}: "
            f"{(getattr(t, 'description', '') or '').strip()}"
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

    store = P.ProposalStore(pm)
    try:
        rej_rows = await store.list_recent_rejections(
            proposed_by="morgoth", limit=NEGATIVE_LIST_LIMIT,
        )
    except Exception as exc:
        # A DB hiccup here must not break reflect — the negative list
        # is opportunistic prompt enrichment, not a gate.
        logger.warning("reflect: negative-list load failed: {}", exc)
        rej_rows = []
    rejections_block = _rejections_block(rej_rows)

    return {
        "tools_block": "\n".join(tool_lines) if tool_lines else "(none)",
        "objectives_block": "\n".join(obj_lines) if obj_lines else "(none)",
        "theses_block": "\n".join(thesis_lines) if thesis_lines else "(none)",
        "rejections_block": rejections_block,
    }


def _reflection_prompt(ctx: dict[str, Any]) -> str:
    """The single prompt sent to the 8B.

    When ``rejections_block`` is empty, no negative-list section is
    emitted at all — the prompt stays BYTE-IDENTICAL to the pre-
    negative-list version. This is the non-regression contract: a
    fresh install (no rejections in the DB) sees the same prompt it
    would have seen before this feature landed.
    """
    negative_list_section = ""
    if ctx.get("rejections_block"):
        negative_list_section = (
            f"\n\nALREADY REJECTED — do NOT re-propose these or trivial variants:\n"
            f"{ctx['rejections_block']}"
        )
    return f"""You are proposing ONE new data-feed tool for Morgoth to add.

CURRENT TOOLS (name, kind, usage, description):
{ctx['tools_block']}

RECENT OBJECTIVE TOPICS (newest first):
{ctx['objectives_block']}

RECENT ACTIVE THESIS SUBJECTS:
{ctx['theses_block']}{negative_list_section}

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


def _corrective_prompt(prompt: str, spec: dict[str, Any] | None, reason: str) -> str:
    """Append a CORRECTION block to the original prompt.

    The corrective attempt sees EVERYTHING the original attempt saw
    plus the rejected spec and the exact gate reason. For shape
    rejects the reason already contains the real API keys the model
    needed — the retry is the model's chance to use them.
    """
    spec_block = json.dumps(spec, indent=2) if spec is not None else "(no spec produced)"
    return (
        f"{prompt}\n\n"
        "YOUR PREVIOUS ATTEMPT WAS REJECTED.\n"
        f"spec:\n{spec_block}\n"
        f"reason: {reason}\n\n"
        "Produce a CORRECTED spec addressing the reason (fix the failing "
        "field(s); pick a different endpoint if the response shape can't "
        "carry a compact scalar digest), or the literal word NONE if no "
        "correction is honest."
    )


# ---------- main entry point -----------------------------------------------

def _short(msg: str, n: int = 200) -> str:
    return msg if len(msg) <= n else msg[:n] + "…"


def _target_path_for_reject(spec: dict[str, Any] | None) -> str:
    """Best-effort target_path for a rejected-spec DB row.

    A malformed spec may not carry a legal tool_name — but the row
    still needs a NOT NULL ``target_path``. We take whatever tool_name
    the spec offers (or ``invalid_spec``) and stuff it under
    ``tools/data_feeds/`` so downstream selectors that filter by
    directory still find the row.
    """
    tn = spec.get("tool_name") if isinstance(spec, dict) else None
    if not (isinstance(tn, str) and tn and SNAKE_CASE_RE.match(tn)):
        tn = "invalid_spec"
    return f"tools/data_feeds/{tn}.py"


async def _persist_pre_submit_reject(
    store: P.ProposalStore,
    outcome: str,
    spec: dict[str, Any] | None,
    reason: str,
    provider: str,
    retry_of: str | None,
) -> str | None:
    """Write a terminal row for a retry-eligible pre-submit reject.

    ``content`` is the spec serialized as JSON — that's the payload
    the next reflect run's negative-list loader parses back to
    surface ``endpoint_path``. Returns the new proposal_id, or None
    on write failure (DB errors here must not break reflect).
    """
    status = _OUTCOME_TO_STATUS[outcome]
    payload = json.dumps(spec, indent=2) if spec else ""
    try:
        return await store.submit_terminal(
            target_path=_target_path_for_reject(spec),
            change_type="new_file",
            content=payload,
            rationale=(spec or {}).get("rationale") if isinstance(spec, dict) else None,
            status=status,
            status_reason=reason,
            proposed_by="morgoth",
            engine=provider,
            retry_of=retry_of,
        )
    except Exception as exc:
        logger.warning(
            "reflect: could not persist {} row (non-fatal): {}", status, exc,
        )
        return None


async def _one_reflect_attempt(
    config: AppConfig,
    pm: PersistentMemory,
    llm: OllamaLLMClient,
    store: P.ProposalStore,
    prompt: str,
    provider: str,
    *,
    retry_of: str | None = None,
) -> dict[str, Any]:
    """Run one attempt from LLM call through every gate to submit.

    Retry-eligible pre-submit rejects (``malformed`` / ``rejected_smoke`` /
    ``rejected_shape``) also get persisted to the DB inside this
    function — the caller consumes the returned ``proposal_id`` when
    building a corrective retry, and the row lands on the negative
    list for future runs. Non-eligible pre-submit outcomes
    (rejected_url / rejected_collision / rejected_zone / unparseable)
    stay ephemeral by design.
    """
    log = lambda msg: logger.info("reflect: {}", msg)  # noqa: E731

    label = f"[retry_of={retry_of[:8]}]" if retry_of else "[first attempt]"
    log(f"engine: {provider} {label}")
    raw, _meta = await reflect_chat(prompt, config, provider, ollama_client=llm)
    log(f"raw spec (first 400 chars): {_short(raw, 400)}")

    # Parse.
    spec = _parse_spec(raw)
    if spec is None:
        if raw.upper().startswith("NONE") or raw.upper() == "":
            log("outcome: NONE (model declined)")
            return {"outcome": "none", "reason": "model declined (NONE)",
                    "proposal_id": None, "pipeline_status": None, "spec": None}
        log("outcome: unparseable")
        return {"outcome": "unparseable",
                "reason": f"could not parse spec: {_short(raw, 300)}",
                "proposal_id": None, "pipeline_status": None, "spec": None}

    # Normalize URL fields BEFORE validation & smoke.
    if isinstance(spec.get("api_base_url"), str):
        spec["api_base_url"] = _html_normalize(spec["api_base_url"])
    if isinstance(spec.get("endpoint_path"), str):
        spec["endpoint_path"] = _html_normalize(spec["endpoint_path"])

    err = _spec_is_well_formed(spec)
    if err:
        log(f"outcome: malformed — {err}")
        pid = await _persist_pre_submit_reject(
            store, "malformed", spec, err, provider, retry_of,
        )
        return {"outcome": "malformed", "reason": err,
                "proposal_id": pid, "pipeline_status": None, "spec": spec}

    if tool_name_collides(spec["tool_name"], config, pm):
        reason = f"tool_name {spec['tool_name']!r} collides with a registered tool"
        log(f"outcome: rejected_collision — {reason}")
        return {"outcome": "rejected_collision", "reason": reason,
                "proposal_id": None, "pipeline_status": None, "spec": spec}

    smoke_target = spec["api_base_url"].rstrip("/") + spec["endpoint_path"]
    url_err = _url_passes_gate(spec["api_base_url"])
    if url_err:
        log(f"outcome: rejected_url — {url_err}")
        return {"outcome": "rejected_url", "reason": url_err,
                "proposal_id": None, "pipeline_status": None, "spec": spec}
    smoke_err, body = await _smoke_get(smoke_target)
    if smoke_err:
        reason = f"smoke test on {smoke_target}: {smoke_err}"
        log(f"outcome: rejected_smoke — {smoke_err}")
        pid = await _persist_pre_submit_reject(
            store, "rejected_smoke", spec, reason, provider, retry_of,
        )
        return {"outcome": "rejected_smoke", "reason": reason,
                "proposal_id": pid, "pipeline_status": None, "spec": spec}

    shape_err = _shape_check(body, list(spec["digest_fields"]))
    if shape_err:
        reason = f"shape check on {smoke_target}: {shape_err}"
        log(f"outcome: rejected_shape — {shape_err}")
        pid = await _persist_pre_submit_reject(
            store, "rejected_shape", spec, reason, provider, retry_of,
        )
        return {"outcome": "rejected_shape", "reason": reason,
                "proposal_id": pid, "pipeline_status": None, "spec": spec}

    # Code assembly (deterministic template, repr-based).
    tool_name = spec["tool_name"]
    class_name = _snake_to_class_name(tool_name)
    source_label = urlparse(spec["api_base_url"]).hostname or ""
    content = TOOL_TEMPLATE.format(
        tool_name=tool_name,
        class_name=class_name,
        tool_name_repr=repr(tool_name),
        base_url_repr=repr(spec["api_base_url"]),
        endpoint_path_repr=repr(spec["endpoint_path"]),
        digest_fields_repr=repr(list(spec["digest_fields"])),
        description_repr=repr(spec["description"]),
        source_label_repr=repr(source_label),
    )
    log(f"code assembled: {len(content)} bytes")

    target_path = f"tools/data_feeds/{tool_name}.py"
    zone = classify_proposal(target_path, "new_file")
    if zone != "green":
        log(f"outcome: rejected_zone — classify_proposal={zone!r}")
        return {"outcome": "rejected_zone",
                "reason": f"pre-submission zone check: {zone}",
                "proposal_id": None, "pipeline_status": None, "spec": spec}

    proposal_id = await store.submit(
        target_path=target_path,
        change_type="new_file",
        content=content,
        rationale=spec["rationale"],
        proposed_by="morgoth",
        engine=provider,
        retry_of=retry_of,
    )
    row = await store.get(proposal_id)
    final_status = await gates.run_pipeline(store, row)
    log(f"outcome: submitted — id={proposal_id[:8]} status={final_status}")
    return {"outcome": "submitted", "reason": final_status,
            "proposal_id": proposal_id, "pipeline_status": final_status,
            "spec": spec}


async def run_reflection(
    config: AppConfig,
    pm: PersistentMemory,
    llm: OllamaLLMClient,
    *,
    provider: str = "ollama",
) -> dict[str, Any]:
    """Attempt one proposal cycle, with one corrective retry on
    retry-eligible pre-submit rejects.

    ``provider`` selects the engine used ONLY for the reflect prompt.
    Prompt, context, and every gate are byte-identical across engines —
    this is a same-input/different-model test. ``provider`` is
    persisted on the proposal row as ``engine``.

    Result shape:
      {'outcome': 'refused_flag' | 'refused_cap' | 'none' | 'unparseable' |
                  'malformed' | 'rejected_url' | 'rejected_smoke' |
                  'rejected_shape' | 'rejected_collision' | 'rejected_zone' |
                  'submitted',
       'reason': str,                      # human-readable
       'proposal_id': str | None,
       'pipeline_status': str | None,      # only when outcome == 'submitted'
       'spec': dict | None,
       'first_attempt': dict | None,       # populated when a retry fired
       'retried': bool}

    Retry policy: if the first attempt lands on malformed / rejected_smoke
    / rejected_shape, ONE corrective attempt runs with the failing spec
    and the exact gate reason injected. The retry goes through EVERY
    gate from parse onward — no gate is skipped. The pending-queue cap
    is checked once at run-start; a retry can only produce ONE
    pending_approval so the cap can't be violated by the retry itself.
    """
    log = lambda msg: logger.info("reflect: {}", msg)  # noqa: E731

    if not config.permissions.permissions.can_self_modify:
        log("refused — can_self_modify=false")
        return {"outcome": "refused_flag", "reason": "can_self_modify is false",
                "proposal_id": None, "pipeline_status": None, "spec": None,
                "first_attempt": None, "retried": False}

    store = P.ProposalStore(pm)

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
                "proposal_id": None, "pipeline_status": None, "spec": None,
                "first_attempt": None, "retried": False}

    ctx = await _build_context(pm, config)
    prompt = _reflection_prompt(ctx)
    n_rej = len(ctx["rejections_block"].splitlines()) if ctx.get("rejections_block") else 0
    log(f"context built: {len(ctx['tools_block'].splitlines())} tools, "
        f"{len(ctx['objectives_block'].splitlines())} objectives, "
        f"{len(ctx['theses_block'].splitlines())} thesis subjects, "
        f"{n_rej} past rejections")

    first = await _one_reflect_attempt(
        config, pm, llm, store, prompt, provider, retry_of=None,
    )
    first_out = first["outcome"]

    if first_out not in _RETRY_ELIGIBLE_OUTCOMES:
        # Not eligible for retry — return the first attempt's result.
        first["first_attempt"] = None
        first["retried"] = False
        return first

    # Retry-with-feedback: build the corrective prompt and run one more
    # attempt. The retry's row (if it also fails eligibly) links back
    # to the first attempt's row via retry_of, so calibration can slice
    # correction-success rate per engine.
    log(f"retry: eligible outcome {first_out!r} — building corrective prompt")
    corrective = _corrective_prompt(prompt, first.get("spec"), first["reason"])
    retry = await _one_reflect_attempt(
        config, pm, llm, store, corrective, provider,
        retry_of=first.get("proposal_id"),
    )
    log(f"retry: outcome {retry['outcome']!r} — final")
    retry["first_attempt"] = {
        "outcome": first_out,
        "reason": first["reason"],
        "proposal_id": first.get("proposal_id"),
        "spec": first.get("spec"),
    }
    retry["retried"] = True
    return retry


# ---------- CLI entry point ------------------------------------------------

async def _main_async(argv: list[str] | None = None) -> int:
    import argparse
    import sys as _sys

    from core.config import load_config

    parser = argparse.ArgumentParser(
        prog="self_modify.reflect",
        description="Run one reflection cycle (Morgoth proposes a new tool).",
    )
    parser.add_argument(
        "--provider",
        choices=("ollama", "anthropic", "claude-cli"),
        default=None,
        help="LLM engine for the reflect prompt. "
             "Overrides REFLECT_PROVIDER env; default 'ollama'.",
    )
    args = parser.parse_args(_sys.argv[1:] if argv is None else argv)

    # Resolve provider with a clean refusal if the env has a typo.
    try:
        provider = resolve_provider(args.provider)
    except ReflectLLMError as exc:
        # Clean CLI refusal — no traceback.
        print(f"reflect: {exc}", file=_sys.stderr)
        return 2

    config = await load_config()
    pm = PersistentMemory(config)
    await pm.initialize()
    llm = OllamaLLMClient(config)
    try:
        try:
            result = await run_reflection(config, pm, llm, provider=provider)
        except ReflectLLMError as exc:
            # Missing key / API errors surface as clean single-line output.
            print(f"reflect: {exc}", file=_sys.stderr)
            return 2
    finally:
        await pm.close()
        await llm.close()

    print(f"provider:        {provider}")
    print(f"outcome:         {result['outcome']}")
    print(f"reason:          {result['reason']}")
    if result.get("retried"):
        first = result.get("first_attempt") or {}
        print("first_attempt:")
        print(f"  outcome:       {first.get('outcome')}")
        print(f"  reason:        {first.get('reason')}")
        if first.get("proposal_id"):
            print(f"  proposal_id:   {first['proposal_id']}")
    if result.get("spec"):
        print("spec:")
        for key, value in result["spec"].items():
            print(f"  {key:16}: {value}")
    if result.get("proposal_id"):
        print(f"proposal_id:     {result['proposal_id']}")
        if result.get("pipeline_status"):
            print(f"pipeline_status: {result['pipeline_status']}")
    return 0 if result["outcome"] in {"submitted", "none"} else 1


def main() -> None:
    import asyncio
    import sys

    sys.exit(asyncio.run(_main_async()))


if __name__ == "__main__":
    main()
