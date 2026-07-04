"""Reflection-job tests: gates, parsing, smoke, code assembly, submission.

Every external touch (DB, LLM, HTTP) is mocked. Real behavior of the
underlying pipeline (gate_zone, gate_tests) is not exercised here — that
is covered by tests/test_self_modify_pipeline.py.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from self_modify import reflect


VALID_SPEC = {
    "tool_name": "get_coingecko_market_dominance",
    "api_base_url": "https://api.coingecko.com",
    "endpoint_path": "/api/v3/global",
    "digest_fields": ["market_cap_percentage", "active_cryptocurrencies", "markets"],
    "description": "Fetch the current crypto market dominance breakdown from CoinGecko.",
    "rationale": "Morgoth has no direct dominance signal; the current tools cover price and on-chain but not market share.",
}


def _fake_config(can_self_modify: bool) -> SimpleNamespace:
    """Build a minimal config with just the permission flags reflect reads."""
    return SimpleNamespace(
        permissions=SimpleNamespace(
            permissions=SimpleNamespace(can_self_modify=can_self_modify),
        ),
    )


# ---------- parsing --------------------------------------------------------

def test_parse_spec_none_variants() -> None:
    assert reflect._parse_spec("NONE") is None
    assert reflect._parse_spec("  none  ") is None
    assert reflect._parse_spec("None — no gap found") is None
    assert reflect._parse_spec("") is None
    assert reflect._parse_spec(None) is None  # type: ignore[arg-type]


def test_parse_spec_json_with_fences() -> None:
    text = '```json\n{"tool_name": "x", "digest_fields": ["a"]}\n```'
    parsed = reflect._parse_spec(text)
    assert parsed == {"tool_name": "x", "digest_fields": ["a"]}


def test_parse_spec_json_with_prose_around() -> None:
    text = (
        "Here is my proposal:\n"
        '{"tool_name": "x", "digest_fields": ["a"]}\n'
        "Hope this helps!"
    )
    assert reflect._parse_spec(text) == {"tool_name": "x", "digest_fields": ["a"]}


def test_parse_spec_unparseable_returns_none() -> None:
    assert reflect._parse_spec("this is not json at all") is None
    assert reflect._parse_spec("{ not valid json") is None


def test_spec_well_formed_matrix() -> None:
    assert reflect._spec_is_well_formed(VALID_SPEC) is None
    # tool_name invalid
    bad = dict(VALID_SPEC, tool_name="BadName")
    assert "tool_name" in reflect._spec_is_well_formed(bad)
    bad = dict(VALID_SPEC, tool_name="a")  # too short
    assert reflect._spec_is_well_formed(bad) is not None
    # api_base_url missing
    bad = dict(VALID_SPEC)
    del bad["api_base_url"]
    assert reflect._spec_is_well_formed(bad) is not None
    # digest_fields wrong shape
    bad = dict(VALID_SPEC, digest_fields=["a", "b"])  # too few
    assert reflect._spec_is_well_formed(bad) is not None
    bad = dict(VALID_SPEC, digest_fields=["a", "b", "c", "d", "e", "f", "g"])  # too many
    assert reflect._spec_is_well_formed(bad) is not None
    # camelCase is now ACCEPTED (real APIs return camelCase keys, and
    # the field is inserted via repr() so injection-neutral).
    ok = dict(VALID_SPEC, digest_fields=["poolId", "blockCount", "avgMatchRate"])
    assert reflect._spec_is_well_formed(ok) is None
    # But identifiers with metacharacters or whitespace stay rejected.
    bad = dict(VALID_SPEC, digest_fields=["has space", "b_ok", "c_ok"])
    assert reflect._spec_is_well_formed(bad) is not None
    bad = dict(VALID_SPEC, digest_fields=["has-dash", "b_ok", "c_ok"])
    assert reflect._spec_is_well_formed(bad) is not None


# ---------- URL / smoke ----------------------------------------------------

def test_url_gate_rejects_non_https() -> None:
    assert reflect._url_passes_gate("http://api.example.com") is not None
    assert reflect._url_passes_gate("ftp://api.example.com") is not None


def test_url_gate_rejects_ip_and_localhost(monkeypatch: pytest.MonkeyPatch) -> None:
    # Direct IP literals are refused before DNS is consulted.
    assert reflect._url_passes_gate("https://127.0.0.1") is not None
    assert reflect._url_passes_gate("https://10.0.0.1") is not None
    # localhost hostname is refused too.
    assert reflect._url_passes_gate("https://localhost") is not None


def test_url_gate_rejects_dns_that_resolves_to_private_range() -> None:
    """A hostname that resolves into a private range must be refused."""
    with patch.object(
        reflect, "socket",
        MagicMock(getaddrinfo=MagicMock(return_value=[
            (None, None, None, None, ("192.168.1.5", 0))
        ])),
    ):
        assert reflect._url_passes_gate("https://internal.example.com") is not None


def test_url_gate_accepts_public_dns() -> None:
    with patch.object(
        reflect, "socket",
        MagicMock(getaddrinfo=MagicMock(return_value=[
            (None, None, None, None, ("104.16.0.1", 0))
        ])),
    ):
        assert reflect._url_passes_gate("https://api.example.com") is None


@pytest.mark.asyncio
async def test_smoke_get_reports_non_2xx() -> None:
    fake_client = AsyncMock()
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)
    fake_client.get = AsyncMock(return_value=SimpleNamespace(status_code=404))
    with patch.object(reflect.httpx, "AsyncClient", return_value=fake_client):
        err, body = await reflect._smoke_get("https://example.com/x")
    assert err is not None and "404" in err
    assert body is None


@pytest.mark.asyncio
async def test_smoke_get_reports_network_error() -> None:
    import httpx

    fake_client = AsyncMock()
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)
    fake_client.get = AsyncMock(side_effect=httpx.ConnectError("boom"))
    with patch.object(reflect.httpx, "AsyncClient", return_value=fake_client):
        err, body = await reflect._smoke_get("https://example.com/x")
    assert err is not None and "ConnectError" in err
    assert body is None


# ---------- code template --------------------------------------------------

def test_template_render_contains_all_load_bearing_fields() -> None:
    """Updated for the repr-based template (Phase C hardening).

    Non-identifier fields (URL, description, digest, source label) are
    inserted via ``repr()`` so a payload with a quote or newline cannot
    break out of a string literal. See tests/test_reflect_injection.py
    for the full injection-audit suite.
    """
    class_name = reflect._snake_to_class_name(VALID_SPEC["tool_name"])
    rendered = reflect.TOOL_TEMPLATE.format(
        tool_name=VALID_SPEC["tool_name"],
        class_name=class_name,
        tool_name_repr=repr(VALID_SPEC["tool_name"]),
        base_url_repr=repr(VALID_SPEC["api_base_url"]),
        endpoint_path_repr=repr(VALID_SPEC["endpoint_path"]),
        digest_fields_repr=repr(list(VALID_SPEC["digest_fields"])),
        description_repr=repr(VALID_SPEC["description"]),
        source_label_repr=repr("api.coingecko.com"),
    )
    assert f"class {class_name}(BaseTool):" in rendered
    assert 'is_data_source = True' in rendered
    assert VALID_SPEC["api_base_url"] in rendered
    assert VALID_SPEC["endpoint_path"] in rendered
    for field in VALID_SPEC["digest_fields"]:
        assert field in rendered


def test_snake_to_class_name() -> None:
    assert reflect._snake_to_class_name("get_fear_greed_index") == "GetFearGreedIndexTool"
    assert reflect._snake_to_class_name("get_x") == "GetXTool"


# ---------- run_reflection: gate matrix ------------------------------------

def _fake_pm() -> MagicMock:
    return MagicMock()


def _fake_llm(response_text: str) -> MagicMock:
    llm = MagicMock()
    llm.chat = AsyncMock(
        return_value=SimpleNamespace(
            message=SimpleNamespace(content=response_text)
        )
    )
    return llm


@pytest.mark.asyncio
async def test_reflect_refuses_when_flag_false() -> None:
    result = await reflect.run_reflection(
        _fake_config(False), _fake_pm(), _fake_llm("NONE")
    )
    assert result["outcome"] == "refused_flag"
    assert result["proposal_id"] is None


@pytest.mark.asyncio
async def test_reflect_refuses_at_cap() -> None:
    pm = _fake_pm()
    store_mock = MagicMock()
    store_mock.count_by_status_and_author = AsyncMock(return_value=3)
    with patch("self_modify.reflect.P.ProposalStore", return_value=store_mock):
        result = await reflect.run_reflection(
            _fake_config(True), pm, _fake_llm("NONE")
        )
    assert result["outcome"] == "refused_cap"


@pytest.mark.asyncio
async def test_reflect_none_response_stops_cleanly() -> None:
    pm = _fake_pm()
    store_mock = MagicMock()
    store_mock.count_by_status_and_author = AsyncMock(return_value=0)
    with patch("self_modify.reflect.P.ProposalStore", return_value=store_mock), \
         patch("self_modify.reflect._build_context",
               AsyncMock(return_value={
                   "tools_block": "t", "objectives_block": "o", "theses_block": "s"
               })):
        result = await reflect.run_reflection(
            _fake_config(True), pm, _fake_llm("NONE")
        )
    assert result["outcome"] == "none"


@pytest.mark.asyncio
async def test_reflect_unparseable_flags_calibration_data() -> None:
    pm = _fake_pm()
    store_mock = MagicMock()
    store_mock.count_by_status_and_author = AsyncMock(return_value=0)
    with patch("self_modify.reflect.P.ProposalStore", return_value=store_mock), \
         patch("self_modify.reflect._build_context",
               AsyncMock(return_value={"tools_block": "", "objectives_block": "", "theses_block": ""})):
        result = await reflect.run_reflection(
            _fake_config(True), pm, _fake_llm("here is my thoughtful reasoning without JSON")
        )
    assert result["outcome"] == "unparseable"


@pytest.mark.asyncio
async def test_reflect_rejects_http_scheme() -> None:
    bad = dict(VALID_SPEC, api_base_url="http://api.example.com")
    pm = _fake_pm()
    store_mock = MagicMock()
    store_mock.count_by_status_and_author = AsyncMock(return_value=0)
    import json as _json
    with patch("self_modify.reflect.P.ProposalStore", return_value=store_mock), \
         patch("self_modify.reflect._build_context",
               AsyncMock(return_value={"tools_block": "", "objectives_block": "", "theses_block": ""})):
        result = await reflect.run_reflection(
            _fake_config(True), pm, _fake_llm(_json.dumps(bad))
        )
    assert result["outcome"] == "rejected_url"
    assert "https" in result["reason"]


@pytest.mark.asyncio
async def test_reflect_rejects_dead_endpoint_at_smoke() -> None:
    import json as _json

    pm = _fake_pm()
    store_mock = MagicMock()
    store_mock.count_by_status_and_author = AsyncMock(return_value=0)

    # Public DNS resolution succeeds; the smoke GET returns 404.
    fake_client = AsyncMock()
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)
    fake_client.get = AsyncMock(return_value=SimpleNamespace(status_code=404))

    with patch("self_modify.reflect.P.ProposalStore", return_value=store_mock), \
         patch("self_modify.reflect._build_context",
               AsyncMock(return_value={"tools_block": "", "objectives_block": "", "theses_block": ""})), \
         patch.object(reflect, "socket", MagicMock(
             getaddrinfo=MagicMock(return_value=[(None, None, None, None, ("104.16.0.1", 0))])
         )), \
         patch.object(reflect.httpx, "AsyncClient", return_value=fake_client):
        result = await reflect.run_reflection(
            _fake_config(True), pm, _fake_llm(_json.dumps(VALID_SPEC))
        )
    assert result["outcome"] == "rejected_smoke"


@pytest.mark.asyncio
async def test_reflect_rejects_path_traversal_at_zone_check() -> None:
    """A tool_name that would target outside tools/data_feeds/ is malformed
    at the snake_case regex; a valid-looking name that classify_proposal
    still rejects would fail at zone. Both paths must never submit.
    """
    import json as _json

    bad = dict(VALID_SPEC, tool_name="../evil")
    pm = _fake_pm()
    store_mock = MagicMock()
    store_mock.count_by_status_and_author = AsyncMock(return_value=0)
    with patch("self_modify.reflect.P.ProposalStore", return_value=store_mock), \
         patch("self_modify.reflect._build_context",
               AsyncMock(return_value={"tools_block": "", "objectives_block": "", "theses_block": ""})):
        result = await reflect.run_reflection(
            _fake_config(True), pm, _fake_llm(_json.dumps(bad))
        )
    # Rejected at spec well-formedness — the snake_case regex catches it.
    assert result["outcome"] == "malformed"


@pytest.mark.asyncio
async def test_reflect_submits_valid_spec_with_proposed_by_morgoth() -> None:
    import json as _json

    pm = _fake_pm()
    store_mock = MagicMock()
    store_mock.count_by_status_and_author = AsyncMock(return_value=0)
    store_mock.submit = AsyncMock(return_value="prop-1")
    store_mock.get = AsyncMock(return_value={"proposal_id": "prop-1"})

    # Response body carries each digest field as a scalar so the shape
    # gate passes — this is the "clean end-to-end" happy path.
    shape_ok_body = {
        "market_cap_percentage": {},  # NB: passed to shape check via json()
        "active_cryptocurrencies": 12000,
        "markets": 800,
    }
    # market_cap_percentage is a dict — that would fail the scalarity
    # rule; swap it for a scalar so the shape check accepts.
    shape_ok_body["market_cap_percentage"] = 42.5
    fake_client = AsyncMock()
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)
    fake_client.get = AsyncMock(return_value=SimpleNamespace(
        status_code=200,
        json=MagicMock(return_value=shape_ok_body),
    ))

    with patch("self_modify.reflect.P.ProposalStore", return_value=store_mock), \
         patch("self_modify.reflect._build_context",
               AsyncMock(return_value={"tools_block": "", "objectives_block": "", "theses_block": ""})), \
         patch.object(reflect, "socket", MagicMock(
             getaddrinfo=MagicMock(return_value=[(None, None, None, None, ("104.16.0.1", 0))])
         )), \
         patch.object(reflect.httpx, "AsyncClient", return_value=fake_client), \
         patch("self_modify.reflect.gates.run_pipeline",
               AsyncMock(return_value="pending_approval")):
        result = await reflect.run_reflection(
            _fake_config(True), pm, _fake_llm(_json.dumps(VALID_SPEC))
        )

    assert result["outcome"] == "submitted"
    assert result["proposal_id"] == "prop-1"
    assert result["pipeline_status"] == "pending_approval"
    # proposed_by='morgoth' was passed through.
    assert store_mock.submit.await_args.kwargs["proposed_by"] == "morgoth"
    # And target_path is under tools/data_feeds/.
    tp = store_mock.submit.await_args.kwargs["target_path"]
    assert tp.startswith("tools/data_feeds/")
    assert tp.endswith(f"{VALID_SPEC['tool_name']}.py")
