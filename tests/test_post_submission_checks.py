"""Full-submission-path coverage for the post-submission checks.

Motivating regression: 9f446bb4 (2026-09-29). Reflect autonomously
submitted a path-digest tool. Gate_tests passed. Then the checks
after gate_tests crashed on ``set(spec["digest_fields"])`` (dict
entries are unhashable) — reflect unwound, the proposal sat at
pending_approval with liveness/overlap/shadow SILENTLY UNEVALUATED.
The gate-selftest of 6bbfb9f only exercised gate_zone + gate_tests;
this suite adds the missing path-digest coverage AND the
crashed-check-holds invariant.

Contract locked by this suite:

  · normalize_digest_fields accepts BOTH strings and {name,path}
    dicts and returns a canonical list of dicts.
  · The full run_reflection path with a path-digest spec reaches
    ``submitted`` and every post-submission check runs.
  · A crash in ANY post-submission check → STATUS_CHECKS_INCOMPLETE,
    the check name recorded in status_reason so ``morgoth show``
    surfaces it, and delegation IS NOT allowed to flip the proposal
    under this condition (crashed shadow != REJECT verdict).
  · The status is registered in ALL_STATUSES so the state machine
    can accept it.
"""

from __future__ import annotations

import json as _json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from self_modify import reflect
from self_modify import proposals as P
from self_modify import post_submission_checks as pchecks
from self_modify.digest_path import (
    normalize_digest_fields, digest_field_names,
)


PATH_DIGEST_SPEC = {
    "tool_name": "get_test_stablecoins",
    "api_base_url": "https://stablecoins.example.com",
    "endpoint_path": "/stablecoins",
    "digest_fields": [
        {"name": "total_supply",
         "path": "sum(peggedAssets[*].circulating.peggedUSD)"},
        "usdt_symbol",   # legacy string form — must coexist with dict entries
        {"name": "asset_count", "path": "count(peggedAssets[*])"},
    ],
    "description": (
        "Fetch aggregate stablecoin issuance test digest for the rail."
    ),
    "rationale": (
        "test-suite path-digest coverage; total_supply is the "
        "concrete field that measures the claimed gap."
    ),
}


# The response body every fake HTTP hit returns — a nested structure
# that ONLY the path-digest resolver can extract. The legacy top-level
# scalar name (usdt_symbol) is also present so the shape gate passes.
BODY = {
    "peggedAssets": [
        {"symbol": "USDT", "circulating": {"peggedUSD": 100_000_000_000}},
        {"symbol": "USDC", "circulating": {"peggedUSD":  40_000_000_000}},
    ],
    "usdt_symbol": "USDT",
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


def _mock_env() -> tuple[MagicMock, AsyncMock]:
    """Return (store_mock, fake_http_client) wired for the happy path."""
    store_mock = MagicMock()
    store_mock.count_by_status_and_author = AsyncMock(return_value=0)
    store_mock.submit = AsyncMock(return_value="prop-path-1")
    _row = {"proposal_id": "prop-path-1", "status_reason": ""}
    async def _get(_pid: str):
        return _row
    async def _update(pid: str, status: str, reason: str = "") -> None:
        _row["status"] = status
        _row["status_reason"] = reason
    store_mock.get = AsyncMock(side_effect=_get)
    store_mock.update_status = AsyncMock(side_effect=_update)
    fake_resp = SimpleNamespace(status_code=200, json=MagicMock(return_value=BODY))
    fake_client = AsyncMock()
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)
    fake_client.get = AsyncMock(return_value=fake_resp)
    return store_mock, fake_client


# --------- unit: normalize + names ----------------------------------------

def test_normalize_upgrades_strings_and_preserves_dicts() -> None:
    entries = ["total_supply",
               {"name": "asset_count", "path": "count(peggedAssets[*])"}]
    out = normalize_digest_fields(entries)
    assert out == [
        {"name": "total_supply", "path": "total_supply"},
        {"name": "asset_count", "path": "count(peggedAssets[*])"},
    ]
    assert digest_field_names(entries) == ["total_supply", "asset_count"]


