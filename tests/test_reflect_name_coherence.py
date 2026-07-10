"""Name/content coherence gate tests.

The 4/4 observed lie class (exchange_flows on mining pools,
exchange_netflow on network economics) is caught here with a pure
substring check — no semantic lexicon. This file pins:

- The all-18-real-tools calibration (no false positives).
- The 4 historical specs (2 lies must reject, 2 honest names pass).
- Normalization edge cases (hyphen, ampersand, case, stem).
- Retry wiring: rejected_name triggers a corrective attempt with the
  missing tokens in the prompt (structured-feedback hypothesis test).
- submit_terminal accepts rejected_name; the status is a first-class
  member of the pre-submit terminal set and the negative-list set.
"""

from __future__ import annotations

import asyncio
import json as _json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from self_modify import proposals as P
from self_modify import reflect


# ---------- normalization edge cases -------------------------------------

def test_normalize_lowercases_and_strips_non_alphanumerics() -> None:
    assert reflect._normalize_for_coherence("On-Chain") == "onchain"
    assert reflect._normalize_for_coherence("Fear & Greed Index") == "feargreedindex"
    assert reflect._normalize_for_coherence("HELLO/world/2") == "helloworld2"
    assert reflect._normalize_for_coherence("") == ""
    assert reflect._normalize_for_coherence(None) == ""  # type: ignore[arg-type]


def test_token_appears_direct_substring() -> None:
    assert reflect._token_appears("onchain", "fetchrealbitcoinonchainmetrics")
    assert reflect._token_appears("news", "fetchnewsitemsfromrssfeeds")


def test_token_appears_stem_minus_one_trailing_char() -> None:
    """`history` matches `historical`; `notify` matches `notification`."""
    assert reflect._token_appears("history", "fetchhistoricalusdprices")
    assert reflect._token_appears("notify", "sendanotificationthroughchannels")


def test_token_appears_stem_minus_two_trailing_chars() -> None:
    """Longer tokens can drop two chars to match a stem — but only when
    len(token) >= 6, so short tokens can't false-positive.

    ``operator``[:-2] = ``operat``, which appears in ``operations``.
    Direct and -1 stems both miss (``operato`` is not in
    ``operations``), so this specifically exercises the -2 branch.
    """
    text = "userperformsoperationsdaily"
    assert "operator" not in text  # direct miss
    assert "operato" not in text   # -1 miss
    assert "operat" in text        # -2 hit
    assert reflect._token_appears("operator", text)


def test_token_stem_len_guard_blocks_false_positives() -> None:
    """Stems are only tried when the token is long enough — a 3-char
    token like ``fee`` cannot match via a 1-char stem."""
    # 4-char token: -1 stem would need len>=5, so no stem tried.
    assert not reflect._token_appears("news", "fetchnew")  # nothing to match
    # 5-char token: -1 stem allowed, but no -2.
    assert reflect._token_appears("flows", "fetchflowmetrics")  # -1: "flow" in text
    assert not reflect._token_appears("flows", "fetchflmetrics")  # would need -2, len 5<6


def test_token_short_no_stem_only_direct() -> None:
    """Tokens with len <5 use direct substring only (no stem)."""
    assert reflect._token_appears("fee", "fetchfeeschedule")  # direct
    # 4-char token, no stem applies (needs >=5 for -1). Direct only.
    assert reflect._token_appears("news", "newsfeed")
    assert not reflect._token_appears("news", "unrelatedtext")


# ---------- calibration: all 18 real tools pass ---------------------------

@pytest.mark.asyncio
async def test_all_registered_tools_pass_coherence_gate() -> None:
    """Non-regression: EVERY currently-registered tool's (name,
    description) pair passes _name_coherence_check. If a future tool
    landing has a name that doesn't reflect its description, this
    fires — and the operator gets a chance to rename OR to expand
    _NAME_STOPWORDS with a documented reason."""
    from core.config import load_config
    from memory.persistent import PersistentMemory
    from scripts.compile_wiki import _registered_tools_offline

    config = await load_config()
    pm = PersistentMemory(config)
    tools = _registered_tools_offline(config, pm)
    failures: list[tuple[str, list[str]]] = []
    for t in tools:
        d = (getattr(t, "description", "") or "").strip()
        missing = reflect._name_coherence_check(t.name, d, "")
        if missing:
            failures.append((t.name, missing))
    assert not failures, f"coherence gate false-positives: {failures}"


# ---------- historical matrix: 4 real specs -------------------------------

