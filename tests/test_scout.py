"""Scout catalog parser, sanitizer, dedupe, probe, and reflect-render.

The scout is untrusted-input in, prompt-safe text out. Every path from
markdown row to reflect prompt has a test here — no live network.

Non-regression contract: when the leads table is empty, the reflect
prompt must be BYTE-IDENTICAL to the pre-scout version. This mirrors
the negative-list byte-identity guarantee.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from self_modify import reflect, scout


# ---------- fixtures ------------------------------------------------------

# Real rows lifted from the public-apis README plus one hostile row.
# Hostile fields test the sanitizer end-to-end.
FIXTURE_MARKDOWN = """
### Cryptocurrency
API | Description | Auth | HTTPS | CORS |
|:---|:---|:---|:---|:---|
| [Bitcambio](https://nova.bitcambio.com.br/api/v3/docs#a-public) | Get the list of all traded assets in the exchange | No | Yes | Unknown |
| [BitcoinCharts](https://bitcoincharts.com/about/exchanges/) | Financial and Technical Data related to the Bitcoin Network | No | Yes | Unknown |
| [Coinlayer](https://coinlayer.com) | Real-time Crypto Currency Exchange Rates | `apiKey` | Yes | Unknown |
| [BitcoinAverage](https://apiv2.bitcoinaverage.com/) | Digital Asset Price Data | `apiKey` | Yes | Unknown |
| [PayloadName](https://evil.example.com) | <script>alert(1)</script>MARKDOWN [inj](x) `code` | No | Yes | Unknown |
| [InsecureAPI](http://insecure.example.com/docs) | HTTP-only tools should never leak into leads | No | Yes | Unknown |

### Anime
API | Description | Auth | HTTPS | CORS |
|:---|:---|:---|:---|:---|
| [AnimeShouldSkip](https://anime.example.com) | Should be filtered out — wrong category | No | Yes | Yes |

### Government
API | Description | Auth | HTTPS | CORS |
|:---|:---|:---|:---|:---|
| [OpenGovKeyless](https://data.example.gov/api) | Government dataset feed | No | Yes | Yes |
"""


# ---------- parser --------------------------------------------------------

def test_parser_filters_by_category_auth_https() -> None:
    rows = scout.parse_catalog(FIXTURE_MARKDOWN)
    names = {r.name for r in rows}
    # 4 crypto rows pass Auth=No + HTTPS=Yes but Coinlayer + BitcoinAverage
    # need apiKey; InsecureAPI is http; AnimeShouldSkip is out-of-category.
    assert "Bitcambio" in names
    assert "BitcoinCharts" in names
    assert "OpenGovKeyless" in names  # Government is allow-listed
    assert "PayloadName" in names  # Hostile row survives PARSE — sanitize catches it
    assert "Coinlayer" not in names  # apiKey — Auth != "No"
    assert "BitcoinAverage" not in names  # apiKey
    # InsecureAPI has HTTPS=Yes in the catalog column but a http:// URL —
    # the parser trusts the column and does NOT re-check the scheme;
    # _sanitize_url is the second gate that catches it (asserted below).
    assert "AnimeShouldSkip" not in names  # not in allow-list


def test_sanitize_url_gate_catches_http_url_that_parser_accepted() -> None:
    """A row whose HTTPS column lies about its own URL is neutralized
    at the _sanitize_url stage — the http:// scheme reduces to None."""
    rows = scout.parse_catalog(FIXTURE_MARKDOWN)
    for r in rows:
        if r.name == "InsecureAPI":
            assert scout._sanitize_url(r.url) is None
            break
    else:
        raise AssertionError("InsecureAPI row missing from parsed set")


def test_parser_extracts_category() -> None:
    rows = scout.parse_catalog(FIXTURE_MARKDOWN)
    by_name = {r.name: r.category for r in rows}
    assert by_name["Bitcambio"] == "Cryptocurrency"
    assert by_name["OpenGovKeyless"] == "Government"


# ---------- sanitizer -----------------------------------------------------

def test_sanitize_strips_markdown_injection_chars() -> None:
    hostile = "<script>alert(1)</script>MARKDOWN [inj](x) `code`"
    clean = scout._sanitize_display(hostile)
    for banned in ("<", ">", "[", "]", "`", "!"):
        assert banned not in clean
    # The visible text survives (letters, digits, spaces, parens).
    assert "script" in clean
    assert "alert" in clean
    assert "MARKDOWN" in clean


def test_sanitize_caps_length() -> None:
    long = "A" * 200
    clean = scout._sanitize_display(long)
    assert len(clean) <= scout.SANITIZE_MAX_CHARS


def test_sanitize_collapses_whitespace() -> None:
    assert scout._sanitize_display("hello   world  \n  tabs\t") == "hello world tabs"


def test_sanitize_url_rejects_http_and_missing_host() -> None:
    assert scout._sanitize_url("http://example.com") is None
    assert scout._sanitize_url("not a url") is None
    assert scout._sanitize_url("https://") is None
    assert scout._sanitize_url("https://example.com") == "https://example.com"


# ---------- filter matrix -------------------------------------------------

def test_filter_matrix_end_to_end() -> None:
    """A row's fate depends on category + auth + https + sanitize outcome."""
    rows = scout.parse_catalog(FIXTURE_MARKDOWN)
    kept = []
    for row in rows:
        name = scout._sanitize_display(row.name)
        url = scout._sanitize_url(row.url)
        if name and url:
            kept.append((name, url))
    urls = {u for _, u in kept}
    # Bitcambio's URL has a fragment (#a-public) which stays intact.
    assert any(u.startswith("https://nova.bitcambio.com.br") for u in urls)
    # PayloadName's URL is https/public-looking — sanitize keeps it.
    # The DEDUPE stage (registered/negative/existing) or PROBE is what
    # would ultimately drop it. Sanitize alone is not a security gate.
    assert any("evil.example.com" in u for u in urls)


# ---------- dedupe --------------------------------------------------------

@pytest.mark.asyncio
async def test_dedupe_against_registered_and_negative_and_existing() -> None:
    """A domain in registered/negative/existing is skipped BEFORE probe."""
    class _FakeTool:
        api_endpoints = ("nova.bitcambio.com.br/api/v3/docs",)

    class _FakeConn:
        async def fetch(self, sql, *args):
            if "self_modify_proposals" in sql:
                # negative list contains a reason mentioning bitcoincharts
                return [{"status_reason": "smoke failed on bitcoincharts.com"}]
            if "api_leads" in sql:
                return []  # nothing pre-existing
            return []

        async def execute(self, *args, **kwargs):
            return None

    class _FakePool:
        def acquire(self):
            class _Ctx:
                async def __aenter__(_self):
                    return _FakeConn()
                async def __aexit__(_self, *a):
                    return None
            return _Ctx()

    pm = MagicMock()
    pm._require_pool = lambda: _FakePool()
    config = SimpleNamespace()

    async def _no_sleep(secs: float) -> None:
        return None

    with patch(
        "scripts.compile_wiki._registered_tools_offline",
        return_value=[_FakeTool()],
    ), patch("self_modify.scout.asyncio.sleep", _no_sleep), patch(
        "self_modify.scout.httpx.AsyncClient"
    ) as mock_client:
        # Catalog fetch: return the fixture markdown.
        catalog_resp = MagicMock()
        catalog_resp.text = FIXTURE_MARKDOWN
        catalog_resp.raise_for_status = MagicMock(return_value=None)
        # Probe fetch: 200 OK for anything.
        probe_resp = MagicMock()
        probe_resp.status_code = 200

        client_mock = AsyncMock()
        client_mock.__aenter__.return_value = client_mock
        client_mock.get = AsyncMock(side_effect=[catalog_resp, probe_resp, probe_resp, probe_resp])
        mock_client.return_value = client_mock

        counts = await scout.run_scout(pm, config, limit=10)

    # Bitcambio → skipped_registered; BitcoinCharts → skipped_negative;
    # PayloadName → sanitized OK, probe attempted; OpenGovKeyless → probed.
    assert counts["skipped_registered"] >= 1
    assert counts["skipped_negative"] >= 1


# ---------- probe status normalization ------------------------------------

@pytest.mark.asyncio
async def test_probe_status_map() -> None:
    async def one(status_code: int) -> str:
        resp = MagicMock()
        resp.status_code = status_code
        client = AsyncMock()
        client.get = AsyncMock(return_value=resp)
        return await scout._probe_url(client, "https://example.com")

    assert await one(200) == "alive"
    assert await one(301) == "alive"
    assert await one(403) == "blocked"
    assert await one(404) == "blocked"
    assert await one(500) == "dead"


@pytest.mark.asyncio
async def test_probe_timeout_is_dead() -> None:
    client = AsyncMock()
    client.get = AsyncMock(side_effect=httpx.TimeoutException("t"))
    assert await scout._probe_url(client, "https://example.com") == "dead"


@pytest.mark.asyncio
async def test_probe_connect_error_is_dead() -> None:
    client = AsyncMock()
    client.get = AsyncMock(side_effect=httpx.ConnectError("nope"))
    assert await scout._probe_url(client, "https://example.com") == "dead"


# ---------- politeness ----------------------------------------------------

@pytest.mark.asyncio
async def test_probe_stage_sleeps_between_requests() -> None:
    """The scout must call asyncio.sleep(PROBE_INTERVAL_SECS) between
    each probe after the first, so an unauthenticated bulk crawl of
    unrelated hosts stays polite."""
    calls: list[float] = []

    async def _capture_sleep(secs: float) -> None:
        calls.append(secs)

    class _FakeConn:
        async def fetch(self, sql, *args):
            return []
        async def execute(self, *args, **kwargs):
            return None

    class _FakePool:
        def acquire(self):
            class _Ctx:
                async def __aenter__(_self):
                    return _FakeConn()
                async def __aexit__(_self, *a):
                    return None
            return _Ctx()

    pm = MagicMock()
    pm._require_pool = lambda: _FakePool()
    config = SimpleNamespace()

    with patch(
        "scripts.compile_wiki._registered_tools_offline", return_value=[]
    ), patch("self_modify.scout.asyncio.sleep", _capture_sleep), patch(
        "self_modify.scout._host_looks_public", return_value=None
    ), patch(
        "self_modify.scout.httpx.AsyncClient"
    ) as mock_client:
        catalog_resp = MagicMock()
        catalog_resp.text = FIXTURE_MARKDOWN
        catalog_resp.raise_for_status = MagicMock(return_value=None)
        probe_resp = MagicMock()
        probe_resp.status_code = 200

        client_mock = AsyncMock()
        client_mock.__aenter__.return_value = client_mock
        # 1 catalog fetch + N probes
        client_mock.get = AsyncMock(
            side_effect=[catalog_resp] + [probe_resp] * 20
        )
        mock_client.return_value = client_mock

        counts = await scout.run_scout(pm, config, limit=10)

    # After the first probe, every subsequent probe should pay ONE
    # PROBE_INTERVAL_SECS wait.
    expected_waits = max(0, counts["probed"] - 1)
    assert len(calls) == expected_waits
    if expected_waits:
        assert all(w == scout.PROBE_INTERVAL_SECS for w in calls)


# ---------- reflect context render ---------------------------------------

@pytest.mark.asyncio
async def test_leads_block_empty_when_no_alive_rows() -> None:
    """No alive leads → empty string → the prompt LEADS section is
    OMITTED entirely (byte-identical to the pre-scout render)."""
    pm = MagicMock()

    async def _fake_ensure(_pm):
        return None
    async def _fake_fetch(_pm, *, limit=12):
        return []

    with patch("self_modify.scout.ensure_leads_table", _fake_ensure), patch(
        "self_modify.scout.fetch_alive_leads", _fake_fetch
    ):
        block = await scout.leads_block_for_reflect(pm)
    assert block == ""


@pytest.mark.asyncio
async def test_leads_block_sanitizes_and_renders() -> None:
    pm = MagicMock()
    fake_leads = [
        {
            "name": "SafeApi",
            "domain": "api.safe.example.com",
            "category": "Cryptocurrency",
            "description": "clean text",
        },
        {
            "name": "MaliciousName<script>",
            "domain": "evil.example.com",
            "category": "Finance",
            "description": "`bad` [ok](x)",
        },
    ]

    async def _fake_ensure(_pm):
        return None
    async def _fake_fetch(_pm, *, limit=12):
        return fake_leads

    with patch("self_modify.scout.ensure_leads_table", _fake_ensure), patch(
        "self_modify.scout.fetch_alive_leads", _fake_fetch
    ):
        block = await scout.leads_block_for_reflect(pm)

    assert "SafeApi" in block
    assert "api.safe.example.com" in block
    assert "MaliciousName" in block  # sanitized survivor
    for banned in ("<", ">", "[", "]", "`", "!"):
        assert banned not in block


# ---------- reflect prompt byte-identity when leads empty ----------------

def _base_ctx() -> dict[str, Any]:
    return {
        "tools_block": "- t (data_source) — objectives_using=0: desc",
        "objectives_block": "- an objective",
        "theses_block": "- a subject",
        "rejections_block": "",
        "leads_block": "",
    }


def test_prompt_omits_leads_section_when_empty() -> None:
    ctx = _base_ctx()
    prompt = reflect._reflection_prompt(ctx)
    assert "FREE-API LEADS" not in prompt


def test_prompt_omits_leads_task_line_when_empty() -> None:
    ctx = _base_ctx()
    prompt = reflect._reflection_prompt(ctx)
    assert "Your spec MAY target a lead's domain" not in prompt


def test_prompt_byte_identical_without_leads_key() -> None:
    """A ctx dict that pre-dates the scout key must render identically
    to one carrying leads_block='' — future-proofs the non-regression
    guarantee against a caller that doesn't know about scout."""
    ctx_new = _base_ctx()
    ctx_old = _base_ctx()
    del ctx_old["leads_block"]
    assert reflect._reflection_prompt(ctx_new) == reflect._reflection_prompt(ctx_old)


def test_prompt_includes_leads_section_when_populated() -> None:
    ctx = _base_ctx()
    ctx["leads_block"] = "- SomeApi (Cryptocurrency, some.example.com)"
    prompt = reflect._reflection_prompt(ctx)
    assert "FREE-API LEADS" in prompt
    assert "SomeApi" in prompt
    assert "MUST still declare an exact endpoint_path" in prompt


# ---------- host guard: bitnodes-class regression ------------------------

def test_host_public_reason_distinguishes_resolve_failure() -> None:
    """A resolve failure returns 'dns resolve failed', NOT the old
    misleading 'not a public DNS host'."""
    reason = scout._host_looks_public("bitnodes.io.definitely.does.not.exist.invalid")
    assert reason is not None
    assert "resolve failed" in reason.lower()


def test_host_public_reason_rejects_ip_literal() -> None:
    reason = scout._host_looks_public("192.168.1.1")
    assert reason is not None
    assert "ip literal" in reason.lower()


def test_host_public_reason_rejects_localhost() -> None:
    reason = scout._host_looks_public("localhost")
    assert reason is not None
    assert "localhost" in reason.lower()


def test_host_public_reason_accepts_public_host() -> None:
    """A DNS-resolving host with a public IP passes the guard.

    Uses a mocked resolver so the test does not depend on the network
    (the reflect sandbox runs under ``unshare --net`` — a live-DNS
    test here reports 'dns resolve failed' and false-fails gate_tests
    on every reflect walk, killing every proposal at the sandbox
    stage regardless of merit).
    """
    import socket as _socket
    fake_infos = [(_socket.AF_INET, _socket.SOCK_STREAM, 0, "",
                   ("104.16.0.1", 0))]
    with patch.object(scout.socket, "getaddrinfo", return_value=fake_infos):
        reason = scout._host_looks_public("example.com")
    assert reason is None


def test_reflect_url_gate_message_distinguishes_resolve_from_private() -> None:
    """A resolve failure produces 'dns resolve failed' in the reject
    reason, not the pre-fix 'not a public DNS host' string that
    misled the operator on bitnodes.io."""
    err = reflect._url_passes_gate("https://bitnodes.io.does.not.exist.invalid/x")
    assert err is not None
    assert "dns resolve failed" in err.lower()
    assert "not a public DNS host" not in err