def test_normalize_never_raises_on_bad_entry() -> None:
    # Malformed entries are DROPPED with a WARN — pre-submit validation
    # is the authoritative rejection path; normalize is defence-in-depth.
    out = normalize_digest_fields([{"name": "TotallyWrong", "path": "x"}])
    assert out == []


# --------- crashed-check contract ------------------------------------------

@pytest.mark.asyncio
async def test_liveness_crash_holds_at_checks_incomplete() -> None:
    """A liveness crash MUST NOT let the proposal reach pending_approval
    silently. The row lands at STATUS_CHECKS_INCOMPLETE with the check
    name in status_reason."""
    store, _ = _mock_env()

    def _boom(probe, spec):
        raise TypeError("unhashable type: 'dict'")

    with patch.object(pchecks, "_liveness_check", _boom):
        results, final = await pchecks.run_post_submission_checks(
            store=store, proposal_id="prop-path-1",
            spec=PATH_DIGEST_SPEC, probe={"hits": []},
            registered_field_names=set(),
            config=_fake_config(), pm=MagicMock(),
            delegation_enabled=False,
        )
    assert final == P.STATUS_CHECKS_INCOMPLETE
    # Named in results and in the persisted status_reason.
    crashed = [r for r in results if r.status == "crashed"]
    assert any(r.name == pchecks.CHECK_LIVENESS for r in crashed)
    reason = store.update_status.await_args.args[2]
    assert "checks_incomplete" in reason
    assert "liveness" in reason
    assert "unhashable" in reason


@pytest.mark.asyncio
async def test_shadow_crash_holds_at_checks_incomplete() -> None:
    store, _ = _mock_env()
    with patch("self_modify.shadow.run_shadow_verdict",
               AsyncMock(side_effect=RuntimeError("model unreachable"))):
        results, final = await pchecks.run_post_submission_checks(
            store=store, proposal_id="prop-path-1",
            spec=PATH_DIGEST_SPEC, probe={"hits": []},
            registered_field_names=set(),
            config=_fake_config(), pm=MagicMock(),
            delegation_enabled=True,   # even under delegation on
        )
    assert final == P.STATUS_CHECKS_INCOMPLETE
    assert any(r.name == pchecks.CHECK_SHADOW and r.status == "crashed"
               for r in results)
    # Delegation MUST NOT flip on a crashed shadow — the verdict is
    # not "REJECT", it's absent. The row is held for operator review.
    assert not any(r.name == pchecks.CHECK_DELEGATION for r in results)


@pytest.mark.asyncio
async def test_overlap_note_names_registered_field() -> None:
    """Mixed digest_fields (strings + dicts) intersect cleanly against
    the registered-name set — the crash-site pattern of 9f446bb4."""
    store, _ = _mock_env()
    with patch("self_modify.shadow.run_shadow_verdict",
               AsyncMock(return_value={
                   "verdict": "APPROVE", "axes": {}, "reasons": [],
                   "engine": "t", "prompt_version": "t",
               })):
        results, final = await pchecks.run_post_submission_checks(
            store=store, proposal_id="prop-path-1",
            spec=PATH_DIGEST_SPEC, probe={"hits": []},
            registered_field_names={"total_supply", "unrelated"},
            config=_fake_config(), pm=MagicMock(),
            delegation_enabled=False,
        )
    assert final == P.STATUS_PENDING_APPROVAL
    overlap_r = next(r for r in results if r.name == pchecks.CHECK_OVERLAP)
    assert overlap_r.status == "warn"
    assert "total_supply" in overlap_r.message


# --------- end-to-end: reflect with path-digest spec ----------------------

