"""Negative-list + retry-with-feedback tests for reflect.

Every DB touch is mocked. Retry logic drives ``run_reflection`` with a
fake ``reflect_chat`` that returns different specs on each call so the
retry path is fully exercised without any real LLM billing.
"""

from __future__ import annotations

import json as _json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from self_modify import proposals as P
from self_modify import reflect


# ---------- _endpoint_from_row + _rejections_block render ----------------

def test_endpoint_from_row_extracts_from_json_content() -> None:
    row = {
        "target_path": "tools/data_feeds/get_x.py",
        "content": _json.dumps({
            "tool_name": "get_x",
            "endpoint_path": "/v1/thing",
            "api_base_url": "https://a.example.com",
        }),
    }
    assert reflect._endpoint_from_row(row) == "/v1/thing"


def test_endpoint_from_row_extracts_from_python_content() -> None:
    """Gate-3 rejects store assembled Python — recover ENDPOINT literal."""
    row = {
        "target_path": "tools/data_feeds/get_x.py",
        "content": (
            "from typing import Any\n"
            "_BASE_URL = 'https://a.example.com'\n"
            "_ENDPOINT_PATH = '/api/v1/other'\n"
        ),
    }
    assert reflect._endpoint_from_row(row) == "/api/v1/other"


def test_endpoint_from_row_returns_none_when_unavailable() -> None:
    assert reflect._endpoint_from_row({"target_path": "x", "content": ""}) is None
    assert reflect._endpoint_from_row({"target_path": "x", "content": None}) is None
    assert reflect._endpoint_from_row({"target_path": "x", "content": "garbage"}) is None


def test_rejections_block_renders_name_endpoint_and_reason() -> None:
    rows = [
        {
            "target_path": "tools/data_feeds/get_pools.py",
            "content": _json.dumps({
                "tool_name": "get_pools", "endpoint_path": "/api/v1/mining/pools/24h",
            }),
            "status": P.STATUS_REJECTED_SHAPE,
            "status_reason": "shape check: missing at top-level [x, y, z]",
        },
        {
            "target_path": "tools/data_feeds/get_bad.py",
            "content": "",
            "status": P.STATUS_MALFORMED,
            "status_reason": "digest_fields must be a list of 3-6 items",
        },
    ]
    block = reflect._rejections_block(rows)
    assert "get_pools" in block
    assert "/api/v1/mining/pools/24h" in block
    assert "shape check" in block
    assert "get_bad" in block
    assert "digest_fields" in block


def test_rejections_block_truncates_long_reason() -> None:
    row = {
        "target_path": "tools/data_feeds/get_x.py",
        "content": "",
        "status": P.STATUS_REJECTED_SHAPE,
        "status_reason": "x" * 300,
    }
    block = reflect._rejections_block([row])
    # Long reason gets truncated with an ellipsis.
    assert "…" in block
    assert len(block) < 300


def test_rejections_block_empty_when_no_rows() -> None:
    assert reflect._rejections_block([]) == ""


# ---------- _reflection_prompt: byte-identical when empty ---------------

def test_prompt_byte_identical_when_no_rejections() -> None:
    """Non-regression: fresh install (no history) sees the pre-feature prompt."""
    ctx = {
        "tools_block": "- t1 (kind) — objectives_using=0: desc",
        "objectives_block": "- obj1",
        "theses_block": "- s1",
        "rejections_block": "",
    }
    prompt_with = reflect._reflection_prompt(ctx)
    ctx_no_key = dict(ctx)
    del ctx_no_key["rejections_block"]
    prompt_without_key = reflect._reflection_prompt(ctx_no_key)
    assert prompt_with == prompt_without_key
    assert "ALREADY REJECTED" not in prompt_with


def test_prompt_includes_negative_list_when_present() -> None:
    ctx = {
        "tools_block": "- t1 (kind) — objectives_using=0: desc",
        "objectives_block": "- obj1",
        "theses_block": "- s1",
        "rejections_block": "- get_pools (/api/v1/mining/pools/24h): missing",
    }
    prompt = reflect._reflection_prompt(ctx)
    assert "ALREADY REJECTED" in prompt
    assert "get_pools" in prompt
    assert "/api/v1/mining/pools/24h" in prompt


