"""CORS regex — local-only, port-agnostic, anchored.

The 3000-squatter-CORS chain: a residual node process holds :3000 →
Next auto-shifts to :3001 → backend allowlist knows only :3000 →
every browser fetch blocked. Fix: accept any localhost/127.0.0.1
origin regardless of port.

Contract:
  - localhost:<any port>       → allowed (ACAO echoes origin)
  - 127.0.0.1:<any port>       → allowed
  - <non-local host>           → no ACAO header (browser blocks)
  - https://localhost.evil.com → no ACAO (regex anchoring — the dot
                                 after localhost makes it NOT match)
"""
from __future__ import annotations

import re

from starlette.testclient import TestClient
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse
from starlette.routing import Route

from api import server as api_server


# ---------- regex form ---------------------------------------------------

def test_regex_matches_localhost_and_127() -> None:
    """Anchored regex accepts every operator-chosen local port."""
    rx = re.compile(api_server._LOCAL_ORIGIN_REGEX)
    for allowed in (
        "http://localhost:3000",
        "http://localhost:3001",
        "http://localhost:5173",
        "http://localhost:8080",
        "http://127.0.0.1:8081",
        "https://localhost:3000",
        "http://localhost",           # no port allowed too
        "http://127.0.0.1",
    ):
        assert rx.match(allowed), allowed


def test_regex_rejects_non_local_and_lookalikes() -> None:
    rx = re.compile(api_server._LOCAL_ORIGIN_REGEX)
    for blocked in (
        "http://evil.com",
        "http://evil.com:3000",
        "https://localhost.evil.com",     # anchoring — dot after localhost
        "http://localhost.evil.com:3000",
        "http://mylocalhost:3000",         # substring host
        "http://127.0.0.1.evil.com",
        "http://[::1]:3000",               # IPv6 not in the allowlist
        "file://local/file",
        "://localhost:3000",               # no scheme
    ):
        assert not rx.match(blocked), blocked


# ---------- end-to-end via a tiny app that reuses the same middleware ----

def _mk_client() -> TestClient:
    """Build a minimal Starlette app with the SAME CORS middleware
    the production server uses. Uses the exported regex constant so
    a future edit that changes the constant propagates here."""
    async def _hello(request):  # noqa: ANN001
        return JSONResponse({"ok": True})

    app = Starlette(
        routes=[Route("/hello", _hello)],
        middleware=[Middleware(
            CORSMiddleware,
            allow_origin_regex=api_server._LOCAL_ORIGIN_REGEX,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )],
    )
    return TestClient(app)


def _acao(resp) -> str | None:  # noqa: ANN001
    return resp.headers.get("access-control-allow-origin")


def test_localhost_3000_allowed() -> None:
    c = _mk_client()
    r = c.get("/hello", headers={"origin": "http://localhost:3000"})
    assert r.status_code == 200
    assert _acao(r) == "http://localhost:3000"


def test_localhost_3001_allowed() -> None:
    """The exact regression the CORS chain caused — Next shifted to
    3001 and the old static allowlist rejected the origin."""
    c = _mk_client()
    r = c.get("/hello", headers={"origin": "http://localhost:3001"})
    assert r.status_code == 200
    assert _acao(r) == "http://localhost:3001"


def test_localhost_5173_allowed() -> None:
    """Vite dashboards / alt dev servers land on 5173 by default."""
    c = _mk_client()
    r = c.get("/hello", headers={"origin": "http://localhost:5173"})
    assert r.status_code == 200
    assert _acao(r) == "http://localhost:5173"


def test_127_8081_allowed() -> None:
    c = _mk_client()
    r = c.get("/hello", headers={"origin": "http://127.0.0.1:8081"})
    assert r.status_code == 200
    assert _acao(r) == "http://127.0.0.1:8081"


def test_evil_com_blocked_no_acao() -> None:
    c = _mk_client()
    r = c.get("/hello", headers={"origin": "http://evil.com"})
    # The endpoint returns 200 (CORS doesn't stop the response body
    # server-side), but the ACAO header must be absent so the browser
    # blocks the response.
    assert r.status_code == 200
    assert _acao(r) is None


def test_localhost_subdomain_impersonation_blocked() -> None:
    """Regex anchoring — ``https://localhost.evil.com`` is a distinct
    origin and must be rejected. If ``localhost`` were unanchored, a
    subdomain-shaped host could smuggle credentials."""
    c = _mk_client()
    r = c.get("/hello", headers={"origin": "https://localhost.evil.com"})
    assert _acao(r) is None


def test_allow_credentials_stays_enabled() -> None:
    """Regex approach preserves credential mode. The wiki reader
    uses cookie-authenticated fetches; downgrading to allow_origins=['*']
    would silently break auth (spec incompat)."""
    c = _mk_client()
    r = c.options(
        "/hello",
        headers={
            "origin": "http://localhost:3001",
            "access-control-request-method": "GET",
        },
    )
    assert r.headers.get("access-control-allow-credentials") == "true"


def test_production_server_still_wired_to_local_regex() -> None:
    """The api.server module must still use the local-origin regex,
    not fall back to a static allowlist. Grep-level."""
    import inspect
    src = inspect.getsource(api_server)
    assert "_LOCAL_ORIGIN_REGEX" in src
    assert "allow_origin_regex=_LOCAL_ORIGIN_REGEX" in src