@pytest.mark.asyncio
async def test_run_reflection_end_to_end_path_digest_spec_reaches_submitted(
    monkeypatch,
) -> None:
    """The 9f446bb4-shape regression: run_reflection with a path-digest
    spec whose digest_fields mixes string and dict entries MUST reach
    outcome=submitted and pending_approval. The pre-fix crash at
    ``set(spec['digest_fields'])`` would have raised TypeError here."""
    # Explicit — belt+braces with tests/fixtures/test.env — so the
    # test never spawns a real gate-selftest sandbox even under a
    # polluted-env parallel worker.
    monkeypatch.setenv("MORGOTH_REFLECT_GATE_PREFLIGHT", "0")
    pm = MagicMock()
    store, fake_client = _mock_env()

    with patch("self_modify.reflect.P.ProposalStore", return_value=store), \
         patch("self_modify.reflect._build_context",
               AsyncMock(return_value={
                   "tools_block": "", "objectives_block": "",
                   "theses_block": "",
               })), \
         patch("self_modify.reflect._registered_endpoints", return_value={}), \
         patch("self_modify.reflect._registered_digest_fields",
               return_value=set()), \
         patch("self_modify.reflect.tool_name_collides", return_value=False), \
         patch.object(reflect, "socket", MagicMock(
             getaddrinfo=MagicMock(return_value=[
                 (None, None, None, None, ("104.16.0.1", 0))
             ])
         )), \
         patch.object(reflect.httpx, "AsyncClient", return_value=fake_client), \
         patch("self_modify.reflect.gates.run_pipeline",
               AsyncMock(return_value=P.STATUS_PENDING_APPROVAL)), \
         patch("self_modify.shadow.run_shadow_verdict",
               AsyncMock(return_value={
                   "verdict": "APPROVE", "axes": {}, "reasons": [],
                   "engine": "t", "prompt_version": "t",
               })), \
         patch("self_modify.reflect.liveness.run_liveness_probe",
               AsyncMock(return_value={
                   "url": "x", "hits": [
                       {"i": i, "ok": True,
                        "vals": {"total_supply": 140_000_000_000 + i * 1e8,
                                 "usdt_symbol": "USDT",
                                 "asset_count": 2 + (i % 2)}}
                       for i in range(4)
                   ],
                   "n_hits": 4, "gap_secs": 150,
                   "digest_fields": ["total_supply", "usdt_symbol",
                                     "asset_count"],
               })):
        result = await reflect.run_reflection(
            _fake_config(), pm, _fake_llm(_json.dumps(PATH_DIGEST_SPEC)),
        )

    assert result["outcome"] == "submitted", result
    assert result["pipeline_status"] == P.STATUS_PENDING_APPROVAL


# --------- artifact check (routes through the sandbox runner) ------------

def _mock_runner(kind: str, message: str, detail: dict[str, Any] | None = None,
                  ok: bool = False) -> Any:
    """Build a patched artifact_runner.run_artifact_in_sandbox that
    returns a pre-shaped ArtifactResult. Every hermetic artifact-check
    unit test uses this — the real bwrap runner is exercised ONLY by
    the integration tests below."""
    from self_modify.artifact_runner import ArtifactResult
    def _fn(content: str, target_path: str, body: Any, **kw: Any) -> ArtifactResult:
        return ArtifactResult(
            ok=ok, kind=kind, message=message, detail=detail or {},
        )
    return _fn


def test_artifact_check_rejects_on_module_exec_crash() -> None:
    """A syntax error at module scope → REJECT with the harness's
    module_exec kind + traceback tail. Hermetic — mocks the bwrap
    runner; the real bwrap path is exercised by the integration
    tests below."""
    from self_modify.post_submission_checks import _artifact_check, CHECK_ARTIFACT
    from self_modify import artifact_runner
    row = {"content": "1 = 2\n", "target_path": "tools/data_feeds/x.py"}
    probe = {"hits": [{"ok": True, "body": {"x": 1}}]}
    with patch.object(
        artifact_runner, "run_artifact_in_sandbox",
        _mock_runner("module_exec",
                     "SyntaxError: cannot assign to literal here",
                     detail={"traceback_tail": "SyntaxError: ..."}),
    ):
        r = _artifact_check(row, probe)
    assert r.name == CHECK_ARTIFACT
    assert r.status == "reject"
    assert "module_exec" in r.message
    assert "SyntaxError" in r.message