def test_prompt_size_bounded_with_full_negative_list() -> None:
    """8 rows × 100-char reason + naming ≈ <2 KB; total prompt <8 KB."""
    rows = [
        {
            "target_path": f"tools/data_feeds/get_t{i}.py",
            "content": _json.dumps({"tool_name": f"get_t{i}", "endpoint_path": f"/api/v{i}"}),
            "status": P.STATUS_REJECTED_SHAPE,
            "status_reason": "x" * 500,
        }
        for i in range(reflect.NEGATIVE_LIST_LIMIT)
    ]
    block = reflect._rejections_block(rows)
    assert len(block) < 2000, f"negative-list block grew to {len(block)} chars"
    ctx = {
        "tools_block": "- t1 (kind) — objectives_using=0: desc",
        "objectives_block": "- obj1",
        "theses_block": "- s1",
        "rejections_block": block,
    }
    prompt = reflect._reflection_prompt(ctx)
    assert len(prompt) < 8000, f"prompt grew to {len(prompt)} chars"


# ---------- _corrective_prompt shape -------------------------------------

def test_corrective_prompt_contains_original_plus_correction() -> None:
    original = "ORIGINAL_PROMPT_MARKER"
    spec = {"tool_name": "get_x", "endpoint_path": "/x"}
    reason = "shape check missing [a, b]"
    corrective = reflect._corrective_prompt(original, spec, reason)
    assert original in corrective
    assert "YOUR PREVIOUS ATTEMPT WAS REJECTED" in corrective
    assert "get_x" in corrective
    assert "shape check missing" in corrective


def test_corrective_prompt_survives_missing_spec() -> None:
    corrective = reflect._corrective_prompt("orig", None, "some reason")
    assert "no spec" in corrective


# ---------- ProposalStore.submit_terminal guard --------------------------

@pytest.mark.asyncio
async def test_submit_terminal_refuses_non_pre_submit_status() -> None:
    """Only malformed / rejected_smoke / rejected_shape are allowed —
    submit_terminal must not be usable to short-circuit the pipeline."""
    from self_modify.proposals import ProposalStore

    class _Ctx:
        def __init__(self, c: Any) -> None:
            self._c = c
        async def __aenter__(self) -> Any:
            return self._c
        async def __aexit__(self, *a: Any) -> None:
            pass

    conn = AsyncMock()
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_Ctx(conn))
    pm = MagicMock()
    pm._require_pool = MagicMock(return_value=pool)
    store = ProposalStore(pm)

    for bad in (
        P.STATUS_SUBMITTED,
        P.STATUS_PENDING_APPROVAL,
        P.STATUS_APPROVED_PENDING_APPLY,
        P.STATUS_APPLIED,
        P.STATUS_REJECTED,
        P.STATUS_ZONE_REJECTED,
        P.STATUS_TESTS_FAILED,
        "not_a_status",
    ):
        with pytest.raises(ValueError):
            await store.submit_terminal(
                target_path="tools/data_feeds/x.py",
                change_type="new_file",
                content="",
                rationale=None,
                status=bad,
                status_reason="test",
                proposed_by="morgoth",
                engine="ollama",
            )


