# Desktop Research Runtime V1

This is a backend contract for a future native ResearchEngineSupervisor. The
existing desktop ManagementSupervisor configures Projects; it does not start
research engines yet.

```text
Desktop ManagementSupervisor → Project selected/configured
Future ResearchEngineSupervisor → scripts.research_engine → engine PAUSED
  → inspect/select Project LLM profile → explicit POST /start → RUNNING
```

## Launch and selection

From the pinned backend checkout, run:

```text
python -m scripts.research_engine --home ABSOLUTE_PRIVATE_HOME --project MANAGED_ID --port PORT
```

The native parent supplies only explicitly trusted environment values. Required
names: `POSTGRES_URL`, `OLLAMA_BASE_URL`, `OLLAMA_PRIMARY_MODEL`,
`OLLAMA_AGENT_MODEL`, `SECRET_KEY`, `MAX_CONCURRENT_AGENTS`,
`LOG_RETENTION_DAYS`, `LOG_LEVEL_THOUGHT`. Optional provider/tool credentials
remain optional. Missing-variable diagnostics contain names only. The launcher
sets `MORGOTH_HOME` and `MORGOTH_PROJECT` before importing `api.server` or
`api.token`, removes `MORGOTH_DOMAIN` and ambient Uvicorn overrides, refuses
`default` and unknown Projects, and binds only `127.0.0.1` on the explicit
validated port. It uses one worker, no reload and no forwarded-proxy headers.
Managed Projects do not load the checkout's `.env` (`core.config.load_config`).
The historical `python main.py` / systemd path remains `0.0.0.0:8000` with
immediate autonomous scheduling, including its existing degraded-awakening
behavior.

## Desktop API authority

```text
Desktop WebView ─X─ direct HTTP
                ─X─ direct WebSocket
Future Rust ResearchEngineClient
  → authenticated native HTTP → Desktop Research API → Brain/Project runtime
```

The Desktop launcher sets its trusted port before Uvicorn starts. The outer
ASGI boundary requires exactly one `X-Morgoth-Token` header on **every**
Desktop HTTP request, including reads, writes, `/docs`, `/redoc` and
`/openapi.json`. It compares against the selected Project's existing token in
constant time, fresh-reading it without creating one for rejected requests.
Query parameters and cookies never authenticate. The `Host` must be exactly
`127.0.0.1:<launcher-port>`; every `Origin` is refused, including `null`,
before historical CORS runs. Uvicorn does not trust forwarded proxy headers.
There is no unauthenticated health exception; native readiness uses the token.

Desktop V1 rejects all WebSockets, including historical `/ws/chat`, with
policy code 1008 before its handler runs. The WebView never receives the token.
Later Rust code may open an authenticated native WebSocket and translate
events to Tauri; this contract does not implement that bridge.

`python main.py` keeps its previous public-local reads/chat, historical local
CORS and WebSocket behavior. Existing proposal mutation guards remain active
there, and continue as defense in depth in Desktop mode. Desktop HTTP access
is therefore native-token-only; loopback binding alone is not the authority.

## Initialization, readiness and control

The desktop lifespan initializes Project storage and its campaigns table,
scheduler, episodic memory and awakening before serving requests. It then
creates the Project UI token at `Project.runtime_dir/auth/ui_token`. The future
Rust supervisor may read that private file natively; it must never enter the
WebView. A healthy process is not equivalent to research readiness.

PAUSED means no autonomous-cycle task exists. It is **not** network-silent:
the existing awakening can probe Ollama, call its local warmup, self-test tools
and dependencies, and initialize storage. `NOT_READY` means awakening did not
pass. `FAILED` records unexpected autonomous-task termination. The status
separately reports initialized, awakening readiness and task liveness.

All four `/api/runtime/v1` routes require the existing `X-Morgoth-Token` gate:

| Method/path | Result |
|---|---|
| `GET /status` | Project, Domain, code SHA, initialization, awakening, research and current-profile readiness |
| `GET /profiles` | Effective provider map and canonical readiness for legacy/claude/codex |
| `POST /profile` `{"profile":"claude"}` | Canonical atomic guarded Project profile switch while paused |
| `POST /start` | Start the one owned autonomous task only after dependency and effective-profile readiness |

The legacy profile evaluates its actual effective task routes. Codex remains
BLOCKED while `SAFE_FOR_WORKLOADS=False`; a new Project in legacy mode cannot
start if those routes reach Codex. Claude is never auto-selected. Profile
selection retains the existing campaign-live guard and must happen after
storage initialization. Profile mutation and start share a process-local async
lock, so a start sees one completed profile selection. Duplicate/concurrent
starts return conflict rather than creating another loop. Normal shutdown
cancels and awaits the owned task before closing resources; there is no Python
process supervisor or auto-restart loop.

## Proof and limits

Hermetic tests use a controlled loop and synthetic readiness; SQL integrations
use only `morgoth_test` and disposable managed schemas. No real provider or
market/weather call is made for this contract. A fully live Brain awakening
and its external dependencies remain for the later Rust-supervised smoke;
PAUSED must not be misrepresented as an inference-free initialization.