def test_artifact_check_rejects_on_execute_crash() -> None:
    """A crash INSIDE execute() (e.g. the 9f446bb4 shape:
    ``for key in _DIGEST_FIELDS: if key in data`` where _DIGEST_FIELDS
    is a list of dicts) → REJECT with kind=execute + traceback tail."""
    from self_modify.post_submission_checks import _artifact_check
    from self_modify import artifact_runner
    row = {"content": "# rendered\n", "target_path": "tools/data_feeds/x.py"}
    probe = {"hits": [{"ok": True, "body": {"x": 1}}]}
    with patch.object(
        artifact_runner, "run_artifact_in_sandbox",
        _mock_runner("execute", "TypeError: unhashable type: 'dict'"),
    ):
        r = _artifact_check(row, probe)
    assert r.status == "reject"
    assert "unhashable" in r.message


def test_artifact_check_ok_when_harness_returns_values() -> None:
    """OK verdict — hermetic. The real harness+bwrap is exercised
    by the integration tests below."""
    from self_modify.post_submission_checks import _artifact_check
    from self_modify import artifact_runner
    row = {"content": "# rendered\n", "target_path": "tools/data_feeds/x.py"}
    probe = {"hits": [{"ok": True, "body": {"x": 1}}]}
    with patch.object(
        artifact_runner, "run_artifact_in_sandbox",
        _mock_runner("ok", "executed against recorded body → 3 value(s) extracted",
                     detail={"digest": {"a": 1, "b": 2, "c": 3}}, ok=True),
    ):
        r = _artifact_check(row, probe)
    assert r.status == "ok"
    assert "3 value(s)" in r.message
    assert r.detail is not None and r.detail["digest"] == {"a": 1, "b": 2, "c": 3}


def test_no_module_execs_rendered_proposal_content_outside_sandbox() -> None:
    """Repo-wide grep-lock: no module in the reflect/checks/cli surface
    may exec/compile/importlib-load a proposal's rendered ``content``
    outside the sandbox runner. The only allowed exec-site is
    ``self_modify/_artifact_harness.py``, which itself runs INSIDE the
    bwrap wrapper (``self_modify.artifact_runner``).

    Any future check that decides "we'll just quickly import the
    rendered module on the host" reopens the surface the operator
    called out: LLM-authored code executing with the operator's env."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent
    allowed = {
        # The harness's exec IS the sandbox — it runs INSIDE bwrap.
        root / "self_modify" / "_artifact_harness.py",
    }
    banned_patterns = (
        "exec(compile(",
        "importlib.util.spec_from_file_location",
        "importlib.import_module",
    )
    import ast
    offenders: list[tuple[str, str]] = []
    for py in list((root / "self_modify").rglob("*.py")):
        if py in allowed:
            continue
        text = py.read_text(encoding="utf-8")
        # Strip module + function docstrings so security-rationale
        # wording ("host exec()", "exec(compile(content))") doesn't
        # self-trip the grep. We AST-parse and check only executable
        # statements.
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                   ast.Module, ast.ClassDef)):
                if (node.body and isinstance(node.body[0], ast.Expr)
                        and isinstance(node.body[0].value, ast.Constant)
                        and isinstance(node.body[0].value.value, str)):
                    node.body = node.body[1:]  # type: ignore[attr-defined]
        body_src = ast.unparse(tree)
        for pat in banned_patterns:
            if pat in body_src:
                # Discovery uses importlib.import_module for tools/data_feeds
                # discovery — legitimate; already excluded by file name.
                if pat == "importlib.import_module" and py.name == "discovery.py":
                    continue
                offenders.append((str(py.relative_to(root)), pat))
    assert not offenders, (
        "found dynamic module loading outside the sandbox harness: "
        f"{offenders}"
    )


def test_artifact_check_never_execs_content_on_host() -> None:
    """Grep-lock: the artifact check MUST NOT call exec/compile on the
    proposal's content in post_submission_checks. The sandbox harness
    is the only path — the rendered file never touches the host
    interpreter. We split on the docstring so the wording of the
    security rationale doesn't self-trip the grep."""
    import inspect, ast
    from self_modify import post_submission_checks as P
    src = inspect.getsource(P._artifact_check)
    # Strip the docstring — the rationale mentions ``host exec()``.
    tree = ast.parse(src)
    func = tree.body[0]
    if (isinstance(func.body[0], ast.Expr)
            and isinstance(func.body[0].value, ast.Constant)):
        func.body = func.body[1:]
    body_src = ast.unparse(func)
    assert "exec(" not in body_src, (
        f"artifact check must not exec content on host; got:\n{body_src}"
    )
    assert "compile(" not in body_src, (
        f"artifact check must not compile content on host; got:\n{body_src}"
    )


