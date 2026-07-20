"""SCOUT: a live-catalog lead generator for the reflect proposer.

Invoked ONLY from the CLI (``python -m self_modify.scout``). Never
imported by ``core/brain.py`` or ``self_modify/apply.py`` — the scout
is a docs-page liveness filter that seeds reflect's prompt with
currently-existing free-API landing pages, so the 8B stops writing
specs against services retired years ago.

Contract — read this before adding a rule
-----------------------------------------
1. The catalog is UNTRUSTED INPUT. Every field entering the prompt
   passes through ``_sanitize_display`` (whitelisted charset, length
   cap). URLs pass a host guard that reuses the same public-DNS check
   the reflect pre-smoke gate uses.
2. This is NOT endpoint verification. A GET on a docs landing page
   proves the domain answers HTTPS today; the tool file's actual API
   endpoint is still verified by the 14 reflect gates (smoke, shape,
   liveness, freshness, name-coherence, endpoint-dup, etc). A lead
   passing the probe grants no rail credit; it only makes the domain
   available to the LLM as a discovery target.
3. Dedupe is protective, not creative: domains already covered by a
   registered tool's ``api_endpoints`` are skipped (avoids the model
   re-proposing something we have), and domains that appeared in a
   recent negative-list rejection reason are skipped (the reason for
   rejection may still hold — don't burn a slot to re-learn it).
4. Politeness: ONE request per second, 10s timeout, no auth headers,
   default User-Agent. The catalog source is a public GitHub README.

Bug class this closes
---------------------
Reflect's spec source was claude-cli training memory. Multiple
proposals (bitnodes.io, an old cryptocompare path, several retired
CoinCap endpoints) died at the SMOKE or SHAPE gate because the
service had gone dark since the model's training cutoff. The scout
grounds the prompt in a live catalog scraped at run time.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
import uuid
from collections.abc import Iterable, Sequence
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import httpx
from loguru import logger


# ---------- constants (env-overridable) -----------------------------------

CATALOG_URL: str = os.environ.get(
    "SCOUT_CATALOG_URL",
    "https://raw.githubusercontent.com/public-apis/public-apis/master/README.md",
)

# Default section allow-list. Env override is a comma-separated list;
# empty override falls back to the default set. Values must match the
# catalog's ``### <Name>`` headings exactly (case-sensitive).
_DEFAULT_CATEGORIES: frozenset[str] = frozenset({
    "Cryptocurrency",
    "Finance",
    "Currency Exchange",
    "Open Data",
    "Government",
})


def _load_allowed_categories() -> frozenset[str]:
    """Read SCOUT_CATEGORIES env override or return the default set."""
    raw = (os.environ.get("SCOUT_CATEGORIES") or "").strip()
    if not raw:
        return _DEFAULT_CATEGORIES
    items = {p.strip() for p in raw.split(",") if p.strip()}
    return frozenset(items) if items else _DEFAULT_CATEGORIES


ALLOWED_CATEGORIES: frozenset[str] = _load_allowed_categories()

# Sanitizer: name + description must reduce to this charset. Anything
# else is stripped (not escaped) — an entry that survives is safe to
# concatenate into the prompt without markdown/injection carry-over.
_SANITIZE_ALLOWED_RE = re.compile(r"[^A-Za-z0-9 .,()/\-]")
SANITIZE_MAX_CHARS: int = 80

# Catalog fetch: catch a hung TCP with a hard cap. The README is ~220KB.
CATALOG_FETCH_TIMEOUT_SECS: float = 20.0

# Probe: keep the connection ceiling small; catalog can have many hosts.
PROBE_TIMEOUT_SECS: float = 10.0
# One request per second — an unauthenticated crawl of unrelated hosts,
# so we're friendly by default. Overridable for tests only.
PROBE_INTERVAL_SECS: float = 1.0

# Default per-run cap. The CLI --limit flag overrides this at the call
# site so a bulk backfill can pass a larger number.
DEFAULT_SCOUT_LIMIT: int = 15

# Reflect context: how many alive leads to render at most.
REFLECT_LEADS_MAX: int = 12
# Truncate any rendered description at this width so the block stays
# compact even if a catalog description happens to be near 80 chars.
LEAD_LINE_DESC_MAX: int = 60


# ---------- markdown parser ------------------------------------------------

# Category heading: '### <Name>' on its own line.
_HEADING_RE = re.compile(r"^###\s+(.+?)\s*$", re.MULTILINE)
# Table row: '| [Label](url) | description | Auth | HTTPS | CORS |'
# The label may include spaces/punctuation; use non-greedy up to ']('.
_ROW_RE = re.compile(
    r"^\|\s*\[(?P<name>[^\]]+?)\]\((?P<url>https?://[^)\s]+)\)\s*\|"
    r"\s*(?P<desc>.*?)\s*\|"
    r"\s*(?P<auth>[^|]*?)\s*\|"
    r"\s*(?P<https>[^|]*?)\s*\|"
    r"\s*(?P<cors>[^|]*?)\s*\|"
    r"\s*$",
    re.MULTILINE,
)


class ParsedRow:
    """A raw table row after markdown parsing but BEFORE sanitization."""

    __slots__ = ("name", "url", "description", "auth", "https", "category")

    def __init__(
        self,
        *,
        name: str,
        url: str,
        description: str,
        auth: str,
        https: str,
        category: str,
    ) -> None:
        self.name = name
        self.url = url
        self.description = description
        self.auth = auth
        self.https = https
        self.category = category

    def __repr__(self) -> str:  # pragma: no cover
        return f"ParsedRow(name={self.name!r}, url={self.url!r}, cat={self.category!r})"


def parse_catalog(markdown: str) -> list[ParsedRow]:
    """Extract every table row and tag it with its enclosing ### section.

    Only rows whose category is in ALLOWED_CATEGORIES and whose
    Auth/HTTPS columns match ``No``/``Yes`` are returned. The category
    filter runs BEFORE the row regex on each section so we only pay
    the regex cost on candidate sections.
    """
    result: list[ParsedRow] = []
    headings = list(_HEADING_RE.finditer(markdown))
    for i, m in enumerate(headings):
        category = m.group(1).strip()
        if category not in ALLOWED_CATEGORIES:
            continue
        start = m.end()
        end = headings[i + 1].start() if i + 1 < len(headings) else len(markdown)
        section = markdown[start:end]
        for rm in _ROW_RE.finditer(section):
            auth = rm.group("auth").strip().strip("`").lower()
            https = rm.group("https").strip().lower()
            # Auth==No AND HTTPS==Yes — strict.
            if auth != "no" or https != "yes":
                continue
            result.append(
                ParsedRow(
                    name=rm.group("name").strip(),
                    url=rm.group("url").strip(),
                    description=rm.group("desc").strip(),
                    auth=auth,
                    https=https,
                    category=category,
                )
            )
    return result


# ---------- sanitization (untrusted input hygiene) ------------------------

def _sanitize_display(value: str, *, max_chars: int = SANITIZE_MAX_CHARS) -> str:
    """Reduce to the whitelisted charset and cap length.

    Any character outside ``[A-Za-z0-9 .,()/-]`` is removed. This is
    stronger than escaping — a hostile row cannot survive as a
    partially-encoded token that a downstream renderer would decode.
    Consecutive whitespace collapses to single spaces after removal.
    """
    if not isinstance(value, str):
        return ""
    cleaned = _SANITIZE_ALLOWED_RE.sub(" ", value)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if len(cleaned) > max_chars:
        cleaned = cleaned[:max_chars].rstrip()
    return cleaned


def _sanitize_url(url: str) -> str | None:
    """Return the URL iff it parses as https + has a hostname.

    This is the pre-probe sanitizer. The live probe additionally
    passes the host through ``_host_looks_public`` to reject private
    or unresolvable targets. Keep the two-step separation: parsing
    is cheap, DNS is not.
    """
    if not isinstance(url, str) or not url:
        return None
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return None
    if parsed.scheme != "https":
        return None
    if not parsed.hostname:
        return None
    return url.strip()


# ---------- host guard (reuses reflect's SSRF posture) --------------------

def _host_looks_public(host: str) -> str | None:
    """Return None if public, else a short reason string.

    Distinguishes 'resolve failed' from 'resolves to private' —
    the reflect ``_host_is_public`` collapses both into a single False
    which produces the misleading 'not a public DNS host' message on
    a legit host with a temporary DNS glitch. Kept conservative: any
    private / loopback / link-local / reserved / multicast address on
    ANY resolved record still rejects.
    """
    import ipaddress
    import socket

    if not host or host.lower() == "localhost":
        return "localhost is not public"
    # IP literal — reject regardless of range so operator error surfaces.
    try:
        ipaddress.ip_address(host)
        return "url uses an IP literal, not a DNS host"
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return "dns resolve failed"
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            return "unparseable resolved address"
        if (
            ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_reserved or ip.is_multicast
        ):
            return f"resolves to non-public address {addr}"
    return None


# ---------- domain extraction ---------------------------------------------

def _domain_from_url(url: str) -> str | None:
    """Return the hostname (no port, no path). Bare IPs bounce."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    host = parsed.hostname
    if not host:
        return None
    return host.lower()