@pytest.mark.asyncio
async def test_submit_terminal_writes_direct_row_for_pre_submit_terminal() -> None:
    """Never transits `submitted` — the write goes straight to the
    terminal status with the correct columns."""
    from self_modify.proposals import ProposalStore

    class _Ctx:
        def __init__(self, c: Any) -> None:
            self._c = c
        async def __aenter__(self) -> Any:
            return self._c
        async def __aexit__(self, *a: Any) -> None:
            pass

    conn = AsyncMock()
    conn.execute = AsyncMock(return_value="INSERT 0 1")
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_Ctx(conn))
    pm = MagicMock()
    pm._require_pool = MagicMock(return_value=pool)
    store = ProposalStore(pm)

    pid = await store.submit_terminal(
        target_path="tools/data_feeds/get_x.py",
        change_type="new_file",
        content='{"tool_name": "get_x"}',
        rationale="r",
        status=P.STATUS_REJECTED_SHAPE,
        status_reason="reason text",
        proposed_by="morgoth",
        engine="claude-cli",
    )
    assert isinstance(pid, str) and len(pid) == 36
    args = conn.execute.await_args.args
    # (query, pid, target, change_type, content, rationale, status,
    #  status_reason, proposed_by, engine, retry_of)
    assert args[-1] is None  # retry_of
    assert args[-2] == "claude-cli"
    assert args[-3] == "morgoth"
    assert args[-4] == "reason text"
    assert args[-5] == P.STATUS_REJECTED_SHAPE


# ---------- ProposalStore.list_recent_rejections query filter ------------

@pytest.mark.asyncio
async def test_list_recent_rejections_filters_and_orders() -> None:
    from self_modify.proposals import ProposalStore

    class _Ctx:
        def __init__(self, c: Any) -> None:
            self._c = c
        async def __aenter__(self) -> Any:
            return self._c
        async def __aexit__(self, *a: Any) -> None:
            pass

    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[])
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_Ctx(conn))
    pm = MagicMock()
    pm._require_pool = MagicMock(return_value=pool)
    store = ProposalStore(pm)

    await store.list_recent_rejections(proposed_by="morgoth", limit=8)
    args = conn.fetch.await_args.args
    query = args[0]
    assert "proposed_by = $1" in query
    assert "status = ANY($2::text[])" in query
    assert "updated_at DESC" in query
    assert args[1] == "morgoth"
    assert set(args[2]) == set(P.NEGATIVE_LIST_STATUSES)
    assert args[3] == 8


# ---------- run_reflection: retry-with-feedback end-to-end --------------

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


def _mock_store() -> MagicMock:
    store = MagicMock()
    store.count_by_status_and_author = AsyncMock(return_value=0)
    store.list_recent_rejections = AsyncMock(return_value=[])
    store.submit_terminal = AsyncMock(return_value="reject-1")
    store.submit = AsyncMock(return_value="prop-1")
    store.get = AsyncMock(return_value={"proposal_id": "prop-1"})
    return store


def _shape_ok_body() -> dict[str, Any]:
    return {"a": 1, "b": 2, "c": 3}


def _shape_bad_body() -> dict[str, Any]:
    """Body that fails the shape gate — first attempt hits this."""
    return {"other_key_a": 1, "other_key_b": 2}


def _make_http_stack(bodies: list[Any]) -> Any:
    """Return an httpx.AsyncClient patch that yields ``bodies`` in order."""
    responses = [
        SimpleNamespace(status_code=200, json=MagicMock(return_value=b))
        for b in bodies
    ]
    fake = AsyncMock()
    fake.__aenter__ = AsyncMock(return_value=fake)
    fake.__aexit__ = AsyncMock(return_value=None)
    fake.get = AsyncMock(side_effect=responses)
    return fake


async def _drive(
    prompts_to_specs: list[dict[str, Any] | str],
    bodies: list[Any],
    store: MagicMock,
) -> tuple[dict[str, Any], list[str]]:
    """Drive run_reflection with a scripted reflect_chat + http_stack.

    Returns (result_dict, captured_prompts).
    """
    calls: list[str] = []

    async def _reflect_chat_mock(prompt: str, config: Any, provider: str, **_: Any) -> tuple[str, dict[str, Any]]:
        calls.append(prompt)
        spec_or_str = prompts_to_specs[len(calls) - 1]
        if isinstance(spec_or_str, str):
            return spec_or_str, {}
        return _json.dumps(spec_or_str), {}

    pm = MagicMock()
    with patch("self_modify.reflect.P.ProposalStore", return_value=store), \
         patch("self_modify.reflect._build_context",
               AsyncMock(return_value={
                   "tools_block": "",
                   "objectives_block": "",
                   "theses_block": "",
                   "rejections_block": "",
               })), \
         patch("self_modify.reflect.tool_name_collides", return_value=False), \
         patch("self_modify.reflect.reflect_chat", side_effect=_reflect_chat_mock), \
         patch.object(reflect, "socket", MagicMock(
             getaddrinfo=MagicMock(return_value=[
                 (None, None, None, None, ("104.16.0.1", 0))
             ])
         )), \
         patch.object(reflect.httpx, "AsyncClient", return_value=_make_http_stack(bodies)), \
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
        result = await reflect.run_reflection(
            _fake_config(), pm, MagicMock(), provider="claude-cli",
        )
    return result, calls