def _rendered_defillama_content(desc: str = "test") -> str:
    """Helper: render the current TOOL_TEMPLATE with a DefiLlama-shape
    spec and return the source. Used by the hermetic escaping test AND
    by the integration positive control."""
    from self_modify import reflect
    from self_modify.digest_path import normalize_digest_fields
    spec = {
        "tool_name": "get_defillama_stablecoins_art",
        "api_base_url": "https://stablecoins.llama.fi",
        "endpoint_path": "/stablecoins",
        "digest_fields": [
            {"name": "total_supply",
             "path": "sum(peggedAssets[*].circulating.peggedUSD)"},
            {"name": "asset_count", "path": "count(peggedAssets[*])"},
        ],
        "description": desc, "rationale": "test",
    }
    entries = normalize_digest_fields(spec["digest_fields"])
    return reflect.TOOL_TEMPLATE.format(
        tool_name=spec["tool_name"],
        class_name=reflect._snake_to_class_name(spec["tool_name"]),
        tool_name_repr=repr(spec["tool_name"]),
        base_url_repr=repr(spec["api_base_url"]),
        endpoint_path_repr=repr(spec["endpoint_path"]),
        digest_fields_repr=repr(entries),
        description_repr=repr(spec["description"]),
        source_label_repr=repr("stablecoins.llama.fi"),
        endpoint_declaration_repr=repr("stablecoins.llama.fi/stablecoins"),
        requires_key_env_repr=repr(None),
        key_in_repr=repr(None), key_param_repr=repr(None),
    )


def test_template_escapes_quotes_triple_quotes_newlines_verbatim() -> None:
    """Escaping proof: a description with quotes, triple quotes, and
    an embedded newline renders a file that (a) compiles and (b)
    embeds the exact string verbatim as the tool's __doc__/
    description. The repr()-based renderer must be the ONLY thing
    that reaches user-supplied text.

    Regression lock: any future ``digest_fields_repr=str(...)`` or
    ``description_repr=f"...{desc}..."`` drift would let a quote
    close a string literal — exactly the surface the operator
    called out."""
    tricky = 'has "double", \'single\', """triple""", and a\nnewline; and \\backslash'
    content = _rendered_defillama_content(desc=tricky)
    # (a) compiles — no unterminated string literals, no syntax error.
    code = compile(content, "<escaping>", "exec")
    # (b) the tricky string is embedded VERBATIM as an object.
    ns: dict[str, Any] = {}
    exec(code, ns, ns)  # NOTE: only in this test — file was built from
    # a locally-controlled spec, not an LLM. Production paths never
    # exec rendered files outside the sandbox harness.
    assert ns["_TOOL_DESCRIPTION"] == tricky, (
        f"description not preserved verbatim: {ns['_TOOL_DESCRIPTION']!r}"
    )


# ---------- real bwrap harness --------------------------------------------
# These tests actually spawn systemd-run + unshare + bwrap + the harness.
# Skipped when the sandbox posture is not OK (host without bwrap or
# systemd-run cannot run them). Runtime: ~2-3s per test.

def _sandbox_ok() -> bool:
    from self_modify import gates
    return bool(gates.sandbox_posture().get("ok"))


@pytest.mark.skipif(not _sandbox_ok(), reason="sandbox (bwrap+unshare+systemd-run) not available")
def test_artifact_integration_ok_on_rendered_defillama() -> None:
    """Real bwrap: render a valid DefiLlama-shape tool, run through
    the sandbox harness, verify total_supply=$165B / asset_count=3."""
    from self_modify.artifact_runner import run_artifact_in_sandbox
    content = _rendered_defillama_content()
    body = {
        "peggedAssets": [
            {"circulating": {"peggedUSD": 120_000_000_000}},
            {"circulating": {"peggedUSD":  40_000_000_000}},
            {"circulating": {"peggedUSD":   5_000_000_000}},
        ]
    }
    r = run_artifact_in_sandbox(
        content, "tools/data_feeds/get_defillama_stablecoins_art.py", body,
    )
    assert r.ok, r
    digest = (r.detail or {}).get("digest", {})
    assert digest["total_supply"] == 165_000_000_000, digest
    assert digest["asset_count"] == 3, digest