def test_historical_exchange_flows_mining_pools_rejected() -> None:
    """ccb623d1 (and its retry sibling): name says exchange_flows,
    endpoint returns mining-pool distribution."""
    missing = reflect._name_coherence_check(
        "get_btc_exchange_flows",
        "Fetches 24h Bitcoin mining pool distribution showing which "
        "pools mined recent blocks, their hash rate share, and empty "
        "block counts, revealing mining centralization and network health.",
        "/api/v1/mining/pools/24h",
    )
    assert set(missing) == {"exchange", "flows"}


def test_historical_exchange_netflow_network_economics_rejected() -> None:
    """738b75de: name says exchange_netflow, endpoint returns
    aggregate network economics (trade volume, mined BTC)."""
    missing = reflect._name_coherence_check(
        "get_bitcoin_exchange_netflow",
        "Fetch aggregate Bitcoin network economics from blockchain.info "
        "stats: trade volume (BTC/USD), estimated transaction volume in "
        "USD, newly mined BTC, and total fees paid.",
        "/stats",
    )
    assert set(missing) == {"exchange", "netflow"}


def test_historical_mempool_stats_honest_name_passes() -> None:
    """cec526ec: name was honest — mempool endpoint, mempool tokens.
    Rejected at gate 3 for partial duplicate, NOT at name coherence."""
    missing = reflect._name_coherence_check(
        "get_bitcoin_mempool_stats",
        "Fetches Bitcoin mempool depth and congestion metrics: "
        "transaction count, total virtual size, total pending fees, "
        "and fee-rate histogram.",
        "/api/mempool",
    )
    assert missing == []


def test_hypothetical_tweets_honest_name_passes() -> None:
    """A social/sentiment tool that would fill a real gap (Reddit was
    retired) — name and content align, gate passes."""
    missing = reflect._name_coherence_check(
        "get_cryptocurrency_tweets",
        "Fetches recent tweets about cryptocurrencies from Twitter for "
        "market-sentiment analysis.",
        "/api/v2/tweets/search/recent",
    )
    assert missing == []


# ---------- stopword semantics --------------------------------------------

def test_stopwords_include_asset_framing_and_crud() -> None:
    """Verify the stopword set has the framing tokens (test-locks the
    documented rationale — asset names are framing, not content)."""
    for w in ("get", "fetch", "btc", "bitcoin", "eth", "ethereum",
              "crypto", "cryptocurrency", "data", "info", "stats", "index"):
        assert w in reflect._NAME_STOPWORDS


def test_asset_only_name_yields_zero_tokens_passes() -> None:
    """`get_btc` (framing only) has no content tokens — auto-passes.
    This is the intended behavior: the gate catches content-family
    lies, not thin naming."""
    assert reflect._name_coherence_check("get_btc", "any text", "") == []
    assert reflect._name_coherence_check("get_crypto", "any", "") == []


# ---------- rejected_name is a first-class terminal status ---------------

def test_rejected_name_is_registered_status() -> None:
    assert P.STATUS_REJECTED_NAME == "rejected_name"
    assert P.STATUS_REJECTED_NAME in P.ALL_STATUSES
    assert P.STATUS_REJECTED_NAME in P.NEGATIVE_LIST_STATUSES
    assert P.STATUS_REJECTED_NAME in P._PRE_SUBMIT_TERMINAL_STATUSES


@pytest.mark.asyncio
async def test_submit_terminal_accepts_rejected_name() -> None:
    """The guard set was extended — verify the write path works."""
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
        target_path="tools/data_feeds/get_x_liar.py",
        change_type="new_file",
        content='{"tool_name": "get_x_liar"}',
        rationale="r",
        status=P.STATUS_REJECTED_NAME,
        status_reason="name token(s) not reflected in description/endpoint: ['liar']",
        proposed_by="morgoth",
        engine="claude-cli",
    )
    assert isinstance(pid, str) and len(pid) == 36


# ---------- end-to-end: rejected_name outcome propagates + retries -------

# Coherent VALID_SPEC — used as the "good" reply on the retry side.
COHERENT_SPEC = {
    "tool_name": "get_price_test",
    "api_base_url": "https://api.example.com",
    "endpoint_path": "/v1/price",
    "digest_fields": ["a", "b", "c"],
    "description": "Fetch the current price for a test asset.",
    "rationale": "coverage gap",
}

