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

ProjectManager creation publishes only configuration and owned directories;
it does not provision PostgreSQL. The first desktop engine initialization
creates or migrates the selected Project's runtime schema through
`PersistentMemory.initialize()`, including the base `objectives` table and its
current cycle, source, outage, campaign and activity columns. It never resolves
a missing managed table through `public`. `scripts.init_db` remains an optional
explicit maintenance/bootstrap command and is not a prerequisite for Desktop
research or human objective creation after PAUSED. Its additional `ui_widgets`
table is UI-facing; `api_leads` is also ensured by Scout when needed.

The desktop lifespan initializes Project storage and its campaigns table,
scheduler, episodic memory and a safe local preflight before serving requests.
It then
creates the Project UI token at `Project.runtime_dir/auth/ui_token`. The future
Rust supervisor may read that private file natively; it must never enter the
WebView. A healthy process is not equivalent to research readiness.

Desktop PAUSED means initialized storage and validated active Domain rail,
local Ollama `/api/tags` reachable with both configured models present, and
**no** autonomous task. Its empty owned workspace needs no `SPEC.md`.
Initialization performs no research-tool execution, LLM inference, notification,
tool-test file write, objective recovery or recurring-task creation. The local
read-only Ollama check is bounded to five seconds; it does not verify external
Domain sources or provider login/quota. `NOT_READY` remains inspectable when
the local preflight fails. The existing `awakening_ready` API field carries
this safe managed-runtime readiness in Desktop mode. `FAILED` records unexpected
autonomous-task termination. The status separately reports initialized,
awakening readiness and task liveness.

Legacy `python main.py` retains its historical full awakening, Ollama warmup,
live tool self-tests, stale/orphan recovery, recurring-task setup and immediate
autonomous scheduling. Desktop requires explicit qualified profile selection
and `POST /start`; only then does it recover objectives, ensure the Domain's
recurring task exactly once, and schedule the autonomous loop. Research-tool
and source-network activity begins after this explicit START.

Before importing the server or publishing a token, the Desktop launcher obtains
`<Project.runtime_dir>/.research-engine.lock` with a nonblocking exclusive
Linux `flock` on a private regular file. A second launcher for the same Project
exits `PROJECT_ENGINE_ALREADY_ACTIVE`; another Project has a separate lease.
The historical `main.py` entrypoint also requires this lease if explicitly
pointed at a managed Project; its default legacy Project remains unchanged.
The research ASGI lifespan refuses a managed Project if no launcher has passed
it a lease currently held by that process, so a direct `uvicorn api.server:app`
invocation cannot initialize managed research without the lease.
The descriptor remains open for the engine process lifetime. An old file is
harmless after a crash because the kernel lock, not a PID or file existence,
is authoritative. This is a host-local Linux lease, not a distributed lock
across machines or NFS.

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

## Objective completion and cadence

An autonomous work cycle first commits its full `cycle_payload` to the active
Project's objective row. If the model's successful `update_objective(done)`
completed that objective, Brain then reloads the committed row and runs the
same finalizer used by MAX_CYCLES. The model's human-readable evidence summary
is preserved. The finalizer assembles persisted current findings, synthesizes
when at least two distinct source tools were used, extracts theses, applies the
existing numeric-fidelity and field-confusion checks, persists accepted theses
or the existing empty-extraction abstention event, and checks contradictions.
Historical-only evidence records an abstention in the feed. A failed auxiliary
Chroma write cannot replace or block the committed PostgreSQL evidence. If the
`cycle_payload` write fails, synthesis is deferred and an error is surfaced;
transient tool data is never treated as committed evidence.

MAX_CYCLES uses the same finalizer on payloads from earlier work cycles and
then takes the normal cancellable inter-cycle sleep. Model completion likewise
sleeps before another claim. An empty-queue `create_objective` success remains
the intentional exception: it chains immediately into work on the new row.

The objective prompt's achievable minimum is
`min(MIN_DISTINCT_SOURCES, len(DATA_SOURCE_TOOLS))`. Crypto retains its
three-source minimum; Weather has two measurement sources (`get_weather_forecast_met` and
`get_nws_weather_observation`). Station discovery is a lookup, not a third
measurement source. A zero-source Domain has a zero prompt minimum and never
demands a nonexistent source; the separate synthesis rule still requires two.

## Proof and limits

Hermetic tests use a controlled loop and synthetic readiness; SQL integrations
use only `morgoth_test` and disposable managed schemas. No real provider or
market/weather call is made for this contract. PAUSED is inference-free but
may contact the configured local Ollama `/api/tags` endpoint.