# ---------- registered-tool + negative-list dedupe ------------------------

def _registered_domains(config: Any, pm: Any) -> frozenset[str]:
    """Domains already covered by a registered tool's api_endpoints.

    The api_endpoints value is normalized ``host/path``; extract just
    the host segment. Empty tuples (search / dynamic-path tools) are
    ignored — they don't reserve a domain in the dedupe sense.
    """
    from scripts.compile_wiki import _registered_tools_offline

    domains: set[str] = set()
    for tool in _registered_tools_offline(config, pm):
        endpoints: Sequence[str] = getattr(tool, "api_endpoints", ()) or ()
        for ep in endpoints:
            if not ep:
                continue
            head = ep.split("/", 1)[0].strip().lower()
            if head:
                domains.add(head)
    return frozenset(domains)


async def _negative_list_domains(pm: Any) -> frozenset[str]:
    """Domains that appear in a recent rejection's status_reason.

    Uses a broad LIKE-based scan of the last 200 pre-submit rejects.
    The domain-token match is intentionally loose (a substring check
    of the reason against each parsed lead's domain later) — but here
    we materialize the set of *known bad* domains cheaply by extracting
    hostnames referenced in the reason column.
    """
    try:
        pool = pm._require_pool()  # noqa: SLF001
    except Exception:
        return frozenset()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT status_reason FROM self_modify_proposals "
            "WHERE status_reason IS NOT NULL "
            "ORDER BY updated_at DESC LIMIT 200"
        )
    _HOST_TOKEN_RE = re.compile(
        r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}",
        re.IGNORECASE,
    )
    domains: set[str] = set()
    for r in rows:
        reason = r.get("status_reason") if isinstance(r, dict) else r["status_reason"]
        if not reason:
            continue
        for m in _HOST_TOKEN_RE.finditer(reason):
            domains.add(m.group(0).lower())
    return frozenset(domains)


