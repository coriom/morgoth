"""Shape-gate tests for reflect.

Two motivating rejects — cec526ec (fee_histogram array-valued) and
ccb623d1 (pools-list, all fields absent at flat extraction site) —
both passed 2xx and reached pending_approval before the shape gate
existed. This suite pins the extraction-site contract, the array
rule, the relaxed digest regex, and the rejected_shape outcome
propagation.
"""

from __future__ import annotations

import json as _json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from self_modify import reflect
from self_modify import proposals as P


# ---------- extraction-site + shape_check unit tests ---------------------

def test_shape_ok_top_level_dict_scalars() -> None:
    body = {"a": 1, "b": "hello", "c": 3.14, "d": True, "e": None}
    assert reflect._shape_check(body, ["a", "b", "c"]) is None


def test_shape_ok_data_zero_fallback() -> None:
    body = {"data": [{"a": 1, "b": 2}, {"a": 9, "b": 8}]}
    assert reflect._shape_check(body, ["a", "b"]) is None


def test_shape_ok_top_level_list() -> None:
    body = [{"a": 1, "b": 2}, {"a": 3}]
    assert reflect._shape_check(body, ["a", "b"]) is None


def test_shape_missing_field_names_it_and_shows_real_keys() -> None:
    body = {"pools": [{"poolId": 1}]}
    err = reflect._shape_check(body, ["pool_name", "blocks_mined"])
    assert err is not None
    assert "pool_name" in err
    # The response actually has `pools` — the operator sees this.
    assert "pools" in err


def test_shape_array_valued_field_rejected_at_top_level() -> None:
    """The cec526ec case exactly: fee_histogram present but list-of-lists."""
    body = {
        "count": 100,
        "vsize": 200,
        "total_fee": 300,
        "fee_histogram": [[1.0, 100], [0.5, 50]],
    }
    err = reflect._shape_check(
        body, ["count", "vsize", "total_fee", "fee_histogram"]
    )
    assert err is not None
    assert "fee_histogram" in err
    assert "array/dict-valued" in err


def test_shape_dict_valued_field_also_rejected() -> None:
    body = {"a": 1, "b": {"nested": "yes"}, "c": 3}
    err = reflect._shape_check(body, ["a", "b", "c"])
    assert err is not None
    assert "b" in err


def test_shape_response_not_dict_or_list_rejected() -> None:
    for body in [42, "hello", 3.14, True]:
        err = reflect._shape_check(body, ["a", "b", "c"])
        assert err is not None
        assert "neither dict nor list" in err


def test_shape_empty_list_rejected() -> None:
    assert "empty list" in (reflect._shape_check([], ["a"]) or "")


def test_shape_list_of_non_dict_rejected() -> None:
    err = reflect._shape_check([1, 2, 3], ["a"])
    assert err is not None and "not a dict" in err


def test_shape_dict_with_no_matches_and_no_data_fallback() -> None:
    """The ccb623d1 case exactly: response is a dict, no field matches
    top-level, and there is no ``data`` key — reject with keys shown."""
    body = {"pools": [{"poolId": 1, "name": "Foundry"}]}
    err = reflect._shape_check(
        body, ["pool_name", "blocks_mined", "hash_rate_share",
               "empty_blocks", "avg_match_rate"],
    )
    assert err is not None
    assert "no digest field matches" in err or "no `data`" in err
    assert "pools" in err


def test_shape_keys_truncated_when_response_has_many_keys() -> None:
    body = {f"k{i}": i for i in range(30)}
    err = reflect._shape_check(body, ["missing_a", "missing_b", "missing_c"])
    assert err is not None
    assert "…(+15 more)" in err


def test_shape_non_json_body_rejected_by_smoke_layer() -> None:
    """The parsing check lives in _smoke_get itself — verify via that."""
    # Covered end-to-end below in _test_reflect_rejected_shape_non_json.


# ---------- DIGEST_FIELD_RE — relaxed but still injection-neutral ---------

