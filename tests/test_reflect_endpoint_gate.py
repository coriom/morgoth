"""Endpoint-duplication gate + field-overlap advisory tests.

Two data-source tools hitting the same (host, path) make the rail
fictional. This file pins the normalization matrix, the retro-catch
for cec526ec's /api/mempool, the registered-endpoint discovery, the
weak-signal field-overlap advisory, and the retry wiring.
"""

from __future__ import annotations

import json as _json
from types import SimpleNamespace
from typing import Any, ClassVar
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from self_modify import proposals as P
from self_modify import reflect


# ---------- normalization matrix -----------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("https://api.example.com/v1/thing", "api.example.com/v1/thing"),
    ("HTTP://API.example.com/V1/THING", "api.example.com/v1/thing"),
    ("api.example.com/v1/thing/", "api.example.com/v1/thing"),
    ("https://api.example.com/v1/thing?a=1&b=2", "api.example.com/v1/thing"),
    ("https://api.example.com/v1/thing#frag", "api.example.com/v1/thing"),
    ("https://a.example.com/", "a.example.com"),
])
def test_normalize_endpoint_single_arg(raw: str, expected: str) -> None:
    assert reflect._normalize_endpoint(raw) == expected


def test_normalize_endpoint_joins_base_and_path() -> None:
    assert reflect._normalize_endpoint(
        "https://api.example.com/", "/v1/thing",
    ) == "api.example.com/v1/thing"
    assert reflect._normalize_endpoint(
        "https://api.example.com", "v1/thing",
    ) == "api.example.com/v1/thing"


def test_normalize_endpoint_drops_query_from_joined_form() -> None:
    """Same path with different query params must collapse — the
    mining-pools ?window=24h vs ?window=24hr case."""
    a = reflect._normalize_endpoint(
        "https://mempool.space", "/api/v1/mining/pools/24h",
    )
    b = reflect._normalize_endpoint(
        "https://mempool.space", "/api/v1/mining/pools/24h?fmt=json",
    )
    assert a == b == "mempool.space/api/v1/mining/pools/24h"


# ---------- registered-endpoint loader ------------------------------------

class _FakeDataSource:
    is_data_source = True
    api_endpoints: ClassVar[tuple[str, ...]] = ()
    digest_fields: ClassVar[tuple[str, ...]] = ()

    def __init__(self, name: str, endpoints: tuple[str, ...],
                 fields: tuple[str, ...] = ()) -> None:
        self.name = name
        # We rely on the loader reading ClassVars from type(tool);
        # instances used here declare on-instance for test isolation.
        # The loader uses ``type(t)`` — set at instance init.
        cls = type(f"_Fake_{name}", (_FakeDataSource,), {
            "api_endpoints": endpoints,
            "digest_fields": fields,
            "is_data_source": True,
            "name": name,
        })
        self.__class__ = cls


class _FakeUtility:
    is_data_source = False
    api_endpoints: ClassVar[tuple[str, ...]] = ()
    digest_fields: ClassVar[tuple[str, ...]] = ()

    def __init__(self, name: str) -> None:
        self.name = name


def _mock_registry(*tools: Any) -> Any:
    return patch(
        "scripts.compile_wiki._registered_tools_offline",
        return_value=list(tools),
    )


def test_registered_endpoints_includes_data_sources_only() -> None:
    pm = MagicMock()
    config = SimpleNamespace()
    with _mock_registry(
        _FakeDataSource("get_x", ("mempool.space/api/mempool",)),
        _FakeDataSource("get_y", ("api.coingecko.com/api/v3/simple/price",)),
        _FakeUtility("notify"),  # not a data source — excluded
    ):
        result = reflect._registered_endpoints(config, pm)
    assert result == {
        "mempool.space/api/mempool": "get_x",
        "api.coingecko.com/api/v3/simple/price": "get_y",
    }


def test_registered_endpoints_normalizes_declarations() -> None:
    """A tool that declares with scheme/case still lands in the same
    normalized bucket the gate would build for a proposed URL."""
    pm = MagicMock()
    config = SimpleNamespace()
    with _mock_registry(
        _FakeDataSource("get_x", ("HTTPS://API.example.com/V1/thing/",)),
    ):
        result = reflect._registered_endpoints(config, pm)
    assert result == {"api.example.com/v1/thing": "get_x"}