# The incoherent one — 'liar' in the name is missing from desc/endpoint.
LIAR_SPEC = {
    "tool_name": "get_price_liar",
    "api_base_url": "https://api.example.com",
    "endpoint_path": "/v1/price",
    "digest_fields": ["a", "b", "c"],
    "description": "Fetch the current price for a test asset.",
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


def _http_stack(bodies: list[Any]) -> Any:
    responses = [
        SimpleNamespace(status_code=200, json=MagicMock(return_value=b))
        for b in bodies
    ]
    fake = AsyncMock()
    fake.__aenter__ = AsyncMock(return_value=fake)
    fake.__aexit__ = AsyncMock(return_value=None)
    fake.get = AsyncMock(side_effect=responses)
    return fake


async def _drive(specs: list[dict[str, Any]], bodies: list[Any], store: MagicMock) -> tuple[dict[str, Any], list[str]]:
    calls: list[str] = []

    async def _rc(prompt: str, *_a: Any, **_kw: Any) -> tuple[str, dict[str, Any]]:
        calls.append(prompt)
        return _json.dumps(specs[len(calls) - 1]), {}

    pm = MagicMock()
    with patch("self_modify.reflect.P.ProposalStore", return_value=store), \
         patch("self_modify.reflect._build_context",
               AsyncMock(return_value={
                   "tools_block": "", "objectives_block": "",
                   "theses_block": "", "rejections_block": "",
               })), \
         patch("self_modify.reflect.tool_name_collides", return_value=False), \
         patch("self_modify.reflect.reflect_chat", side_effect=_rc), \
         patch.object(reflect, "socket", MagicMock(
             getaddrinfo=MagicMock(return_value=[
                 (None, None, None, None, ("104.16.0.1", 0))
             ])
         )), \
         patch.object(reflect.httpx, "AsyncClient", return_value=_http_stack(bodies)), \
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
async def test_rejected_name_persists_and_triggers_retry() -> None:
    """Liar spec → rejected_name (persisted) → corrective retry with
    the missing tokens → coherent spec → submitted."""
    store = _mock_store()
    result, calls = await _drive(
        [LIAR_SPEC, COHERENT_SPEC],
        [{"a": 1, "b": 2, "c": 3}],  # only retry reaches HTTP
        store,
    )
    assert result["outcome"] == "submitted"
    assert result["retried"] is True
    assert result["first_attempt"]["outcome"] == "rejected_name"
    # Corrective prompt carries the missing tokens verbatim.
    assert "liar" in calls[1]
    # The rejected_name row was persisted with the correct status.
    store.submit_terminal.assert_awaited_once()
    st_kwargs = store.submit_terminal.await_args.kwargs
    assert st_kwargs["status"] == P.STATUS_REJECTED_NAME
    assert st_kwargs["retry_of"] is None
    # Successful retry links back.
    assert store.submit.await_args.kwargs["retry_of"] == "reject-1"


@pytest.mark.asyncio
async def test_rejected_name_second_reject_no_third_attempt() -> None:
    """Liar × 2 → both rejected_name; second row links to first, no
    third attempt."""
    store = _mock_store()
    store.submit_terminal = AsyncMock(side_effect=["reject-first", "reject-retry"])
    result, calls = await _drive(
        [LIAR_SPEC, LIAR_SPEC],
        [],
        store,
    )
    assert result["outcome"] == "rejected_name"
    assert result["retried"] is True
    assert len(calls) == 2
    st_kwargs = [c.kwargs for c in store.submit_terminal.await_args_list]
    assert len(st_kwargs) == 2
    assert st_kwargs[0]["retry_of"] is None
    assert st_kwargs[1]["retry_of"] == "reject-first"


@pytest.mark.asyncio
async def test_coherent_spec_reaches_submit_without_retry() -> None:
    store = _mock_store()
    result, calls = await _drive(
        [COHERENT_SPEC],
        [{"a": 1, "b": 2, "c": 3}],
        store,
    )
    assert result["outcome"] == "submitted"
    assert result["retried"] is False
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_coherence_runs_before_collision() -> None:
    """A colliding-name spec whose name ALSO lies must be rejected by
    coherence first (deterministic order documents the pipeline)."""
    store = _mock_store()
    # tool_name has an incoherent token AND would collide.
    lying_colliding = dict(LIAR_SPEC, tool_name="get_news_flows")
    calls: list[str] = []

    async def _rc(prompt: str, *_a: Any, **_kw: Any) -> tuple[str, dict[str, Any]]:
        calls.append(prompt)
        return _json.dumps(lying_colliding), {}

    pm = MagicMock()
    # get_news IS registered — but coherence should fire FIRST since
    # 'flows' isn't in the description.
    with patch("self_modify.reflect.P.ProposalStore", return_value=store), \
         patch("self_modify.reflect._build_context",
               AsyncMock(return_value={
                   "tools_block": "", "objectives_block": "",
                   "theses_block": "", "rejections_block": "",
               })), \
         patch("self_modify.reflect.tool_name_collides", return_value=True), \
         patch("self_modify.reflect.reflect_chat", side_effect=_rc), \
         patch.object(reflect.httpx, "AsyncClient", return_value=_http_stack([])), \
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
    # first attempt rejects for name, then retry also lies → still name.
    assert result["outcome"] == "rejected_name"