@pytest.mark.skipif(not _sandbox_ok(), reason="sandbox (bwrap+unshare+systemd-run) not available")
def test_artifact_integration_canary_dot_env_unreachable() -> None:
    """Confinement proof: a rendered file whose IMPORT tries to read
    ~/Morgoth/morgoth/.env MUST fail inside the harness. The sandbox
    binds only /usr /lib /lib64 /bin /etc /venv and the sandbox tree
    itself — the operator's ~ is not on the mount table, so the .env
    read must raise FileNotFoundError inside the harness (surfaced
    as kind='module_exec' from the JSON verdict)."""
    from self_modify.artifact_runner import run_artifact_in_sandbox
    canary_file = (
        "# CANARY: this file must NEVER be able to read the host's .env.\n"
        "from tools.base_tool import BaseTool\n"
        "with open('/home/corio/Morgoth/morgoth/.env', 'r') as _f:\n"
        "    _leaked = _f.read()\n"
        "class GetCanaryTool(BaseTool):\n"
        "    name = 'get_canary_tool'; is_data_source = True\n"
        "    description = 'canary'; digest_fields = ('x',)\n"
        "    def __init__(self, cfg, client=None): self._client = client\n"
        "    async def execute(self, **_kw):\n"
        "        return {'success': True, 'result': {'x': 1}}\n"
    )
    r = run_artifact_in_sandbox(
        canary_file, "tools/data_feeds/get_canary_tool.py", {"x": 1},
    )
    assert not r.ok, r
    # The harness's module_exec kind reports the import failure.
    assert r.kind == "module_exec", r
    assert (
        "FileNotFoundError" in r.message
        or "No such file" in r.message
        or "PermissionError" in r.message
    ), r


# --------- state-machine lock ---------------------------------------------

def test_checks_incomplete_registered_in_all_statuses() -> None:
    assert P.STATUS_CHECKS_INCOMPLETE == "checks_incomplete"
    assert P.STATUS_CHECKS_INCOMPLETE in P.ALL_STATUSES


def test_amend_cli_registered() -> None:
    """Grep-lock: ``morgoth amend`` MUST be wired so an operator has a
    third path at gate 3 (approve / reject / amend). Amendments live
    in the ``amendments`` jsonb column and are shown by ``morgoth show``.
    Descriptions are invisible to synthesis; units belong in FIELD
    NAMES — this is the surface that gets them there."""
    import pathlib
    from self_modify import cli
    src = pathlib.Path(cli.__file__).read_text(encoding="utf-8")
    assert '"amend"' in src
    assert "_cmd_amend" in src
    # Show renders the amendment history.
    assert 'amendments' in src