# ---------- table ensure + persistence ------------------------------------

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS api_leads (
    lead_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(80) NOT NULL,
    domain VARCHAR(255) NOT NULL UNIQUE,
    url TEXT NOT NULL,
    category VARCHAR(80) NOT NULL,
    description VARCHAR(160) NOT NULL DEFAULT '',
    probe_status VARCHAR(20) NOT NULL DEFAULT 'pending',
    probed_at TIMESTAMPTZ,
    source VARCHAR(255) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""


async def ensure_leads_table(pm: Any) -> None:
    """Create api_leads if missing. Idempotent."""
    try:
        pool = pm._require_pool()  # noqa: SLF001
    except Exception as exc:
        raise RuntimeError(f"scout: pm has no pool: {exc}") from exc
    async with pool.acquire() as conn:
        await conn.execute(_CREATE_TABLE_SQL)


async def _existing_lead_domains(pm: Any) -> frozenset[str]:
    """Domains already persisted in api_leads (any probe_status)."""
    pool = pm._require_pool()  # noqa: SLF001
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT domain FROM api_leads")
    return frozenset((r["domain"] or "").lower() for r in rows)


async def _insert_lead(
    pm: Any,
    *,
    name: str,
    domain: str,
    url: str,
    category: str,
    description: str,
    probe_status: str,
    probed_at: datetime | None,
    source: str,
) -> None:
    """Insert one lead. Domain is UNIQUE — race-safe via ON CONFLICT."""
    pool = pm._require_pool()  # noqa: SLF001
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO api_leads "
            "(name, domain, url, category, description, probe_status, probed_at, source) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8) "
            "ON CONFLICT (domain) DO NOTHING",
            name,
            domain,
            url,
            category,
            description,
            probe_status,
            probed_at,
            source,
        )