def test_digest_field_re_accepts_camelcase() -> None:
    for name in ["poolId", "blockCount", "avgMatchRate", "emptyBlocks"]:
        assert reflect.DIGEST_FIELD_RE.match(name), (
            f"{name!r} should be accepted post-relaxation"
        )


def test_digest_field_re_accepts_snake_case() -> None:
    for name in ["a", "abc_def", "long_name_ok"]:
        assert reflect.DIGEST_FIELD_RE.match(name)


def test_digest_field_re_still_rejects_metachars_and_whitespace() -> None:
    """Injection-neutrality: the relaxed regex still admits only
    ``[a-zA-Z0-9_]``. Anything the template could not repr() cleanly
    as an identifier-shaped list element stays out."""
    for bad in [
        "has space",       # whitespace
        "has-dash",        # dash
        "has.dot",         # dot
        "has,comma",       # comma
        'has"quote',       # quote — the actual injection vector
        "has'apostrophe",
        "has;semicolon",
        "has\\backslash",
        "has\nnewline",
        "café",            # unicode
        "12starts_num",    # can't start with digit
        "_underscore",     # can't start with underscore
        "",                # empty
        "a" * 41,          # too long (max 40)
    ]:
        assert not reflect.DIGEST_FIELD_RE.match(bad), (
            f"{bad!r} was admitted — regex is too loose"
        )


def test_spec_validation_accepts_camelcase_digest_fields() -> None:
    """End-to-end at the spec-validation layer."""
    spec = {
        "tool_name": "get_x_y_z",
        "api_base_url": "https://example.com",
        "endpoint_path": "/a",
        "digest_fields": ["poolId", "blockCount", "avgMatchRate"],
        "description": "long enough description",
        "rationale": "why",
    }
    assert reflect._spec_is_well_formed(spec) is None


def test_spec_validation_still_rejects_bad_digest_field() -> None:
    spec = {
        "tool_name": "get_x_y_z",
        "api_base_url": "https://example.com",
        "endpoint_path": "/a",
        "digest_fields": ['bad"quote', "b_ok", "c_ok"],
        "description": "long enough description",
        "rationale": "why",
    }
    assert reflect._spec_is_well_formed(spec) is not None


# ---------- proposals.py exposes rejected_shape --------------------------

def test_rejected_shape_is_a_known_status() -> None:
    assert P.STATUS_REJECTED_SHAPE == "rejected_shape"
    assert P.STATUS_REJECTED_SHAPE in P.ALL_STATUSES


# ---------- _smoke_get returns (err, body) — non-JSON path ---------------

@pytest.mark.asyncio
async def test_smoke_get_non_json_body_is_rejected() -> None:
    fake_resp = SimpleNamespace(
        status_code=200,
        json=MagicMock(side_effect=ValueError("Expecting value")),
    )
    fake_client = AsyncMock()
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)
    fake_client.get = AsyncMock(return_value=fake_resp)
    with patch.object(reflect.httpx, "AsyncClient", return_value=fake_client):
        err, body = await reflect._smoke_get("https://example.com/x")
    assert err == "response is not JSON"
    assert body is None


@pytest.mark.asyncio
async def test_smoke_get_json_body_returned_on_2xx() -> None:
    fake_resp = SimpleNamespace(
        status_code=200,
        json=MagicMock(return_value={"a": 1, "b": 2}),
    )
    fake_client = AsyncMock()
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)
    fake_client.get = AsyncMock(return_value=fake_resp)
    with patch.object(reflect.httpx, "AsyncClient", return_value=fake_client):
        err, body = await reflect._smoke_get("https://example.com/x")
    assert err is None
    assert body == {"a": 1, "b": 2}


# ---------- end-to-end: outcome=rejected_shape propagates -----------------

VALID_SPEC = {
    # tool_name tokens must appear in description — the coherence gate
    # runs before shape. "test" and "digest" are present in the desc.
    "tool_name": "get_test_digest",
    "api_base_url": "https://api.example.com",
    "endpoint_path": "/v1/thing",
    "digest_fields": ["a", "b", "c"],
    "description": "Fetch a compact tri-field digest for testing.",
    "rationale": "coverage gap",
}