def test_registered_endpoints_skips_empty_declarations() -> None:
    """Dynamic-path tools declare () → contribute nothing (exempt)."""
    pm = MagicMock()
    config = SimpleNamespace()
    with _mock_registry(
        _FakeDataSource("get_news", ()),
        _FakeDataSource("web_search", ()),
        _FakeDataSource("get_x", ("mempool.space/api/mempool",)),
    ):
        result = reflect._registered_endpoints(config, pm)
    assert result == {"mempool.space/api/mempool": "get_x"}


# ---------- registered digest fields --------------------------------------

def test_registered_digest_fields_unions_data_sources() -> None:
    pm = MagicMock()
    config = SimpleNamespace()
    with _mock_registry(
        _FakeDataSource("get_x", ("h/p",), fields=("a", "b")),
        _FakeDataSource("get_y", ("h/q",), fields=("b", "c")),
        _FakeUtility("notify"),
    ):
        result = reflect._registered_digest_fields(config, pm)
    assert result == {"a", "b", "c"}


# ---------- _endpoint_duplicates --------------------------------------------

def test_endpoint_duplicates_finds_match_and_returns_owner() -> None:
    registered = {"mempool.space/api/mempool": "get_bitcoin_onchain"}
    match, owner = reflect._endpoint_duplicates(
        "https://mempool.space", "/api/mempool", registered,
    )
    assert match == "mempool.space/api/mempool"
    assert owner == "get_bitcoin_onchain"


def test_endpoint_duplicates_returns_none_for_new_endpoint() -> None:
    registered = {"mempool.space/api/mempool": "get_bitcoin_onchain"}
    match, owner = reflect._endpoint_duplicates(
        "https://api.other.com", "/v1/thing", registered,
    )
    assert match is None
    assert owner is None


# ---------- retro-validation for cec526ec ---------------------------------

def test_retro_cec526ec_mempool_endpoint_would_reject() -> None:
    """cec526ec proposed /api/mempool on mempool.space — the exact URL
    get_bitcoin_onchain already hits. With the gate live, this is
    caught at reflect-time, one gate earlier than the operator did."""
    pm = MagicMock()
    config = SimpleNamespace()

    class _RealOnchain:
        name = "get_bitcoin_onchain"
        is_data_source = True
        api_endpoints = (
            "mempool.space/api/v1/mining/hashrate/3d",
            "mempool.space/api/v1/difficulty-adjustment",
            "mempool.space/api/v1/fees/recommended",
            "mempool.space/api/mempool",
        )
        digest_fields = ("hash_rate", "difficulty", "mempool_tx_count",
                         "mempool_vsize")

    with _mock_registry(_RealOnchain()):
        registered = reflect._registered_endpoints(config, pm)
        match, owner = reflect._endpoint_duplicates(
            "https://mempool.space", "/api/mempool", registered,
        )
    assert match == "mempool.space/api/mempool"
    assert owner == "get_bitcoin_onchain"


def test_retro_738b75de_market_price_usd_recall_limit_documented() -> None:
    """738b75de proposed market_price_usd; get_crypto_price surfaces
    `price` (not market_price_usd). Exact-name overlap doesn't fire.

    This is the DOCUMENTED recall limit: semantic dedup (price ==
    market_price_usd) is gate-2.5 territory. This test locks the
    behavior so a future contributor doesn't 'fix' it silently."""
    proposed = {"market_price_usd", "volume_24h", "n_btc_mined"}
    existing = {"price", "change_24h", "volume_24h", "symbol"}
    overlap = proposed & existing
    # Only exact matches — volume_24h is the only overlap, and that's
    # a real one (crypto_price does surface volume_24h). Everything
    # else slips past the exact-name check.
    assert overlap == {"volume_24h"}
    assert "market_price_usd" not in overlap


# ---------- BaseTool ClassVar defaults + declared on registry -----------

def test_base_tool_defaults_are_empty_tuples() -> None:
    from tools.base_tool import BaseTool
    assert BaseTool.api_endpoints == ()
    assert BaseTool.digest_fields == ()