@pytest.mark.asyncio
async def test_retry_fires_on_rejected_shape_and_succeeds() -> None:
    """First attempt shape-rejects, retry passes → pending_approval."""
    store = _mock_store()
    # First attempt's spec targets fields absent from body_bad; retry
    # keeps the same spec but the second body carries all three.
    result, calls = await _drive(
        [VALID_SPEC, VALID_SPEC],
        [_shape_bad_body(), _shape_ok_body()],
        store,
    )
    assert result["outcome"] == "submitted"
    assert result["retried"] is True
    assert result["first_attempt"]["outcome"] == "rejected_shape"
    # Two LLM calls: original + corrective.
    assert len(calls) == 2
    # Corrective prompt contains the rejection notice.
    assert "YOUR PREVIOUS ATTEMPT WAS REJECTED" in calls[1]
    # First attempt was persisted as rejected_shape.
    store.submit_terminal.assert_awaited_once()
    st_kwargs = store.submit_terminal.await_args.kwargs
    assert st_kwargs["status"] == P.STATUS_REJECTED_SHAPE
    assert st_kwargs["retry_of"] is None
    # Successful retry's submit carries retry_of pointing at first row.
    submit_kwargs = store.submit.await_args.kwargs
    assert submit_kwargs["retry_of"] == "reject-1"


@pytest.mark.asyncio
async def test_retry_second_reject_terminal_no_third_attempt() -> None:
    """First AND retry both shape-reject → no third attempt, retry row
    persisted with retry_of set."""
    store = _mock_store()
    # submit_terminal returns different ids for first vs retry, so we
    # can verify retry_of on the retry write.
    store.submit_terminal = AsyncMock(side_effect=["reject-first", "reject-retry"])
    result, calls = await _drive(
        [VALID_SPEC, VALID_SPEC],
        [_shape_bad_body(), _shape_bad_body()],
        store,
    )
    assert result["outcome"] == "rejected_shape"
    assert result["retried"] is True
    assert len(calls) == 2
    # Second submit_terminal linked to first.
    calls_kwargs = [c.kwargs for c in store.submit_terminal.await_args_list]
    assert len(calls_kwargs) == 2
    assert calls_kwargs[0]["retry_of"] is None
    assert calls_kwargs[1]["retry_of"] == "reject-first"
    # No submit() call happened (never reached submission).
    store.submit.assert_not_awaited()


@pytest.mark.asyncio
async def test_retry_fires_on_malformed_first_attempt() -> None:
    """malformed spec → retry with the reason."""
    store = _mock_store()
    bad_spec = dict(VALID_SPEC, digest_fields=["only_one"])  # too few
    result, calls = await _drive(
        [bad_spec, VALID_SPEC],
        [_shape_ok_body()],  # only the retry actually reaches HTTP
        store,
    )
    assert result["outcome"] == "submitted"
    assert result["retried"] is True
    assert result["first_attempt"]["outcome"] == "malformed"
    # Correction prompt includes the malformed reason.
    assert "digest_fields" in calls[1]