def _fake_config() -> SimpleNamespace:
    return SimpleNamespace(
        permissions=SimpleNamespace(
            permissions=SimpleNamespace(can_self_modify=True),
        ),
    )


def _fake_llm(response_text: str) -> MagicMock:
    llm = MagicMock()
    llm.chat = AsyncMock(
        return_value=SimpleNamespace(
            message=SimpleNamespace(content=response_text)
        )
    )
    return llm


async def _run_reflect_with_body(body: Any) -> dict[str, Any]:
    """Drive run_reflection to the smoke/shape stage with a mocked body."""
    pm = MagicMock()
    store_mock = MagicMock()
    store_mock.count_by_status_and_author = AsyncMock(return_value=0)
    store_mock.submit = AsyncMock(return_value="prop-1")
    store_mock.get = AsyncMock(return_value={"proposal_id": "prop-1"})

    fake_resp = SimpleNamespace(
        status_code=200,
        json=MagicMock(return_value=body),
    )
    fake_client = AsyncMock()
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)
    fake_client.get = AsyncMock(return_value=fake_resp)

    with patch("self_modify.reflect.P.ProposalStore", return_value=store_mock), \
         patch("self_modify.reflect._build_context",
               AsyncMock(return_value={
                   "tools_block": "",
                   "objectives_block": "",
                   "theses_block": "",
               })), \
         patch("self_modify.reflect.tool_name_collides", return_value=False), \
         patch.object(reflect, "socket", MagicMock(
             getaddrinfo=MagicMock(return_value=[
                 (None, None, None, None, ("104.16.0.1", 0))
             ])
         )), \
         patch.object(reflect.httpx, "AsyncClient", return_value=fake_client), \
         patch("self_modify.reflect.gates.run_pipeline",
               AsyncMock(return_value="pending_approval")), \
         patch("self_modify.shadow.run_shadow_verdict",
               AsyncMock(return_value={
                   "verdict": "APPROVE", "axes": {}, "reasons": [],
                   "engine": "test", "prompt_version": "test",
               })), \
         patch("self_modify.reflect.liveness.run_liveness_probe",
               AsyncMock(return_value={
                   "url": "x", "hits": [], "n_hits": 0,
                   "digest_fields": [],
               })):
        return await reflect.run_reflection(
            _fake_config(), pm, _fake_llm(_json.dumps(VALID_SPEC))
        )


@pytest.mark.asyncio
async def test_end_to_end_shape_pass_reaches_submit() -> None:
    result = await _run_reflect_with_body({"a": 1, "b": 2, "c": 3})
    assert result["outcome"] == "submitted"


@pytest.mark.asyncio
async def test_end_to_end_shape_reject_missing_field() -> None:
    """Only `a` is present — `b` and `c` missing."""
    result = await _run_reflect_with_body({"a": 1, "other": "junk"})
    assert result["outcome"] == "rejected_shape"
    assert "missing" in result["reason"]
    # And the real keys are surfaced.
    assert "other" in result["reason"] or "a" in result["reason"]


@pytest.mark.asyncio
async def test_end_to_end_shape_reject_array_valued() -> None:
    """All three fields are present but `c` is a list — reject."""
    result = await _run_reflect_with_body({"a": 1, "b": 2, "c": [1, 2, 3]})
    assert result["outcome"] == "rejected_shape"
    assert "array/dict-valued" in result["reason"]
    assert "c" in result["reason"]


@pytest.mark.asyncio
async def test_end_to_end_shape_reject_pools_list_regression() -> None:
    """Regression for ccb623d1: {"pools": [...]} with no top-level match
    and no `data` fallback — reject and surface the real key."""
    body = {
        "pools": [
            {"poolId": 1, "name": "Foundry USA", "blockCount": 41},
        ],
    }
    result = await _run_reflect_with_body(body)
    assert result["outcome"] == "rejected_shape"
    assert "pools" in result["reason"]