def test_amend_records_diff_and_reruns_checks(monkeypatch) -> None:
    """End-to-end (with the store + probe + shadow mocked): an amend
    call must (a) parse the stored spec, (b) apply --rename and
    --description, (c) persist a new content + a JSON amendment row,
    (d) run the post-submission checks. This locks the shape of the
    amendment JSON (ts / who / changes.before / changes.after / note)."""
    import argparse, asyncio, json
    from unittest.mock import AsyncMock, MagicMock, patch
    from self_modify import cli as _cli
    from self_modify import proposals as P
    from self_modify import reflect as _reflect
    from self_modify.digest_path import normalize_digest_fields
    # Render a minimal proposal with two path-digest fields.
    entries = normalize_digest_fields([
        {"name": "open_interest", "path": "result.open_interest"},
        {"name": "volume_24h", "path": "result.stats.volume"},
    ])
    content = _reflect.TOOL_TEMPLATE.format(
        tool_name="get_amend_probe",
        class_name=_reflect._snake_to_class_name("get_amend_probe"),
        tool_name_repr=repr("get_amend_probe"),
        base_url_repr=repr("https://api.example.com"),
        endpoint_path_repr=repr("/probe"),
        digest_fields_repr=repr(entries),
        description_repr=repr("old description"),
        source_label_repr=repr("api.example.com"),
        endpoint_declaration_repr=repr("api.example.com/probe"),
        requires_key_env_repr=repr(None),
        key_in_repr=repr(None), key_param_repr=repr(None),
    )
    row = {
        "proposal_id": "amend-1",
        "target_path": "tools/data_feeds/get_amend_probe.py",
        "content": content,
        "rationale": "old rationale",
        "status": P.STATUS_PENDING_APPROVAL,
        "status_reason": "",
    }
    execute_calls: list[tuple[str, tuple[Any, ...]]] = []
    class _FakePM:
        async def execute(self, q: str, *a: Any) -> None:
            execute_calls.append((q, a))
            # Update the local row so subsequent get() sees new content/rationale
            if "UPDATE self_modify_proposals" in q and "content = $1" in q:
                row["content"] = a[0]; row["rationale"] = a[1]
    store = MagicMock()
    store._pm = _FakePM()
    store.get = AsyncMock(side_effect=lambda pid: row)
    store.update_status = AsyncMock()
    args = argparse.Namespace(
        proposal_id="amend-1",
        rename=["open_interest_usd=open_interest",
                "volume_24h_btc=volume_24h"],
        path=[], description="Deribit BTC-PERPETUAL: open interest in "
              "USD; 24h volume in BTC.",
        rationale=None, note="verified from docs.deribit.com",
        full=False,
    )

    async def _fake_resolve(store_, ident):
        return "amend-1", 0

    async def _fake_probe(url, entries, *, hits=1):
        # Return values matching the amended names so the shadow
        # sampler + artifact check are happy.
        return {
            "url": url, "hits": [{
                "i": 0, "ok": True,
                "vals": {"open_interest_usd": 1.0, "volume_24h_btc": 2.0},
                "body": {"result": {"open_interest": 1.0,
                                    "stats": {"volume": 2.0}}},
            }],
            "n_hits": 1, "gap_secs": 150,
            "digest_fields": ["open_interest_usd", "volume_24h_btc"],
        }

    from self_modify import post_submission_checks as _pc

    async def _fake_checks(**kw: Any) -> Any:
        return [], P.STATUS_PENDING_APPROVAL

    with patch.object(_cli, "_resolve_or_bail", _fake_resolve), \
         patch.object(_cli, "load_config",
                      AsyncMock(return_value=MagicMock())), \
         patch.object(_pc, "run_post_submission_checks", _fake_checks), \
         patch("self_modify.liveness.run_liveness_probe", _fake_probe), \
         patch("self_modify.reflect._registered_digest_fields",
               return_value=set()):
        rc = asyncio.run(_cli._cmd_amend(store, args))
    assert rc == 0
    # The persist call carries: content, rationale, amendment JSON, ...
    upd = [c for c in execute_calls if "UPDATE self_modify_proposals" in c[0]]
    assert upd, "amend must persist content + amendment"
    q, a = upd[0]
    amendment_json = a[2]
    amendment = json.loads(amendment_json)[0]
    assert amendment["who"], amendment
    assert amendment["note"] == "verified from docs.deribit.com"
    changes = amendment["changes"]
    # digest_fields diff: name renamed (path unchanged).
    df = changes["digest_fields"]
    before_names = [e.get("name") if isinstance(e, dict) else e
                    for e in df["before"]]
    after_names = [e["name"] for e in df["after"]]
    assert "open_interest" in before_names
    assert "open_interest_usd" in after_names
    # description diff:
    dd = changes["description"]
    assert dd["before"] == "old description"
    assert "USD" in dd["after"]


def test_recheck_cli_registered() -> None:
    """Grep-lock: the recheck subcommand MUST be wired so operators can
    resurrect a proposal from checks_incomplete without invoking reflect."""
    import pathlib
    from self_modify import cli
    src = pathlib.Path(cli.__file__).read_text(encoding="utf-8")
    assert '"recheck"' in src
    assert "_cmd_recheck" in src