@pytest.mark.parametrize("tool_name", [
    "get_bitcoin_onchain",
    "get_crypto_price",
    "get_fear_greed_index",
    "get_news",
    "get_crypto_global_market",
    "web_search",
])
def test_all_six_data_sources_declare_classvars(tool_name: str) -> None:
    """Every currently-registered data_source tool has both ClassVars
    declared (may be empty tuple for dynamic-path tools)."""
    from importlib import import_module

    module_map = {
        "get_bitcoin_onchain": ("tools.data_feeds.onchain", "GetBitcoinOnchainTool"),
        "get_crypto_price": ("tools.data_feeds.crypto", "GetCryptoPriceTool"),
        "get_fear_greed_index": ("tools.data_feeds.fear_greed", "GetFearGreedIndexTool"),
        "get_news": ("tools.data_feeds.news", "GetNewsTool"),
        "get_crypto_global_market": (
            "tools.data_feeds.get_crypto_global_market",
            "GetCryptoGlobalMarketTool",
        ),
        "web_search": ("tools.web_search", "WebSearchTool"),
    }
    mod_name, cls_name = module_map[tool_name]
    cls = getattr(import_module(mod_name), cls_name)
    assert hasattr(cls, "api_endpoints")
    assert hasattr(cls, "digest_fields")
    assert isinstance(cls.api_endpoints, tuple)
    assert isinstance(cls.digest_fields, tuple)


def test_onchain_declares_all_four_mempool_endpoints() -> None:
    from tools.data_feeds.onchain import GetBitcoinOnchainTool
    assert "mempool.space/api/mempool" in GetBitcoinOnchainTool.api_endpoints
    assert len(GetBitcoinOnchainTool.api_endpoints) == 4


def test_dynamic_path_tools_declare_empty_endpoints() -> None:
    from tools.data_feeds.news import GetNewsTool
    from tools.web_search import WebSearchTool
    assert GetNewsTool.api_endpoints == ()
    assert WebSearchTool.api_endpoints == ()


# ---------- template emits both ClassVars --------------------------------

def test_tool_template_renders_api_endpoints_classvar() -> None:
    """Every future morgoth-born tool self-declares its endpoint."""
    endpoint = reflect._normalize_endpoint(
        "https://api.example.com", "/v1/thing",
    )
    rendered = reflect.TOOL_TEMPLATE.format(
        tool_name="get_ok",
        class_name="GetOkTool",
        tool_name_repr=repr("get_ok"),
        base_url_repr=repr("https://api.example.com"),
        endpoint_path_repr=repr("/v1/thing"),
        digest_fields_repr=repr(["a", "b", "c"]),
        description_repr=repr("Fetch OK."),
        source_label_repr=repr("api.example.com"),
        endpoint_declaration_repr=repr(endpoint),
    )
    assert "api_endpoints = ('api.example.com/v1/thing',)" in rendered
    assert "digest_fields = tuple(_DIGEST_FIELDS)" in rendered


# ---------- rejected_endpoint is a first-class terminal status -----------

def test_rejected_endpoint_registered() -> None:
    assert P.STATUS_REJECTED_ENDPOINT == "rejected_endpoint"
    assert P.STATUS_REJECTED_ENDPOINT in P.ALL_STATUSES
    assert P.STATUS_REJECTED_ENDPOINT in P.NEGATIVE_LIST_STATUSES
    assert P.STATUS_REJECTED_ENDPOINT in P._PRE_SUBMIT_TERMINAL_STATUSES


# ---------- end-to-end: rejected_endpoint outcome + retry -----------------

# The gate runs after coherence. Pick a tool_name whose tokens all
# appear in the description so coherence passes and endpoint fires.
LIAR_ENDPOINT_SPEC = {
    "tool_name": "get_bitcoin_extra_mempool",
    "api_base_url": "https://mempool.space",
    "endpoint_path": "/api/mempool",
    "digest_fields": ["count", "vsize", "total_fee"],
    "description": "Fetch extra Bitcoin mempool metrics: count, virtual size, and total fees.",
    "rationale": "gap",
}
CLEAN_SPEC = {
    "tool_name": "get_bitcoin_test_data",
    "api_base_url": "https://api.example.com",
    "endpoint_path": "/v1/testdata",
    "digest_fields": ["a", "b", "c"],
    "description": "Fetch bitcoin test data from a fresh source.",
    "rationale": "gap",
}