async def fetch_alive_leads(pm: Any, *, limit: int = REFLECT_LEADS_MAX) -> list[dict[str, Any]]:
    """Alive leads, crypto/finance-first, rotating by lead age.

    The rotation is deterministic given the DB clock: order alive
    rows by category priority, then oldest-first so leads that never
    made it into the prompt eventually surface. Reflect calls this
    once per run — no need for a caching layer.
    """
    priority = ("Cryptocurrency", "Finance", "Currency Exchange", "Open Data", "Government")
    pool = pm._require_pool()  # noqa: SLF001
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT name, domain, category, description "
            "FROM api_leads WHERE probe_status = 'alive'"
        )
    # Sort in Python so the priority ordering matches the intended
    # tuple even if a new category is added out of alphabetical order.
    def sort_key(r: Any) -> tuple[int, str]:
        cat = r["category"]
        try:
            idx = priority.index(cat)
        except ValueError:
            idx = len(priority)
        return (idx, r["domain"])
    ordered = sorted(rows, key=sort_key)
    return [dict(r) for r in ordered[:limit]]


# ---------- probe ---------------------------------------------------------

async def _probe_url(client: httpx.AsyncClient, url: str) -> str:
    """Return 'alive' | 'dead' | 'blocked' for one URL.

    ``alive`` = any 2xx or 3xx; ``blocked`` = 4xx (a docs page behind
    a login wall or a 403); ``dead`` = 5xx, connection failure, or
    timeout. This is a coarse triage — the reflect gates verify the
    actual API endpoint. Blocked leads are still persisted (a docs
    page can be behind CF while the API works fine) but don't feed
    the prompt.
    """
    try:
        resp = await client.get(url, timeout=PROBE_TIMEOUT_SECS)
    except (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError):
        return "dead"
    except httpx.HTTPError:
        return "dead"
    code = resp.status_code
    if 200 <= code < 400:
        return "alive"
    if 400 <= code < 500:
        return "blocked"
    return "dead"


# ---------- orchestrator ---------------------------------------------------