@pytest.mark.asyncio
async def test_retry_fires_on_rejected_smoke() -> None:
    """First attempt's smoke gate fails → retry."""
    store = _mock_store()
    # Non-JSON body triggers rejected_smoke.
    non_json_resp = SimpleNamespace(
        status_code=200,
        json=MagicMock(side_effect=ValueError("not json")),
    )
    ok_resp = SimpleNamespace(
        status_code=200,
        json=MagicMock(return_value=_shape_ok_body()),
    )
    fake = AsyncMock()
    fake.__aenter__ = AsyncMock(return_value=fake)
    fake.__aexit__ = AsyncMock(return_value=None)
    fake.get = AsyncMock(side_effect=[non_json_resp, ok_resp])
    calls: list[str] = []

    async def _reflect_chat_mock(prompt: str, *_a: Any, **_kw: Any) -> tuple[str, dict[str, Any]]:
        calls.append(prompt)
        return _json.dumps(VALID_SPEC), {}

    pm = MagicMock()
    with patch("self_modify.reflect.P.ProposalStore", return_value=store), \
         patch("self_modify.reflect._build_context",
               AsyncMock(return_value={
                   "tools_block": "", "objectives_block": "",
                   "theses_block": "", "rejections_block": "",
               })), \
         patch("self_modify.reflect.tool_name_collides", return_value=False), \
         patch("self_modify.reflect.reflect_chat", side_effect=_reflect_chat_mock), \
         patch.object(reflect, "socket", MagicMock(
             getaddrinfo=MagicMock(return_value=[
                 (None, None, None, None, ("104.16.0.1", 0))
             ])
         )), \
         patch.object(reflect.httpx, "AsyncClient", return_value=fake), \
         patch("self_modify.reflect.gates.run_pipeline",
               AsyncMock(return_value="pending_approval")):
        result = await reflect.run_reflection(
            _fake_config(), pm, MagicMock(), provider="ollama",
        )
    assert result["outcome"] == "submitted"
    assert result["retried"] is True
    assert result["first_attempt"]["outcome"] == "rejected_smoke"


@pytest.mark.asyncio
async def test_no_retry_when_first_attempt_unparseable() -> None:
    """unparseable is NOT retry-eligible (Phase C1 scope)."""
    store = _mock_store()
    result, calls = await _drive(
        ["this is not json"],
        [],
        store,
    )
    assert result["outcome"] == "unparseable"
    assert result["retried"] is False
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_no_retry_when_first_attempt_says_none() -> None:
    store = _mock_store()
    result, calls = await _drive(
        ["NONE"],
        [],
        store,
    )
    assert result["outcome"] == "none"
    assert result["retried"] is False
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_no_retry_when_first_attempt_submits_successfully() -> None:
    """Happy path — no retry needed."""
    store = _mock_store()
    result, calls = await _drive(
        [VALID_SPEC],
        [_shape_ok_body()],
        store,
    )
    assert result["outcome"] == "submitted"
    assert result["retried"] is False
    assert len(calls) == 1
    # submit() called with retry_of=None (first-shot success).
    assert store.submit.await_args.kwargs["retry_of"] is None


# ---------- integration: rejections load into next run's negative list --

@pytest.mark.asyncio
async def test_build_context_includes_rejections_block() -> None:
    """_build_context queries list_recent_rejections and renders it."""
    from types import SimpleNamespace as SN

    pm = MagicMock()
    pm.get_objectives = AsyncMock(return_value=[])
    pm.get_theses = AsyncMock(return_value=[])
    store = MagicMock()
    store.list_recent_rejections = AsyncMock(return_value=[
        {
            "target_path": "tools/data_feeds/get_pools.py",
            "content": _json.dumps({
                "tool_name": "get_pools",
                "endpoint_path": "/api/v1/mining/pools/24h",
            }),
            "status": P.STATUS_REJECTED_SHAPE,
            "status_reason": "shape check: missing at top-level [x, y, z]",
        },
    ])
    with patch(
        "scripts.compile_wiki._registered_tools_offline", return_value=[],
    ), patch(
        "scripts.compile_wiki._load_tool_usage",
        AsyncMock(return_value=({}, {})),
    ), patch("self_modify.reflect.P.ProposalStore", return_value=store):
        ctx = await reflect._build_context(pm, SN())
    assert "get_pools" in ctx["rejections_block"]
    assert "/api/v1/mining/pools/24h" in ctx["rejections_block"]
    # The block is queried once.
    store.list_recent_rejections.assert_awaited_once()