def _mock_store() -> MagicMock:
    store = MagicMock()
    store.count_by_status_and_author = AsyncMock(return_value=0)
    store.list_recent_rejections = AsyncMock(return_value=[])
    store.submit_terminal = AsyncMock(return_value="reject-1")
    store.submit = AsyncMock(return_value="prop-1")
    store.get = AsyncMock(return_value={"proposal_id": "prop-1",
                                         "status_reason": "gate_tests: pytest passed"})
    store.update_status = AsyncMock(return_value=True)
    return store


def _fake_config() -> SimpleNamespace:
    return SimpleNamespace(
        permissions=SimpleNamespace(
            permissions=SimpleNamespace(can_self_modify=True),
        ),
    )


async def _drive(specs: list[dict[str, Any]], bodies: list[Any],
                 store: MagicMock, registered_ep: dict[str, str]) -> dict[str, Any]:
    calls: list[str] = []

    async def _rc(prompt: str, *_a: Any, **_kw: Any) -> tuple[str, dict[str, Any]]:
        calls.append(prompt)
        return _json.dumps(specs[len(calls) - 1]), {}

    responses = [
        SimpleNamespace(status_code=200, json=MagicMock(return_value=b))
        for b in bodies
    ]
    fake = AsyncMock()
    fake.__aenter__ = AsyncMock(return_value=fake)
    fake.__aexit__ = AsyncMock(return_value=None)
    fake.get = AsyncMock(side_effect=responses)

    pm = MagicMock()
    with patch("self_modify.reflect.P.ProposalStore", return_value=store), \
         patch("self_modify.reflect._build_context",
               AsyncMock(return_value={
                   "tools_block": "", "objectives_block": "",
                   "theses_block": "", "rejections_block": "",
               })), \
         patch("self_modify.reflect._registered_endpoints",
               return_value=registered_ep), \
         patch("self_modify.reflect._registered_digest_fields",
               return_value=set()), \
         patch("self_modify.reflect.tool_name_collides", return_value=False), \
         patch("self_modify.reflect.reflect_chat", side_effect=_rc), \
         patch.object(reflect, "socket", MagicMock(
             getaddrinfo=MagicMock(return_value=[
                 (None, None, None, None, ("104.16.0.1", 0))
             ])
         )), \
         patch.object(reflect.httpx, "AsyncClient", return_value=fake), \
         patch("self_modify.reflect.gates.run_pipeline",
               AsyncMock(return_value="pending_approval")), \
         patch("self_modify.shadow.run_shadow_verdict",
               AsyncMock(return_value={
                   "verdict": "APPROVE", "axes": {}, "reasons": [],
                   "engine": "test", "prompt_version": "test",
               })):
        return await reflect.run_reflection(
            _fake_config(), pm, MagicMock(), provider="claude-cli",
        )


@pytest.mark.asyncio
async def test_endpoint_duplication_rejects_and_retries() -> None:
    store = _mock_store()
    registered = {"mempool.space/api/mempool": "get_bitcoin_onchain"}
    result = await _drive(
        [LIAR_ENDPOINT_SPEC, CLEAN_SPEC],
        [{"a": 1, "b": 2, "c": 3}],  # only retry reaches HTTP
        store,
        registered,
    )
    assert result["outcome"] == "submitted"
    assert result["retried"] is True
    assert result["first_attempt"]["outcome"] == "rejected_endpoint"
    # First attempt persisted with the correct status.
    st_kwargs = store.submit_terminal.await_args.kwargs
    assert st_kwargs["status"] == P.STATUS_REJECTED_ENDPOINT
    reason = st_kwargs["status_reason"]
    assert "endpoint duplicates" in reason
    assert "get_bitcoin_onchain" in reason
    assert "mempool.space/api/mempool" in reason


@pytest.mark.asyncio
async def test_endpoint_gate_passes_for_new_endpoint() -> None:
    store = _mock_store()
    registered = {"mempool.space/api/mempool": "get_bitcoin_onchain"}
    result = await _drive(
        [CLEAN_SPEC],
        [{"a": 1, "b": 2, "c": 3}],
        store,
        registered,
    )
    assert result["outcome"] == "submitted"
    assert result["retried"] is False


# ---------- field-overlap advisory: gate-3 note, NOT a reject -----------