async def run_scout(pm: Any, config: Any, *, limit: int = DEFAULT_SCOUT_LIMIT) -> dict[str, int]:
    """One scout pass. Returns counts for the CLI to print.

    Steps: fetch catalog → parse → filter+sanitize → dedupe against
    registered tools + negative-list + existing leads → probe (polite)
    → persist. Persists ALL probed leads including 'blocked'/'dead' so
    a future run can skip them without paying the DNS/HTTP cost again.
    """
    await ensure_leads_table(pm)

    async with httpx.AsyncClient(timeout=CATALOG_FETCH_TIMEOUT_SECS, follow_redirects=True) as client:
        resp = await client.get(CATALOG_URL)
        resp.raise_for_status()
        markdown = resp.text

    parsed = parse_catalog(markdown)
    counts = {
        "parsed": len(parsed),
        "sanitized": 0,
        "skipped_registered": 0,
        "skipped_negative": 0,
        "skipped_existing": 0,
        "skipped_bad_host": 0,
        "probed": 0,
        "alive": 0,
        "dead": 0,
        "blocked": 0,
    }

    registered = _registered_domains(config, pm)
    negative = await _negative_list_domains(pm)
    existing = await _existing_lead_domains(pm)

    # First stage: sanitize + dedupe (cheap). Only the survivors reach
    # the probe stage which pays DNS + one HTTP request per lead.
    to_probe: list[tuple[ParsedRow, str, str, str]] = []
    for row in parsed:
        name = _sanitize_display(row.name)
        desc = _sanitize_display(row.description, max_chars=160)
        url = _sanitize_url(row.url)
        if not name or not url:
            continue
        domain = _domain_from_url(url)
        if not domain:
            continue
        counts["sanitized"] += 1
        if domain in registered:
            counts["skipped_registered"] += 1
            continue
        if domain in negative:
            counts["skipped_negative"] += 1
            continue
        if domain in existing:
            counts["skipped_existing"] += 1
            continue
        host_reason = _host_looks_public(domain)
        if host_reason is not None:
            counts["skipped_bad_host"] += 1
            continue
        to_probe.append((row, name, url, desc))
        if len(to_probe) >= limit:
            break

    async with httpx.AsyncClient(follow_redirects=True) as client:
        for i, (row, name, url, desc) in enumerate(to_probe):
            if i > 0:
                await asyncio.sleep(PROBE_INTERVAL_SECS)
            status = await _probe_url(client, url)
            counts[status] += 1
            counts["probed"] += 1
            domain = _domain_from_url(url) or ""
            await _insert_lead(
                pm,
                name=name,
                domain=domain,
                url=url,
                category=row.category,
                description=desc,
                probe_status=status,
                probed_at=datetime.now(timezone.utc),
                source=CATALOG_URL,
            )
            logger.info("scout: {} → {}", domain, status)

    return counts


# ---------- reflect block renderer ---------------------------------------

async def leads_block_for_reflect(pm: Any) -> str:
    """Rendered block ready to slot into the reflect prompt.

    Empty string when the table has no alive rows — reflect uses this
    as a signal to omit the LEADS section entirely so the prompt stays
    byte-identical to the pre-scout version on a fresh install.
    """
    try:
        await ensure_leads_table(pm)
    except Exception as exc:
        logger.warning("scout: leads_block ensure_table failed (non-fatal): {}", exc)
        return ""
    try:
        leads = await fetch_alive_leads(pm, limit=REFLECT_LEADS_MAX)
    except Exception as exc:
        logger.warning("scout: leads_block fetch failed (non-fatal): {}", exc)
        return ""
    if not leads:
        return ""
    lines: list[str] = []
    for lead in leads:
        # Every value passed through _sanitize_display at persist time,
        # so a re-render here is defense-in-depth against a manually
        # inserted row escaping the sanitizer.
        name = _sanitize_display(lead["name"])
        domain = _sanitize_display(lead["domain"])
        category = _sanitize_display(lead["category"])
        desc = _sanitize_display(lead.get("description") or "", max_chars=LEAD_LINE_DESC_MAX)
        head = f"- {name} ({category}, {domain})"
        if desc:
            head += f": {desc}"
        lines.append(head)
    return "\n".join(lines)


# ---------- CLI -----------------------------------------------------------

def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="morgoth-scout",
        description="Fetch the public-apis catalog, filter+probe, persist as leads",
    )
    parser.add_argument("--limit", type=int, default=DEFAULT_SCOUT_LIMIT)
    return parser


async def _amain(limit: int) -> int:
    from core.config import load_config
    from memory.persistent import PersistentMemory

    config = await load_config()
    pm = PersistentMemory(config)
    await pm.initialize()
    try:
        counts = await run_scout(pm, config, limit=limit)
    finally:
        await pm.close()
    print("scout counts:")
    for k in ("parsed", "sanitized", "skipped_registered", "skipped_negative",
              "skipped_existing", "skipped_bad_host", "probed", "alive",
              "dead", "blocked"):
        print(f"  {k:24s} {counts[k]}")
    return 0


def main() -> int:
    args = _build_arg_parser().parse_args()
    try:
        return asyncio.run(_amain(args.limit))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