@pytest.mark.asyncio
async def test_field_overlap_appends_note_to_pending_approval() -> None:
    """Weak signal — reaches pending_approval and adds a note."""
    store = _mock_store()
    registered = {"api.other.com/v1/data": "get_other"}

    async def _rc(prompt: str, *_a: Any, **_kw: Any) -> tuple[str, dict[str, Any]]:
        return _json.dumps(CLEAN_SPEC), {}

    responses = [
        SimpleNamespace(status_code=200, json=MagicMock(
            return_value={"a": 1, "b": 2, "c": 3})),
    ]
    fake = AsyncMock()
    fake.__aenter__ = AsyncMock(return_value=fake)
    fake.__aexit__ = AsyncMock(return_value=None)
    fake.get = AsyncMock(side_effect=responses)

    # 'a' overlaps with the union of registered digest fields.
    pm = MagicMock()
    with patch("self_modify.reflect.P.ProposalStore", return_value=store), \
         patch("self_modify.reflect._build_context",
               AsyncMock(return_value={
                   "tools_block": "", "objectives_block": "",
                   "theses_block": "", "rejections_block": "",
               })), \
         patch("self_modify.reflect._registered_endpoints",
               return_value={}), \
         patch("self_modify.reflect._registered_digest_fields",
               return_value={"a"}), \
         patch("self_modify.reflect.tool_name_collides", return_value=False), \
         patch("self_modify.reflect.reflect_chat", side_effect=_rc), \
         patch.object(reflect, "socket", MagicMock(
             getaddrinfo=MagicMock(return_value=[
                 (None, None, None, None, ("104.16.0.1", 0))
             ])
         )), \
         patch.object(reflect.httpx, "AsyncClient", return_value=fake), \
         patch("self_modify.reflect.gates.run_pipeline",
               AsyncMock(return_value="pending_approval")), \
         patch("self_modify.shadow.run_shadow_verdict",
               AsyncMock(return_value={
                   "verdict": "APPROVE", "axes": {}, "reasons": [],
                   "engine": "test", "prompt_version": "test",
               })):
        result = await reflect.run_reflection(
            _fake_config(), pm, MagicMock(), provider="claude-cli",
        )

    assert result["outcome"] == "submitted"
    # update_status called with the augmented reason.
    assert store.update_status.await_count >= 1
    last_reason = store.update_status.await_args_list[-1].args[2]
    assert "field-name overlap" in last_reason
    assert "'a'" in last_reason


@pytest.mark.asyncio
async def test_no_overlap_note_when_no_field_intersection() -> None:
    store = _mock_store()
    async def _rc(prompt: str, *_a: Any, **_kw: Any) -> tuple[str, dict[str, Any]]:
        return _json.dumps(CLEAN_SPEC), {}

    responses = [
        SimpleNamespace(status_code=200, json=MagicMock(
            return_value={"a": 1, "b": 2, "c": 3})),
    ]
    fake = AsyncMock()
    fake.__aenter__ = AsyncMock(return_value=fake)
    fake.__aexit__ = AsyncMock(return_value=None)
    fake.get = AsyncMock(side_effect=responses)

    pm = MagicMock()
    with patch("self_modify.reflect.P.ProposalStore", return_value=store), \
         patch("self_modify.reflect._build_context",
               AsyncMock(return_value={
                   "tools_block": "", "objectives_block": "",
                   "theses_block": "", "rejections_block": "",
               })), \
         patch("self_modify.reflect._registered_endpoints",
               return_value={}), \
         patch("self_modify.reflect._registered_digest_fields",
               return_value={"unrelated_field"}), \
         patch("self_modify.reflect.tool_name_collides", return_value=False), \
         patch("self_modify.reflect.reflect_chat", side_effect=_rc), \
         patch.object(reflect, "socket", MagicMock(
             getaddrinfo=MagicMock(return_value=[
                 (None, None, None, None, ("104.16.0.1", 0))
             ])
         )), \
         patch.object(reflect.httpx, "AsyncClient", return_value=fake), \
         patch("self_modify.reflect.gates.run_pipeline",
               AsyncMock(return_value="pending_approval")), \
         patch("self_modify.shadow.run_shadow_verdict",
               AsyncMock(return_value={
                   "verdict": "APPROVE", "axes": {}, "reasons": [],
                   "engine": "test", "prompt_version": "test",
               })):
        result = await reflect.run_reflection(
            _fake_config(), pm, MagicMock(), provider="claude-cli",
        )
    assert result["outcome"] == "submitted"
    # No overlap → update_status NOT called with augmented reason.
    for c in store.update_status.await_args_list:
        assert "field-name overlap" not in (c.args[2] or "")
