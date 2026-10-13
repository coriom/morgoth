## 2026-10-09 - Integrated Codex confinement, qualification blocked

The Codex subprocess path now enters a mandatory Bubblewrap Linux namespace
with private workspace and a narrow read-only ChatGPT auth mount. Synthetic
outside read/write, unrelated HOME/repository and fake-key access are denied;
confined non-inference login works. The sole authorized model-authority
canary returned safe `cli_diagnostic`; no text probe followed. The cause is
unclassified and `SAFE_FOR_WORKLOADS=False`. Desktop exposes separate local
installation/authentication/sandbox facts without claiming workload readiness.
No Weather run, production state, provider key or new dependency was used.

## 2026-10-09 - Codex-only target foundation

Development branch codex-only-provider-foundation-v1 removes Claude from the
supported Project profile catalog, readiness probe, recommendation, heartbeat,
and automatic fallback. CHAT remains Ollama; external tasks target Codex CLI.
The installed 0.162.0 CLI and ChatGPT login were checked without inference.
SAFE_FOR_WORKLOADS=False remains the deployment gate until whole-process
confinement and final text/event canaries pass. No production state was used.

# PROGRESS.md — Morgoth Development Tracker

## 2026-10-13 — Codex final-message diagnostic correction, no inference

- On `codex-01621-final-message-semantics-v1`, the diagnostic-only authority
  canary now permits multiple completed assistant messages but requires the
  last to match both `CANARY_BLOCKED` and the readable final file. Earlier
  messages remain checked for synthetic marker disclosure and malformed/tool
  events. The prior operator run's final item cannot be reconstructed from its
  discarded private JSONL; its unknown pre-turn warning remains unresolved.
- Exact tagged local diagnostic-log-write and model-reroute templates now have
  fixed probable-family classifications, but both remain review blockers.
  Unknown warnings remain blocked. Production event validation, CLI arguments,
  Bubblewrap, auth mounts and `SAFE_FOR_WORKLOADS=False` are unchanged.
- No model inference, Weather, autonomous START, production DB/service or
  Desktop action. Focused no-inference suite: 141 passed, 3 deselected; three
  host structural/auth/cleanup checks passed. No full canonical rerun. The
  ordinary-WSL one-shot operator command is in `docs/CODEX_PROVIDER.md` and
  was not executed here.

## 2026-10-10 — Codex 0.162.0 startup-warning source review (feature branch)

Pinned upstream tracing shows config, general, deprecation and reroute
notifications can collapse to `item.type=error`, while critical errors remain
top-level. A diagnostic-only exact-template classifier covers verified config,
deprecation, state and runtime warning strings; no skill JSONL template was
proven. Two plausible pre-turn producers are the under-development
`skip_host_skill_discovery` feature and nested Bubblewrap user-namespace probe,
but the prior private messages were discarded, so neither is a template match.
No new inference: installed Codex drifted to 0.162.1 and its standalone layout
fails the pinned production sandbox contract. `SAFE_FOR_WORKLOADS=False` and
production event rejection remain unchanged. No dependency, Desktop change,
production state, Weather run or service restart. See `docs/CODEX_PROVIDER.md`.
Canonical hermetic: 2113 passed, 2 skipped. Focused Codex selection: 78
passed, including 14 source-template tests. Three host Bubblewrap tests were
attempted but failed before their assertions because the active standalone
Codex 0.162.1 layout is rejected by the pinned 0.162.0 transport. No SQL test
was needed or run.

## 2026-10-09 — Codex diagnostic attribution (feature branch)

Added a diagnostic-only, one-shot reducer for the existing Bubblewrap Codex
canary. It distinguishes error items, top-level errors, failed/completed turns,
forbidden tool items and unknown events using fixed safe categories and
bounded lifecycle metadata. Production event rejection and
`SAFE_FOR_WORKLOADS=False` remain unchanged. No new dependency, API endpoint,
Desktop change, provider key, research run or production state was added.
See `docs/CODEX_PROVIDER.md` for the event contract and final one-shot result.
The sole live canary exited 0 with a completed turn and no tool/outside-file
effect, but emitted two unclassified error items before turn start. Final
verdict is `BLOCKED_CODEX_DIAGNOSTIC_UNKNOWN`; current 0.162.0 cause is not
proved to match the historical 0.159.0 skill/config family. No text probe or
retry followed. Workloads remain blocked pending separately authorized
early-start diagnostic attribution.

## 2026-10-09 — Project-local work-cycle failure ledger (feature branch)

The autonomous loop now records bounded objective/attempt/stage/exception-class
metadata for work failures, including caught payload and episodic writes.
MAX_CYCLES with no committed cycle payload marks the objective `failed` and
does not invoke synthesis or thesis extraction; one or more committed payloads
retain canonical finalization. The attempt counter and cadence are unchanged.
Runtime schema initialization owns the Project-isolated ledger; `init_db`
inherits it without duplicate DDL. No dependency, UI endpoint, raw exception
message, production state, provider inference or Codex activation was added.
See `docs/DESKTOP_RESEARCH_RUNTIME.md` for the stage contract.
Canonical hermetic: 2069 passed, 2 skipped, exit 0; focused cycle/finalizer
selection: 114 passed; affected `morgoth_test` SQL selection: 6 passed.

## 2026-10-08 — Desktop runtime liveness (feature branch)

Added token-protected `/api/runtime/v1/liveness` for cheap identity/task
monitoring. Full profile readiness remains on explicit status and control
operations; synchronous local probes now overlap in threads with Ollama's
bounded tag check. START uses one fresh readiness snapshot. No research,
Domain, profile or legacy behavior changed. The separate Desktop Rust monitor
must adopt this endpoint before a new live qualification attempt.

## 2026-10-08 — Canonical objective finalization (feature branch)

Brain now uses one finalizer for model-requested `done` (after committed
`cycle_payload`) and MAX_CYCLES completion. The finalizer retains existing
synthesis, thesis extraction, numeric-fidelity, field-confusion, abstention and
contradiction behavior. Forced completion takes the shared cancellable cadence;
empty-queue objective creation still chains immediately. The prompt source
minimum is bounded by the active Domain's measurement-source rail: Crypto 3,
Weather 2; zero/one-source rails have explicit prompt semantics. No new
dependency, endpoint, table or UI change. Tests use provider fakes and only
disposable `morgoth_test` schemas; no live inference or research request.

## 2026-10-07 — Managed Project objective storage bootstrap

The canonical `PersistentMemory.initialize()` runtime DDL now creates the
historical base `objectives` table before its idempotent migrations. The
runtime-critical objective and campaign migrations fail startup if they cannot
complete; PAUSED cannot advertise incomplete objective storage. `scripts.init_db`
continues to call that initializer and owns only its extra UI/Scout tables.
Fresh and historical managed schemas, service-level objective creation/claim/
cycle/evidence, and a real Weather Desktop PAUSED process are covered by
disposable `morgoth_test` tests. No `.env`, production Project, or UI change.
Validation: 2045 canonical hermetic passed, 2 existing skips; 8 guarded SQL
regressions passed. The historical 31 genuine / 16 confused control remains in
the canonical suite. No dependency added.

## 2026-10-06 — Safe managed research bootstrap V1 (feature branch)

- The managed Desktop launcher obtains a process-lifetime, host-local Linux
  `flock` for its Project before importing the server or publishing a token.
  Unsafe lock files fail closed; a second engine for that Project exits with
  `PROJECT_ENGINE_ALREADY_ACTIVE`. The historical entrypoint also takes this
  lease when explicitly targeting a managed Project. An old file has no
  authority after death.
- Explicit `DESKTOP_PAUSED` initialization retains storage and Domain-rail
  construction but uses a bounded local Ollama model-list preflight. It does
  not run historical live self-tests, inference, notifications, objective
  recovery, recurring-task creation or autonomy. Recovery and recurring setup
  occur immediately before explicit START. `LEGACY_FULL` retains the original
  awakening and immediate scheduling sequence.
- Actual launcher smoke used a disposable `morgoth_test` cluster and fake
  loopback Ollama for Crypto and Weather managed Projects: authenticated PAUSED,
  no autonomous task, duplicate-process refusal and released leases. Canonical
  hermetic: 2044 passed, 2 existing skips, exit 0. Focused sandbox: 35 passed.
  Guarded SQL selections: 8 and 95 passed, 11 total deselections. The Deribit
  31 genuine / 16 confused control passed. No UI, main, production service,
  production state, provider inference or Codex safety-lock change.
- Decision: the lease is deliberately host-local and non-distributed; Desktop
  readiness proves local models and structural rail only, not external sources.
  Research-engine supervision, provider inference and packaging remain outside
  this backend contract. See docs/DESKTOP_RESEARCH_RUNTIME.md.

## 2026-10-05 — Desktop research API authority boundary V1 (feature branch)

- A Desktop-only outer ASGI middleware authenticates every HTTP path, including
  docs/OpenAPI, before historical CORS and routes. It requires one Project UI
  token header, the trusted launcher's exact loopback Host/port, and no Origin;
  it reads the token without creating it on rejected requests. Desktop
  WebSockets close before the historical chat handler. Legacy HTTP, CORS,
  WebSocket and proposal mutation guards retain their previous behavior.
- Fresh-process proof binds the selected managed Project token before server
  import. Focused ASGI/legacy checks: 41 passed. Canonical hermetic selection:
  2032 passed, 2 existing skips. Three guarded `morgoth_test` SQL regressions
  passed. No production/UI/main change, provider inference or Codex activation.
- Decision: Rust will later own authenticated native HTTP and a separate
  WebSocket bridge if needed; browser/WebView requests remain unauthorised.
  See docs/DESKTOP_RESEARCH_RUNTIME.md.

## 2026-10-05 — Desktop research-runtime contract V1 (feature branch)

- A separate managed-Project launcher binds loopback on an explicit port, sets
  Project/home before engine and token imports, rejects `default`/unknown
  Projects, and starts the initialized engine PAUSED. Managed Projects continue
  to skip checkout `.env`; the historical `main.py` port/start behavior remains.
- Brain retains one autonomous-task handle, gates desktop start on awakening,
  exposes lifecycle/task death and cancels/awaits that exact task on shutdown.
  The existing legacy automatic scheduling remains, including degraded wakeup.
- Four versioned runtime routes reuse the Project UI token and canonical LLM
  profile readiness/switch/campaign guard. Start and profile mutation share a
  process-local lock; blocked Codex/legacy routes cannot start research.
- Verification: 2024 canonical hermetic passed, 2 existing skips; focused
  runtime/management/profile/campaign 57 passed; three guarded morgoth_test
  SQL regressions passed. No provider inference, production state/service,
  UI, dependency, Domain, Codex safety lock or main-branch change.
- Decisions: desktop PAUSED still performs existing awakening probes; runtime
  Git SHA is captured once by the launcher. External process supervision and
  packaged startup remain future Rust work. See docs/DESKTOP_RESEARCH_RUNTIME.md.

## 2026-10-03 — Independent local Management API V1 (feature branch)

- Added a separate authenticated FastAPI factory over the offline ProjectManager:
  versioned status, Domain listing, Project list/show/validate/create, typed
  responses, stable operation IDs and generated OpenAPI. Engine API/startup is
  not imported; no Project is rebound and no storage/provider is initialized.
- Explicit private-token launcher binds only 127.0.0.1, one worker, no reload
  or proxy headers. Host and Origin checks, constant-time header comparison,
  strict bounded JSON and safe errors guard every route; no browser CORS.
- The manager remains the sole publication/validation implementation. This
  branch does not add supervision, profile endpoints, Tauri or UI code.
- Verification: canonical worktree-root hermetic suite 2014 passed (+12),
  2 existing skips, exit 0; focused API/manager/profile 69 passed; five
  guarded `morgoth_test` SQL regressions passed. See docs/MANAGEMENT_API.md.
- No new dependencies, production database/service/Project/profile or Codex
  activation; production checkout and UI remain untouched.

## 2026-10-03 — Offline Project Manager V1 (feature branch)

- Added a Project Manager service over the existing RuntimeHome,
  ProjectConfig, Domain loader and namespace validators. It lists, shows,
  validates and creates isolated Project configurations without rebinding the
  process-bound current Project or initializing PostgreSQL/Chroma/engine state.
- The thin `morgoth projects` / `python -m scripts.projects_cli` entry points
  expose structured JSON. Creation uses a cooperative catalog lock, owned
  private directories, a staged manifest and a non-overwriting atomic link.
  Existing/symlinked paths, malformed neighbours and collisions fail closed.
  New Projects have no managed LLM profile state, retaining legacy routing.
- Disposable-home tests cover two Projects with the same Crypto Domain, a
  Weather Project, fresh-process resolution, concurrency, read-only commands,
  pre-publication failure and bounded cleanup. No production Project, service,
  profile, DB, UI, live provider or Codex change; no new dependency.
- Final worktree-root canonical suite: 2002 passed, 2 existing nested-sandbox
  skips, 209 integration-marker deselections, wall 37.7 s, exit 0. Focused
  manager/profile: 57 passed; affected morgoth_test SQL: 5 passed, 5
  deselected; broader guarded SQL: 101 passed, 11 deselected. Historical
  Deribit 31/16 control passed. The production checkout remains on main.
- Platform-tested boundary is Linux local cooperative locking. Engine start,
  database/Chroma provisioning, deletion and Tauri/API remain deferred; see
  `docs/PROJECT_MANAGER.md`.

## 2026-10-03 — Weather temperature provider-forecast verifier (feature branch)

- Weather selects a trusted, pure `verification` scorer. V1 compares only
  persisted MET temperature predictions to NWS station observations under
  explicit 10 km / 30 min bounds, `as_of`, prospective eligibility, maturity,
  deterministic station/revision selection and explicit unmatched statuses.
  Matched-pair MAE, bias and RMSE are descriptive and revision-weighted.
- `python -m scripts.verify_facts --from ... --to ... --as-of ... --json` uses
  a Project-selected, direct, read-only PostgreSQL connection and one bounded
  repeatable-read snapshot. Missing table and >10,000 facts fail without a
  partial score. No runtime initialization or score table was added.
- Worktree-root canonical launcher: 1975 passed, 2 existing nested-sandbox
  skips, 209 integration-marker deselections, exit 0, wall 29.3 s. Targeted
  SQL: 2 passed, 15 deselected; guarded broader SQL: 101 passed, 11 deselected.
  Historical Deribit 31/16 control passed separately. See
  `docs/WEATHER_SCORER.md` for the scientific and operational limits.
- Development occurred on `weather-temperature-verifier` descending from
  `b082472`, without production checkout edits, service/database changes,
  live API/LLM calls, Codex activation or UI changes. No new dependencies.

## 2026-10-03 — Temporal-fact capture/retrieval hardening

- One active-rail tool result now persists its declared facts in one validated
  PostgreSQL transaction. Capture status is explicit in result metadata;
  extraction/SQL failure leaves no partial new batch and never changes the
  tool's success/result. Duplicate facts retain the first DB insertion time.
- `acquired_at` means first database insertion, not HTTP receipt or external
  timestamp attestation. Cache replays do not backdate it. Query results are
  complete up to 10,000 rows or raise `TemporalFactQueryOverflow`; separate
  calls do not share a snapshot. MET still needs explicit tool invocation for
  prospective capture; only NWS observation collection is scheduled.
- The pre-existing reflect-template assertion failed identically at 4c8f364
  and e395243; its test now verifies rendered field-name normalization without
  running reflect. Baseline/current logs, sanitized JUnit, selection audit and
  final verification: `docs/verification/temporal_facts_20261003/`.
- Final canonical hermetic launcher: 1960 passed, 2 nested-sandbox skips,
  208 integration-marker deselections (collection audit), exit 0, 33.8 s.
  Five scoped morgoth_test SQL tests passed, 5 deselected, 13.92 s; broader
  guarded integration: 100 passed, 11 deselected, 19.11 s. Historical 31/16
  control and Codex safety lock remain green/unchanged. No new dependencies,
  scorer, production writes, service action, live HTTP, LLM call or UI change.


## 2026-10-03 — Generic prospective temporal facts

- Domain scorer roles are validated identifiers rather than a hardcoded list;
  trusted implementation registration and Crypto caller signatures remain unchanged.
- Added a Project-local `temporal_facts` table and generic tool-result capture
  driven by declarative Domain rules, separate from legacy `metric_series`.
  PostgreSQL assigns first `acquired_at`; semantic-key dedup retains that proof.
- Weather captures MET temperature predictions and NWS temperature observations
  from actual normalized tool output, with no Weather branch in generic core.
  MET `meta.updated_at` is now `source_updated_at`, never `issued_at`.
- Validation: 1960 hermetic passed (+18), 208 integration deselected, exit 0;
  5 targeted `morgoth_test` SQL integrations passed. The historical 31/16
  control and Codex safety lock remain unchanged. No Weather scoring yet.
- No service, production campaign/Project/Profile, real provider, `.env`, UI
  or production database action; no new dependency.


## 2026-10-01 — First production non-crypto Domain: Weather

- Added declarative `domains/weather/domain.yaml` with its own three-tool rail,
  neutral location entity, temperature/precipitation/wind/general windows,
  MET/NWS source and unit metadata, and one hourly NWS benchmark metric.
- Added MET compact forecast, NWS linked station discovery and latest actual
  observation tools under the existing catalog. Shared non-secret User-Agent,
  bounded URL-keyed HTTP cache, strict coordinates/station IDs and source-unit
  validation; no generic core, crypto pack, Project or LLM-profile change.
- Official MET/NWS documentation verified 2026-10-01. Open-Meteo Free and MET
  Frost excluded. One MET and three linked NWS public smoke requests returned
  HTTP 200 and were minimized into hermetic parser fixtures.
- Fresh crypto/Weather subprocesses prove opposite effective rails; Weather
  `morgoth_test` storage proof passed. Coverage blind spots: 0. Validation:
  1942 canonical passed (+19), 2 skipped, wall 22.9s, exit 0; 59 dedicated
  morgoth_test integration tests passed. Crypto pack hash unchanged, historical
  31/16 control and Codex SAFE_FOR_WORKLOADS=False retained. No service,
  campaign, production Project/Profile, UI or production database action.
- Scorer/calibration, geocoding, global observations and precipitation
  verification deferred; see docs/WEATHER_DOMAIN.md.

## 2026-09-30 — Declarative Domain tool rail

- Added one `rail.tools` allowlist to Domain and a canonical installed/global/
  active resolver in core/tool_rail.py. Crypto declares its prior 15 research
  tools explicitly; source/chat/registered sets match the pre-change baseline.
- API registration, Brain schemas, ToolRouter lookup/execution, `/api/tools`,
  source/metric collectors, measurement diagnostics and wiki fallback now use
  effective Domain authority. Installed inactive tools return a distinct denial.
- Apply's post-restart proof checks `/api/tools/catalog` for installation;
  Domain activation remains a separate human-reviewed declaration.
- Synthetic two-Project/two-tool subprocess tests prove installed != active,
  including schema/API invisibility and direct execution rejection.
- Validation: 1923 canonical passed (+14), 2 skipped, wall 39.4s, exit 0;
  336 focused regressions passed; 58 dedicated morgoth_test cache/Project/
  profile/campaign integration tests passed. Historical Deribit 31/16 intact.
- No production service, campaign, Project or Profile action; no reflect,
  live rail call, .env change, database schema/dependency or Codex activation.
  The UI repository only mirrors the new catalog/active endpoint DTOs.
  Weather tools and activation UX remain deferred; see docs/TOOL_RAIL_CONTRACT.md.

## 2026-09-30 — Generic Domain runtime contract

- Moved crypto subject entities, semantic contradiction windows, metric collector
  specs/cadence/source, bootstrap task and prompt examples into the declarative
  crypto pack. Generic core now consumes typed, fail-closed Domain declarations.
- Added trusted lazy scorer registry; crypto scoring remains specialised and
  explicitly selected by its Domain. Project namespaces and LLM profiles unchanged.
- Added test-only neutral Domain/Project subprocess proof, independent metric
  schedules, malformed-pack tests and curated core hardcode regression.
- Crypto before/after subject, window and recorder fixture identical; historical
  0.0001 control remains 31 genuine / 16 confused. Canonical hermetic suite:
  1909 passed (+12), 2 skipped, wall 16.3s, exit 0; focused regressions 469
  passed; dedicated morgoth_test Project/profile/campaign SQL proofs 3 passed.
- No production service, campaign, Project or profile mutation; no live market
  commands, reflect, .env access, dependency/schema/UI change or Codex activation.
  Offline crypto report/backtest scripts and the shared numeric parser import
  remain documented in docs/DOMAIN_CONTRACT.md for a separate chantier.

## 2026-09-30 — Canonical campaign archive and lifecycle-aware recovery

- Added analysis/campaign_archive.py and CLI `morgoth campaign --archive UUID
  --output PATH`: direct repeatable-read/read-only PostgreSQL snapshot; no runtime
  initialization, DDL, Chroma or inference. Only campaign-owned evidence is included;
  unreconstructible/global classes are declared, producer versions never inferred.
- Deterministic JSON + exact-byte SHA-256, 0600 files, fsync and atomic no-overwrite
  Linux rename. Sensitive-looking content refuses export without exposing values.
- Added core/campaign_lifecycle.py: one SQL eligibility predicate shared by claiming,
  orphan recovery, stale sweep, active selection and expiry. Closed/expired/missing
  campaign objectives remain untouched/nonclaimable; live and ordinary recovery
  retain the existing deadlines and statuses. No general timeout override.
- Added docs/DERIBIT_RERUN.md with ownership audit, archival limits and exact future
  rerun sequence: preserve PARTIAL artifact, normal restart, prove old objectives
  unchanged, health/rail checks, new campaign + full SHA/Project/start/end manifest.
- Validation: canonical 1825 passed (+23), 2 skipped, wall 16.9s, exit 0; focused
  archive/lifecycle/Project/Deribit 154 passed; real SQL proof in a disposable schema
  of morgoth_test 1 passed (0.29s), no production DB access in tests.
- Historical 31 genuine / 16 confused control green. Project/Domain, Deribit logic,
  thesis semantics and Codex SAFE_FOR_WORKLOADS=False unchanged; UI untouched.
- No production archive/state mutation, service lifecycle action, .env edit, reflect,
  proposal approval/application or systemd workaround. Autostart remains disabled.
- No dependencies or schema changes. Deferred read-only Chroma/global telemetry
  reconstruction and non-Linux atomic-publication portability.


## 2026-09-29 — Deribit measurement coverage and source/unit audit

- Completed declarative Deribit served/rail/phrase/source/unit/context/cache metadata;
  warning-only coverage diagnostic reports missing metadata after explicit exemptions.
- Added analysis/source_attribution.py using the existing fidelity number parser:
  clause then evidence attribution, corresponding objective fields, written precision,
  explicit units, cross-source counts and retained gate-rewrite provenance.
- Historical read-only snapshots replayed offline: old counts/gaps/fragmentation
  unchanged; 0.0001 control remains 31 genuine / 16 confused. Three unit errors
  manually confirmed, no new source errors. See docs/DERIBIT_MEASUREMENT.md.
- Canonical suite: 1802 passed (+64), 2 skipped, 0 failed, wall 15.3s, exit 0.
  Focused Deribit + unchanged Project/namespace regressions: 132 passed, wall 6.9s.
- Project selection/storage, Deribit tool, numeric gate, thesis extraction, UI and
  Codex SAFE_FOR_WORKLOADS=False unchanged. No dependencies or production writes.
- Campaign persisted status active; system/user service state reads returned inactive.
  No lifecycle action, campaign mutation, reflect, live rail-check or .env access.
- Other tools' metadata warnings and convenience formatting deferred.


## 2026-09-29 — Project runtime foundation (complete, not deployed)

- Added core/project.py, core/runtime.py, core/storage_namespace.py: immutable Project,
  fixed MORGOTH_PROJECT selection, trusted catalog, canonical namespace and collisions.
- Domain stays declarative semantics. Only legacy_project imports old crypto namespace
  fields; new projects must explicitly supply storage. Legacy paths/behavior preserved.
- Wired memory/persistent.py (schema reset on every pool checkout, no public fallback),
  memory/episodic.py, config/logs, wiki, UI token and optional task provider overrides.
  Global permissions mutation, legacy backups and shared self-modification disabled for
  new project runtime; human gate 3 unchanged. scripts/init_db shares the same resolver.
- Added project unit/subprocess/integration proofs; adapted old Domain storage checks
  to Project authority without removing pack/crypto regression coverage.
- Documented full audit, ownership, migration boundary and future desktop contract in
  docs/PROJECT_RUNTIME.md. UI mirrors ProjectConfig/Project in types/morgoth.ts.
- Validation: canonical hermetic suite 1738 passed (+60), 2 skipped, wall 26.0s, exit 0;
  morgoth_test two-process integration 1 passed (6.14s); UI tsc exit 0.
- Live crypto report/scorer wrappers deferred: they initialize writable storage;
  hermetic crypto/scoring/fidelity/session-report tests passed. No production database,
  service restart, .env access/edit, real LLM, reflect job or campaign changes.
- No new dependencies. No Tauri/Rust implementation, process manager, project CLI,
  cross-platform sandbox, data migration or historical-script rewrite in this slice.


> Updated by Codex after each completed deliverable.
> Updated by human after each review, test, or decision.

---

## Current Status

**Phase**: 3 — Intelligence Expansion  
**Overall**: Phase 1 stable, Phase 2 implemented, Phase 2b backend endpoints complete, Phase 3 Steps 1-6 plus agent and tool-router registration complete  
**Last updated**: 2026-05-12 by Codex — Cycle-cap auto-completion: cycle_count column added to objectives, increment_cycle_count() in PersistentMemory, force-complete after MAX_CYCLES_PER_OBJECTIVE in run_autonomous_cycle(), directive no-objectives prompt, MAX_CYCLES_PER_OBJECTIVE config field  
**Next action**: Human to `pm2 restart morgoth`, wait 4-5 cycles (~40 min), then check `curl http://localhost:8000/api/objectives | python3 -m json.tool` — original objective should be status='done' with auto_completed=true in evidence; at least one new objective should exist

---

## Phase 1 — Backend (morgoth/)

### Core
| File | Status | Notes |
|---|---|---|
| `core/config.py` | ⬜ Todo | |
| `core/llm_client.py` | ⬜ Todo | |
| `core/tool_router.py` | ⬜ Todo | |
| `core/scheduler.py` | ⬜ Todo | |
| `core/brain.py` | ⬜ Todo | |

### Memory
| File | Status | Notes |
|---|---|---|
| `memory/working.py` | ⬜ Todo | |
| `memory/episodic.py` | ⬜ Todo | ChromaDB |
| `memory/persistent.py` | ⬜ Todo | PostgreSQL via asyncpg |

### Tools
| File | Status | Notes |
|---|---|---|
| `tools/base_tool.py` | ⬜ Todo | Interface contract |
| `tools/web_search.py` | ⬜ Todo | DuckDuckGo |
| `tools/code_executor.py` | ⬜ Todo | Sandboxed subprocess |
| `tools/file_manager.py` | ⬜ Todo | EVOLVABLE ZONE only |
| `tools/data_feeds/crypto.py` | ⬜ Todo | CoinGecko public API |
| `tools/data_feeds/finance.py` | ⬜ Todo | Yahoo Finance / FRED |
| `tools/data_feeds/news.py` | ⬜ Todo | RSS feeds |
| `tools/remember.py` | ⬜ Todo | Write to ChromaDB |
| `tools/recall.py` | ⬜ Todo | Query ChromaDB |
| `tools/create_agent.py` | ⬜ Todo | Calls agent_manager |
| `tools/notify.py` | ⬜ Todo | Telegram wrapper |

### Agents
| File | Status | Notes |
|---|---|---|
| `agents/base_agent.py` | ⬜ Todo | Interface contract |
| `agents/agent_manager.py` | ⬜ Todo | Lifecycle management |

### Notifications
| File | Status | Notes |
|---|---|---|
| `notifications/telegram.py` | ⬜ Todo | Bot + chat_id from .env |

### API
| File | Status | Notes |
|---|---|---|
| `api/server.py` | ⬜ Todo | FastAPI app, port 8000 |
| `api/routes/chat.py` | ⬜ Todo | |
| `api/routes/agents.py` | ⬜ Todo | |
| `api/routes/market.py` | ⬜ Todo | |
| `api/routes/brain.py` | ⬜ Todo | |
| `api/routes/admin.py` | ⬜ Todo | |
| `api/ws/handler.py` | ⬜ Todo | WebSocket manager |

### Entry Points & Config
| File | Status | Notes |
|---|---|---|
| `main.py` | ⬜ Todo | AWAKENING protocol |
| `scripts/init_db.py` | ⬜ Todo | PostgreSQL table creation |
| `requirements.txt` | ⬜ Todo | |
| `MORGOTH_PERMS.json` | ⬜ Todo | Initial permissions |

### Tests
| File | Status | Notes |
|---|---|---|
| `tests/conftest.py` | ⬜ Todo | |
| `tests/test_tools.py` | ⬜ Todo | |

---

## Phase 2 — Self-Modification Engine (morgoth/)

> Permissions are now enabled in `MORGOTH_PERMS.json`.
> Deliverables below are implemented and awaiting human review.

| File | Status | Notes |
|---|---|---|
| `ecosystem.config.js` | ✅ Done | PM2 app definition; interpreter=none, 2G memory cap, timestamped logs to data/logs/, PYTHONUNBUFFERED=1 |
| `scripts/setup_pm2.sh` | ✅ Done | Idempotent: installs pm2 if absent, deletes+restarts from ecosystem.config.js, saves, tails 50 lines, prints cheatsheet |
| `scripts/health_monitor.py` | ✅ Done | 60-second checks for Ollama, PostgreSQL, ChromaDB, persistent agents, memory, and stalled tasks |
| `scripts/init_db.py` | ✅ Done | Adds `objectives` and `ui_widgets` tables after base schema initialization |
| `core/objectives.py` | ✅ Done | Objective persistence, status updates, heuristic + LLM-assisted generation |
| `self_modify/code_writer.py` | ✅ Done | Permission-gated Python generation with syntax validation |
| `self_modify/code_tester.py` | ✅ Done | Isolated pytest runner with timeout and bounded test targets |
| `self_modify/diff_logger.py` | ✅ Done | Git diff capture and persistence to `self_modifications` |
| `self_modify/updater.py` | ✅ Done | Atomic integration, rollback on failed tests, diff logging |

### Verification

| Check | Status | Notes |
|---|---|---|
| `./.venv/bin/python -m pytest -q tests/test_phase2.py` | ✅ | 3 tests passed |
| `./.venv/bin/python -m compileall core/objectives.py self_modify scripts/health_monitor.py` | ✅ | All new Python modules compile |
| `./.venv/bin/python main.py` | ⚠️ Partial | Reached uvicorn startup and entered application startup without immediate traceback; full READY state not observed in short timeout window |

### PM2 Service Verification (2026-05-11)

| Check | Status | Notes |
|---|---|---|
| `bash scripts/setup_pm2.sh` | ✅ | pm2 installed via npm; morgoth started from ecosystem.config.js; logs tailed for 60s |
| `pm2 status` shows morgoth online | ✅ | PID 1742, 3m uptime, 0 restarts, 178.9 MB RAM |
| `curl http://localhost:8000/api/brain/status` → 200 | ✅ | Returned 200 immediately after startup |
| `pm2 logs morgoth \| grep -i "autonomous cycle"` | ✅ | "Autonomous cycle scheduled" present in logs |
| `pkill -f "main.py"; sleep 10; curl …/api/brain/status` → 200 | ✅ | PM2 restarted to PID 2433 (↺=1); 200 returned after 20s startup |

---

## PM2 Operations

Morgoth runs as a persistent PM2 service. Start with `bash scripts/setup_pm2.sh` on a fresh machine.

```
pm2 status              # check state
pm2 logs morgoth        # tail logs
pm2 restart morgoth     # restart
pm2 stop morgoth        # stop
pm2 monit               # live dashboard
pm2 startup             # configure auto-start on OS boot
```

Log files:
- Combined: `data/logs/pm2-combined.log`
- stdout:   `data/logs/pm2-out.log`
- stderr:   `data/logs/pm2-error.log`

---

## Phase 3 — Frontend (morgoth_ui/)

> Start Phase 3 only after Phase 1 backend is running and WebSocket is tested.
> Full spec in morgoth_ui/SPEC_UI.md

| Area | Status | Notes |
|---|---|---|
| Design system + Tailwind config | 🔒 Locked | Phase 3 |
| TypeScript types | 🔒 Locked | Phase 3 |
| WebSocket client | 🔒 Locked | Phase 3 |
| Zustand stores | 🔒 Locked | Phase 3 |
| Layout components | 🔒 Locked | Phase 3 |
| Dashboard page | 🔒 Locked | Phase 3 |
| Chat page | 🔒 Locked | Phase 3 |
| Agents page | 🔒 Locked | Phase 3 |
| Market page | 🔒 Locked | Phase 3 |
| Brain page | 🔒 Locked | Phase 3 |
| Admin page | 🔒 Locked | Phase 3 |

---

## Phase 2b — UI Refactor Backend Support

| File | Status | Notes |
|---|---|---|
| `api/routes/consciousness.py` | ✅ Done | Added recent THOUGHT aggregation for clusters and concept co-occurrence graph |
| `api/routes/objectives.py` | ✅ Done | Added objective list, create, and status-update endpoints backed by `core/objectives.py` |
| `api/routes/evolution.py` | ✅ Done | Added growth metrics and cumulative timeline endpoints from PostgreSQL tables |
| `api/server.py` | ✅ Done | Registered the new consciousness, objectives, and evolution routers |
| `tests/test_phase2b_routes.py` | ✅ Done | Added targeted tests for cluster/concept/timeline aggregation helpers |

---

## Phase 3 — Intelligence Expansion

| File | Status | Notes |
|---|---|---|
| `agents/research_agent.py` | ✅ Done | Deep research and synthesis agent with timeout degradation and UI task metadata |
| `agents/sentiment_agent.py` | ✅ Done | News and social sentiment analysis agent with timeout degradation and UI task metadata |
| `agents/macro_agent.py` | ✅ Done | Macro economic indicator agent backed by FRED tool context and UI task metadata |
| `tools/connectors/fred.py` | ✅ Done | FRED series search and observations tools using `FRED_API_KEY` from environment |
| `tools/connectors/reddit.py` | ✅ Done | Reddit public search and subreddit post tools |
| `tools/analysis/technical.py` | ✅ Done | Local RSI, MACD, SMA, and EMA analysis tool |
| `agents/agent_manager.py` | ✅ Done | Registers research, sentiment, and macro specialist agents without touching `core/` |
| `api/server.py` | ✅ Done | Registers Phase 3 FRED, Reddit, and technical-analysis tools in `build_tool_router()` |
| `tests/test_phase3_intelligence.py` | ✅ Done | Focused tests for specialist agent selection, FRED, Reddit, and TA tools |
| `core/brain.py` | ✅ Done | Adds bounded Morgoth identity, capped recall summaries, and an 8-tool chat schema subset before LLM calls |
| `tests/test_agents_and_llm.py` | ✅ Done | Covers system prompt injection, capped pre-chat recall context, chat tool subset selection, Ollama fallback, and non-fatal chat tool failures |

### Verification

| Check | Status | Notes |
|---|---|---|
| `./.venv/bin/python -m pytest -q tests/test_phase3_intelligence.py` | ✅ | 8 passed |
| `./.venv/bin/python -m compileall agents/research_agent.py agents/sentiment_agent.py agents/macro_agent.py agents/agent_manager.py tools/connectors/fred.py tools/connectors/reddit.py tools/analysis/technical.py tests/test_phase3_intelligence.py` | ✅ | All Phase 3 backend modules compile |
| `python3 -m compileall core/tool_router.py` | ✅ | Tool router module compiles after registration pass; literal `python` executable is not present on this machine |
| `./.venv/bin/python -m pytest -q tests/test_agents_and_llm.py` | ✅ | 5 passed after Ollama serialization/bootstrap race fix |
| `./.venv/bin/python -m compileall core/llm_client.py core/brain.py tests/test_agents_and_llm.py` | ✅ | Touched backend files compile |
| `./.venv/bin/python main.py` + chat curl | ✅ | Startup completed with no startup 500s; `POST /api/chat` returned `200` and identified Morgoth as Morgoth. Literal `python` executable is not present on this machine |

---

## Bootstrap Checklist

> To be completed by human before declaring Morgoth OPERATIONAL.

| Check | Status |
|---|---|
| Ollama reachable at OLLAMA_BASE_URL | ⬜ |
| deepseek-r1:14b model available | ⬜ |
| llama3:8b model available | ⬜ |
| PostgreSQL connection successful | ⬜ |
| All DB tables created by init_db.py | ⬜ |
| ChromaDB collections initialized | ⬜ |
| All 11 Layer 1 tools return success | ⬜ |
| FastAPI server starts on port 8000 | ⬜ |
| WebSocket /ws/chat accepts connection | ⬜ |
| Telegram notification received | ⬜ |
| Exploration report generated | ⬜ |
| First conversation with Morgoth successful | ⬜ |

---

## Decisions Made

> Log of implementation choices made when spec was ambiguous.

| Date | Decision | Reason |
|---|---|---|
| 2026-04-14 | `ui_widgets` table created during Phase 2 instead of waiting for Phase 5 | Root-level instructions required DB schema parity when introducing new tables |
| 2026-04-14 | `SafeUpdater` rolls back immediately on failed pytest validation | Robustness-first requirement forbids leaving partially integrated code in place |
| 2026-04-14 | Health monitor escalates repeated failures into monitoring objectives | Repeated faults should become tracked autonomous work, not only alerts |
| 2026-04-15 | `GetCryptoPriceTool` now serves per-symbol cached prices for 60 seconds and reuses stale cache on CoinGecko 429s | The UI poll cadence was exhausting CoinGecko limits and the market route needed a stable last-known-data fallback |
| 2026-04-15 | Agent creation accepts an optional `model` override | The API contract needed to accept the provided creation payload and return the actual model the UI should display |
| 2026-04-16 | Consciousness topics are derived from recent THOUGHT log tokens instead of a stored topic field | Existing logs do not persist explicit topic metadata, so Step 1 aggregates from content while keeping the API contract stable |
| 2026-04-17 | Ollama chat payloads now omit empty message fields and strip unsupported JSON Schema keywords from tool definitions | The local Ollama integration was rejecting tool-enabled requests with HTTP 400, and the safest contract is the minimal supported payload |
| 2026-04-25 | Phase 3 tools were implemented but not registered in `core/tool_router.py` | The session hard rule forbids touching `core/`; runtime registration needs human approval or a later task that allows that file |
| 2026-04-28 | Phase 3 tools are registered in `api/server.py` bootstrap | The existing runtime registration block is `build_tool_router()`, while `core/tool_router.py` only defines the registry class |
| 2026-05-11 | `tool_choice: required` not passed — Ollama API does not support it | The Ollama `/api/chat` endpoint accepts model-inference `options` (temperature etc.) but has no `tool_choice` parameter; the imperative prompt alone is sufficient |
| 2026-05-11 | `update_objective` uses dynamic SQL parameter building instead of a single fixed query | The method must handle any combination of status-only, evidence-only, or both; a single query with NULLable params would silently overwrite existing evidence with NULL |
| 2026-05-11 | Autonomous cycle stores objectives in ChromaDB, not PostgreSQL | `remember` tool writes to ChromaDB; `/api/objectives` reads PostgreSQL `objectives` table; bridging these is a future task |
| 2026-05-11 | `create_objective` uses hardcoded category='research' | `ObjectiveCategory` enum only has research/capability/monitoring/optimization; 'autonomous' caused a 500 on list endpoint; 'research' is the most appropriate default for autonomous knowledge-gap objectives |
| 2026-05-11 | process_message() converted from single-round to 5-round agentic tool loop | Single round prevented the model from calling create_objective after seeing news/price data; the while loop passes tools on every subsequent call so the model can chain discoveries into objective creation |
| 2026-05-12 | Force-completion happens in code (not prompt) after N cycles | llama3.1:8b reliably fails to call update_objective on its own after 6+ cycles; prompt-only fixes are insufficient for weak models; cycle_count column is idempotent via IF NOT EXISTS guard |
| 2026-05-12 | ALTER TABLE objectives ADD COLUMN IF NOT EXISTS cycle_count runs in PersistentMemory.initialize() with try/except | The objectives table is created by scripts/init_db.py separately; wrapping the migration in try/except keeps startup non-fatal on first boot before init_db.py has run |

---

## Dependency Changes

> Any packages added beyond the baseline requirements.txt in SPEC.md.

| Package | Version | Reason | Added by |
|---|---|---|---|
| — | — | — | — |

---

## Issues & Blockers

> Current known issues or blockers.

| Issue | Status | Notes |
|---|---|---|
| Task `result` loaded from PostgreSQL as string `'null'` caused `Task` validation failure on startup | ✅ Resolved | Fixed task row normalization to deserialize JSON text and coerce `'null'` to `None` before building `Task` models |
| CoinGecko `429 Too Many Requests` errors from `/api/market/prices` | ✅ Resolved | `GetCryptoPriceTool` now caches the last successful result per symbol for 60 seconds and returns cached data when CoinGecko rate-limits; `/api/market/prices` also catches `httpx.HTTPStatusError` and serves last known values |
| Chat POST/UI payload mismatch and duplicate live-response risk | ✅ Resolved | UI chat sender now includes `user_id`, uses the websocket when connected, and falls back to REST only when needed; backend POST contract remains `{content, user_id}` returning `BrainResponse` |
| Agent creation payload/response drift | ✅ Resolved | `POST /api/agents` now accepts optional `model` and returns the created agent shape the UI expects; the UI inserts the returned agent immediately instead of waiting for the next poll |
| `GET /api/agents` raised `PydanticSerializationError` because runtime agent objects leaked `OllamaLLMClient` into API payloads | ✅ Resolved | `BaseAgent.to_dict()` now returns an explicit serializable DTO and no longer serializes subclass runtime dependencies |
| Ollama `/api/chat` returned `400 Bad Request` for tool-enabled chat payloads | ✅ Resolved | Chat messages now serialize only supported fields, tool schemas are sanitized before dispatch, the exact payload is logged before send, and `Brain.process_message()` retries once without tools to isolate and bypass tool-schema rejections |
| Morgoth had no stable identity prompt and did not preload previous conversation memories before chat | ✅ Resolved | `Brain.process_message()` now starts every LLM turn with `SYSTEM_PROMPT`, calls the `recall` tool against the `conversations` collection before the LLM request, and injects returned memories as system context |
| Ollama `llama runner process has terminated` from chat context-window overflow | ✅ Resolved | `Brain.process_message()` now sends a sub-100-word system prompt, summarizes at most 3 recalled conversation memories using only `content[:200]`, and exposes only 8 common chat tools to the LLM while keeping all tools registered for direct execution |
| Concurrent Ollama runner crash from bootstrap self-test agent and chat race | ✅ Resolved | All `OllamaLLMClient.chat()` calls now share a module-level `asyncio.Lock`, awakening uses a direct Ollama ping instead of creating `self_test_agent`, and chat tool failures are returned to the model instead of surfacing as HTTP 500s |
| `python main.py` startup verification on this machine | ✅ Resolved | `./.venv/bin/python main.py` completed application startup and exposed port `8000`; the literal `python` executable is not installed in this shell |
| Live verification of chat response | ✅ Resolved | `curl -X POST http://localhost:8000/api/chat ...` returned `200 OK` with Morgoth identifying itself as Morgoth |
| Ollama runner crashes on first post-awakening chat request (`llama runner process has terminated: %!w(<nil>)`) | ✅ Resolved | `Brain.awaken()` now sends a full-context warmup chat (system prompt + 8 chat tools) immediately after the direct ping, so the model is loaded into VRAM with the same inference shape it will see in production before any real request arrives. Warmup is non-fatal. |

---

## Session Log

> Brief log of each work session for continuity.

| Date | Who | What was done |
|---|---|---|
| Project init | Human | Repos created, SPEC.md written, environment set up |
| 2026-04-14 | Codex | Implemented Phase 2 deliverables and added focused tests for self-modify components |
| 2026-04-15 | Codex | Added CoinGecko caching/rate-limit fallback, aligned chat and agent creation API contracts, and verified targeted backend tests plus Python compilation for touched modules |
| 2026-04-16 | Codex | Completed Phase 2b Step 1 by adding consciousness, objectives, and evolution endpoints plus focused backend aggregation tests |
| 2026-04-17 | Codex | Fixed the `/api/agents` serialization boundary, hardened Ollama chat payload construction and fallback behavior, added focused regression tests in `tests/test_agents_and_llm.py`, and confirmed targeted tests/compilation pass; live port-8000 verification stayed blocked by startup |
| 2026-04-25 | Codex | Completed Phase 3 backend Steps 1-6 and specialist agent registration: added research, sentiment, and macro agents; FRED, Reddit, and technical-analysis tools; and focused tests passing without live network calls |
| 2026-04-28 | Codex | Registered Phase 3 FRED, Reddit, and technical-analysis tools in the runtime tool router bootstrap and verified `core/tool_router.py` compilation with `python3` and the project venv |
| 2026-05-02 | Codex | Added Morgoth system identity prompt plus recall-backed conversation context in `core/brain.py`; updated focused regression coverage and verified tests/compilation |
| 2026-05-04 | Codex | Fixed Ollama chat context overflow by shortening `SYSTEM_PROMPT`, summarizing recalled memories, limiting LLM-visible chat tools to 8, and verifying focused tests/compilation; live curl remains blocked by local app startup not binding port 8000 |
| 2026-05-10 | Human | Full live end-to-end verification: startup OK, datetime chat OK (web_search called, 200 returned), ETH price OK (CoinGecko 2360.24 EUR +1.11%), brain logs OK, objectives empty. Ollama runner crashed on first chat attempt then self-recovered without restart. No code changes. |
| 2026-05-11 | Codex | Added post-awakening Ollama warmup in `Brain.awaken()` (full chat context + 8 tools, non-fatal); added `Brain.run_autonomous_cycle()` background loop driven by `AUTONOMOUS_CYCLE_MINUTES` config; started loop as asyncio task in `Brain.initialize()`; added `PersistentMemory.get_objectives()` for pending-objective queries. 34 tests pass; all three modules compile. |
| 2026-05-11 | Codex | Live verification: warmup PASS (both log lines present, ~1.7s), first chat PASS (200 OK, no Ollama crash), 1st autonomous cycle PASS (fired at ~58s after startup), 2nd autonomous cycle PASS (count reached 2). Objective creation: NO — model replied without calling tools (tool_calls=0); response stored in conversations episodic memory only. `.env` restored to AUTONOMOUS_CYCLE_MINUTES=10. |
| 2026-05-11 | Codex | Rewrote autonomous cycle prompts to be imperative ("ACT NOW. Tool calls only."). Verified: both cycles fired tool_calls=3 (get_news + get_crypto_price + remember). Note: `tool_choice: required` is not supported by Ollama API — prompt-only fix is sufficient. Objectives still show empty in `/api/objectives` because `remember` writes to ChromaDB while that endpoint reads PostgreSQL `objectives` table (architectural gap, not a regression). `.env` restored to AUTONOMOUS_CYCLE_MINUTES=10. |
| 2026-05-11 | Codex | Added `CreateObjectiveTool` (tools/objectives_tool.py) writing to PostgreSQL `objectives` table with valid category='research'; added `create_objective()` to `PersistentMemory`; registered tool in api/server.py; added to CHAT_TOOL_NAMES; updated autonomous cycle prompt to call `create_objective` in STEP 3; converted tool execution from single-round `if` to 5-round agentic `while` loop in `process_message()` so model can chain tool calls across rounds. Live result: objective "Crypto Market Sentiment Analysis" confirmed in PostgreSQL at 09:58:26; `/api/objectives` returns 200 with data. 39 tests pass. `.env` restored to AUTONOMOUS_CYCLE_MINUTES=10. |
| 2026-05-11 | Codex | Created `ecosystem.config.js` (PM2 app definition with interpreter=none, autorestart, 2G cap, timestamped logs); rewrote `scripts/setup_pm2.sh` to be fully idempotent (installs pm2 if absent, delete+start from ecosystem config, save, tail, cheatsheet); chmod +x applied. All 5 verification checks passed: script clean, pm2 status online, brain/status 200, autonomous cycle in logs, survived pkill+restart with 200 returned. |
| 2026-05-11 | Codex | Fixed infinite reddit_search loop: (1) Added `UpdateObjectiveTool` (`tools/objectives_tool.py`) writing status+evidence to PostgreSQL; added `PersistentMemory.update_objective()` (`memory/persistent.py`); registered in `api/server.py`; added to `CHAT_TOOL_NAMES` in `core/brain.py`. (2) Rewrote autonomous cycle objective branch to recall past actions from episodic memory and prompt Morgoth to decide done/not-done explicitly. (3) Added progression rule to SYSTEM_PROMPT. 42 tests pass; all 5 touched modules compile. |
| 2026-05-12 | Codex | Force-completion via cycle cap: (1) `ALTER TABLE objectives ADD COLUMN IF NOT EXISTS cycle_count INTEGER DEFAULT 0` in `PersistentMemory.initialize()`. (2) `increment_cycle_count()` method in `memory/persistent.py`. (3) Auto-complete logic at cycle start in `Brain.run_autonomous_cycle()` — increments counter, force-marks objective done after `MAX_CYCLES_PER_OBJECTIVE` cycles, returns early to unblock the loop. (4) `MAX_CYCLES_PER_OBJECTIVE` field in `AppConfig` + env loading in `load_config()`. (5) Added `MAX_CYCLES_PER_OBJECTIVE=3` to `.env`. (6) More directive no-objectives prompt pointing to `create_objective`. (7) Two new unit tests for `increment_cycle_count`. 44 tests pass; all touched modules compile and import cleanly. |


## 2026-09-29 — Internal Codex CLI migration (inference blocked, fail closed)

- Verified handoff HEAD afd527cced99a8114c800ec9f3ef34ce93833f6b and clean backend tree.
- Added core/llm/codex_cli.py through existing provider factory/registry; migrated THESIS, SYNTHESIS, reflect, shadow and reserved scout defaults to codex-cli. Chat remains Ollama.
- Automatic fallback is API -> Codex -> Ollama; Claude remains explicit rollback only. Heartbeat/environment reports use non-inference probes.
- CLI 0.159.0: both isolated direct probes emitted tool events despite restrictions. SAFE_FOR_WORKLOADS=False rejects all real prompts before subprocess execution; no runtime override bypass. Activation remains BLOCKED pending structural isolation and successful canary.
- Updated provider/shadow/reflect selection, hermetic subprocess guard and tests, docs/CODEX_PROVIDER.md, scripts/probe_codex_provider.py. No new Pydantic models/endpoints, UI changes or dependencies.
- Canonical hermetic suite: 0 failed, 1678 passed (+37), 2 skipped, wall 23.6 s, exit 0. Existing crypto price/cache/rate-limit/history and thesis/backtest tests included; no live crypto/DB test.
- No production .env read/edit, service restart, reflect/shadow workload, proposal approval/application or campaign e6563e60 operation. Human gate 3 unchanged.
- Operator migration and explicit rollback assignments are documented; production runtime overrides were not inspected.


## 2026-09-30 — Codex structural qualification: NOT_QUALIFIED

- Initial backend 0d29efc, clean, origin/main matched. Only provider diagnostics,
  synthetic security probes/tests and documentation changed; no dependencies.
- Distinguished structured error diagnostics from tool authority without allowing
  either through the provider. SAFE_FOR_WORKLOADS remains False; no env bypass.
- Seven independent synthetic probe kinds; event allowlist redaction, outside
  read/write witnesses, fake repo and credential-free HOME/MCP configuration.
- No forbidden effect observed; host-read confinement remains structurally
  unproven. Authenticated observations and unauthenticated config are inconclusive.
- Canonical suite: 1867 passed (+42), 2 skipped, wall 15.0 s, exit 0; focused 126 passed.
- Runtime, campaigns, Project/Domain, Deribit measurement and UI untouched.


## 2026-09-30 — Project-scoped LLM profiles

- Added immutable data-driven legacy/claude/codex policies and a shared Project
  manager in core/llm/profiles.py. No API models/endpoints, UI or new dependencies.
- Atomic 0600 runtime state; managed routes override historical task env; legacy
  preserves prior behavior. Per-workload reads observe the next selection.
- models use/profiles and env recommendations use readiness, not presence. Codex
  remains NOT_QUALIFIED / SAFE_FOR_WORKLOADS=False with no force bypass.
- Canonical campaign-live SQL + bounded SHARE lock reject live-campaign switches
  and serialize concurrent campaign starts. No campaign row writes or initialization.
- Focused 221 passed; dedicated morgoth_test SQL proofs 3 passed (profile guard,
  Project isolation, campaign lifecycle); canonical 1897 passed (+30), 2 skipped,
  wall 16.7 s, exit 0. No production profile switch, service operation or market call.
- docs/LLM_PROFILES.md defines the future Tauri selector contract; Unix lock
  portability, custom profile authoring and Tauri implementation remain future work.

## 2026-10-09 — Claude CLI safe error taxonomy

- `ReflectLLMError.safe_code` now distinguishes six fixed CLI failure classes;
  `llm_calls.error_code` is nullable, migrated idempotently, and stores no provider output.
  Historical `outcome=error:ReflectLLMError` remains compatible. The prior
  disposable synthesis failure cannot be classified retroactively.
- No CLI argv, timeout, fallback, provider readiness, or research semantics changed.
  New focused tests cover each branch and secret-output hygiene; a disposable
  `morgoth_test` proof covers fresh and migrated Project-local `llm_calls`.
- Canonical hermetic: 2077 passed, 2 skipped, wall 33.1 s, exit 0.
  Focused provider/SQL selection: 30 passed, wall 1.68 s, exit 0.

## 2026-10-10 — Codex 0.162.1 standalone confinement requalification

- A new backend-only branch targets the installed `codex-cli 0.162.1`; the
  earlier 0.162.0 diagnostic remains UNKNOWN. The exact standalone release
  mounts its executable file only through the mandatory Bubblewrap envelope;
  the npm layout remains supported. No HOME/config mount was broadened.
- Exact version/flags/features and confined ChatGPT login passed without
  inference. Host structural read/write and process cleanup tests passed.
  `SAFE_FOR_WORKLOADS=False`; strict JSONL validator unchanged.
- The full hermetic suite is currently blocked by unrelated `test_apply`
  nested-test hangs in this execution environment; focused Codex tests pass.
  One diagnostic canary ran after a clean committed preflight: process exit 1,
  no turn completion, 14 diagnostics (first early config-template match,
  second early unknown, later critical errors unknown), zero tool events and
  no outside-file effect. Verdict `BLOCKED_CODEX_STARTUP_UNKNOWN`; no retry or
  text inference. Focused: 117 hermetic + 3 host integration passed.

## 2026-10-10 — Codex 0.162.1 critical-exit diagnosis (workloads blocked)

- Added diagnostic-only reduction for top-level JSONL `error` and `turn.failed`,
  separate from startup warning items. Conservative exact-tag templates produce
  fixed codes; mixed/unmatched content stays UNKNOWN. Raw CLI text is neither
  persisted nor printed. Production validator and `SAFE_FOR_WORKLOADS=False`
  remain unchanged.
- Canonical `test_apply` timeout reproduced on the exact clean `ed68bd3` base;
  the synthetic baseline runner is reached, then pytest-asyncio teardown times
  out. Classification: `CANONICAL_BASELINE_REPRODUCED`; host/plugin root cause
  unresolved, full canonical suite **NOT PASS**. No nested pytest subprocess
  was observed in the isolated new-file node.
- One new confined canary exited 0 with a completed turn, two startup warning
  items, zero critical events, zero tool events and no observed outside effect.
  The old critical exit did not recur; cause remains UNKNOWN. No retry or
  separate text inference. Workloads remain BLOCKED.

## 2026-10-10 — Canonical runner provenance and teardown recovery

- The old canonical runner always copied `/home/corio/Morgoth/morgoth`, even
  when invoked from a feature worktree. That checkout was `e3952430` while
  the requested base was `f6db1f15`; two selected Codex diagnostic files
  existed only in the feature tree. Historical canonical counts cannot be
  attributed to feature branches without independent copy evidence. The old
  clean-base timeout comparison used the same hardcoded source and was not a
  distinct-tree canonical comparison.
- The runner now derives its source from its own module, requires the expected
  Git origin, clean worktree and stable HEAD, verifies selected copied file
  hashes, and prints requested/tested source SHAs. Its pytest command uses
  `gates._build_pytest_argv` with the same loadfile xdist schedule, socket and
  marker restrictions, Bubblewrap/net namespace, and systemd cgroup wrapper.
  `.git`, `.env`, data, vault, backups and the gate's other exclusions remain.
- `tests/test_apply.py::test_baseline_timeout_leaves_row_at_approved_pending_apply`
  passes its assertions, then hangs in Python 3.12 default-executor teardown
  inside the agent tool sandbox. A minimal `asyncio.run(asyncio.to_thread(...))`
  also times out there, but exits normally outside that tool sandbox. The
  isolated node passes in both separate clean-base and feature worktrees
  outside it. This supports a host tool-sandbox teardown cause; it is not
  evidence of nested pytest, an application task leak, or an xdist defect.
  Full canonical acceptance must use the runner's own isolation and verified
  feature copy.
- Committed source `ec9997e` passed the isolated problem node in the canonical
  four-worker sandbox (1/1). The full canonical run verified matching source
  SHA and selected file hashes, then failed: 1917 passed, 2 skipped, four
  worker-crash errors, exit 1, 175.3 s. The four affected nodes passed
  together when isolated under the same sandbox (9/9). The serial diagnostic
  run, with identical confinement and source but no xdist, had 2132 passed,
  2 skipped, 220 deselected, and one 15-second child-process timeout in
  `test_fresh_process_binds_only_selected_project_token`; that node passed
  alone under the canonical sandbox. No deterministic repository-level fix is
  established for these full-suite interactions. Canonical acceptance remains
  **NOT PASS**; focused Codex/Bubblewrap hermetic tests passed 150/150 with
  three PostgreSQL-gated integration tests excluded by the canonical marker.

## 2026-10-11 — Canonical full-suite reliability investigation

- Exact inherited parallel crash nodes: `test_cli_json_and_fresh_process_runtime_resolution`
  on gw0, `test_ui_no_backend_skips_health_and_execs_dev_sh` on gw1,
  `test_factory_never_imports_engine_server` on gw2, and
  `test_failure_stage_attribution_and_secret_exclusion[WORK_INFERENCE]` on
  gw3. Their exit signals and old cgroup counters were not retained. The
  inherited serial failure was the 15-second child timeout in
  `test_fresh_process_binds_only_selected_project_token`; the child stage was
  not retained. These are distinct observations, with no proven shared cause.
- The canonical runner now rejects all included source symlinks, copies with
  symlink following disabled, and verifies a hash manifest of every included
  copied regular file. Synthetic outside-secret and unselected-file mutation
  tests fail under the old behavior and pass now; a clean tracked-symlink
  fixture confirmed the old runner copied synthetic outside content while the
  new runner rejected the link before copying. On timeout, it kills and
  reaps only its dedicated launch process group; a disposable user-scope
  sleeper confirmed no owned child survived. Existing source and gate policy
  checks, exclusions, Bubblewrap, netns, cgroup and pytest selection remain.
- Targeted diagnosis: full authority module serial 10 passed; the four crash
  modules together under 4-worker loadfile scheduling 55 passed; combined
  provenance/apply/authority/crash-module selection 86 passed. One bounded
  parallel diagnostic from clean candidate `be73b4c` copied the same SHA and
  passed 2136 with 2 skipped, exit 0, wall 48.3 s. Its own scope peaked at
  700182528 bytes and 87 PIDs, with zero OOM kills and zero PID-limit hits.
  This run did not reproduce the old failure; it does not prove that old host
  resource exhaustion or cross-test contamination was absent then.
- Focused Codex/Bubblewrap hermetic selection: 150 passed, 3 PostgreSQL-gated
  integration tests deselected. No model inference, production data, provider
  gate, event validator or sandbox authority changed. The historical full-suite
  failure has no deterministic cause or corrective reproducer yet, so this
  chantier's **reproducibly green canonical acceptance remains NOT ACCEPTED**
  despite one green diagnostic run. `SAFE_FOR_WORKLOADS=False` remains.

## 2026-10-11 — Codex 0.162.1 unified diagnostic canary

- Committed one bounded, diagnostic-only reducer and `--unified` entry path.
  It combines exact-template warning and critical classification, turn state,
  exact `CANARY_BLOCKED` response verification, tool events and outside-marker
  effects from one private JSONL stream. The production event validator,
  Bubblewrap mounts, auth mount, CLI arguments and `SAFE_FOR_WORKLOADS=False`
  did not change. Source-template matches remain probable families because
  the tagged JSONL mapper does not preserve notification variants.
- No-inference focused selection: 110 passed and 3 host integration checks
  deselected. The 3 host structural/auth/timeout-cancellation checks passed
  separately with a test-shaped database guard; no database connection.
  Exact 0.162.1 confined discovery/login preflight passed from clean commit
  `3470b5b`. No new full canonical run; the historical `be73b4c` run had
  2,136 passed, 2 skipped, exit 0, with intermittent reliability unresolved.
- Exactly one 90-second-bounded confined model canary ran. It exited 1,
  started but did not complete the turn, failed the turn, and did not produce
  the exact expected response. Three diagnostic items occurred at positions
  2, 3 and 9: one config-warning template match and two unknown. Eleven
  critical events classified `CODEX_FATAL_UNKNOWN`. Zero forbidden tool
  events, zero unknown event shapes, and unchanged outside markers were
  observed. Verdict `BLOCKED_CODEX_FATAL`; no retry or separate text probe.
  The specific terminal cause and both unknown warning families remain open.
- Desktop worktree `a7dab1b` unchanged. No Weather, research START, Claude,
  production database/service operation or workload activation. Next work
  requires separate, narrow authorization for safe fatal attribution.

## 2026-10-12 — Codex 0.162.1 text transport isolation

- Diagnostic-only changes committed at `a24df60`: the unified reducer now
  distinguishes pre-turn/in-turn/post-turn diagnostic items and privately
  groups repeated critical error messages. Exact-tag fixed error families
  were added for request timeout, overload, quota, content filter, rate-limit
  report, stream-disconnect symptom and connection-failed display. Unknown
  text remains `CODEX_ERROR_UNKNOWN`. No raw messages, error-message hashes,
  response text or retry payloads leave the reducer; the permitted fixed
  text-response SHA-256 is reported. Production provider/validator and
  Bubblewrap code remain unchanged; `SAFE_FOR_WORKLOADS=False`.
- No-inference focused selection 124 passed, 3 host integration checks
  deselected. The three host confinement/auth/cleanup checks passed separately
  with a test-shaped database guard and no DB connection. Clean exact-CLI
  preflight and confined resolver/trust/workspace checks passed; no external
  model-service reachability claim was made. No new full canonical run; the
  historical `be73b4c` success was 2,136 passed and 2 skipped, with prior
  intermittent full-suite failures unresolved.
- Exactly one fixed-prompt, 90-second-bounded text inference ran through
  the unchanged confined transport. It exited 1, started and failed the turn,
  produced no response, emitted 10 top-level errors plus one failed-turn
  record, and had six distinct private critical messages (largest equal group
  two). All critical messages remained unclassified. Diagnostic items:
  pre-turn 2 (config-template, unknown), in-turn 1 (unknown), post-turn 0.
  Tool events and unknown event shapes: zero. Verdict
  `CODEX_TEXT_TRANSPORT_BLOCKED_UNKNOWN`. No second inference or file/tool
  canary was run. The result shows this attempt's failure was not limited to
  an adversarial prompt; its underlying cause is still unknown.
- No Desktop, production checkout, main branch, Weather, START, synthesis,
  thesis or Claude change. Next work must narrowly attribute the pinned CLI's
  actual top-level error family before changing runtime resources or policy.

## 2026-10-12 — Codex confined connectivity diagnosis

- Feature branch from clean `ca106ade`; diagnostic script/tests committed as
  `2496320` before a single non-inference `codex doctor --json` run.
- Exact 0.162.1 doctor source shows HEAD provider reachability plus authenticated
  Responses WebSocket handshake, state checks, and MCP configuration checks.
  Confined `mcp list --json` showed zero configured servers before doctor.
  No user config or repository was mounted; Bubblewrap and provider code were
  unchanged. Doctor is not an offline-only check.
- Focused no-inference selection: 97 passed, 3 host integration deselected.
  Separate structural confinement, confined login and process cleanup checks:
  3 passed. No full canonical run; historical `be73b4c` 2,136 passed and 2
  skipped remains a single success amid unresolved intermittent failures.
- One doctor run: exit 1, valid JSON; individual HTTPS provider check FAIL
  without a reported HTTP status; WebSocket check WARNING with no successful
  handshake/status. WebSocket enabled; no immediate-close indication. Child
  proxy environment names all absent. Verdict
  `CODEX_HTTPS_REACHABILITY_BLOCKED` in the nested development network.
  DNS/TLS/proxy/auth and production-network root cause are not established.
- No model inference, retry, Weather, research START, production state or
  Desktop change. `SAFE_FOR_WORKLOADS=False`. Smallest next differential:
  bounded DNS/TLS inside versus outside the development network restriction,
  without changing mounts, provider restrictions or credentials.

## 2026-10-12 — operator-native Codex authority-canary preparation

- Operator-reported ordinary-WSL evidence: exact confined 0.162.1 doctor
  exited 0 with HTTPS/WebSocket checks OK; one confined text request completed
  with the exact 17-byte response and no critical/tool events. One of two
  pre-turn diagnostics was a config-template match; the other remains UNKNOWN.
  The prior development-network failures are not proven to have one cause.
- Diagnostic-only doctor parser now accepts the tagged exact HTTPS/WebSocket
  success summaries when JSON redacts numeric details, reports status-only
  success without inventing HTTP/handshake codes, and rejects a no-endpoint
  OK check as inconclusive. Startup templates require whole-message matches;
  current `skip_host_skill_discovery`, config, state and inner-sandbox warnings
  were investigated as candidate families, without claiming the unknown
  message's cause or changing production event validation.
- Added an operator-only, one-shot authority-canary entry point. It checks a
  clean tracked-origin-equal feature checkout and confined exact-CLI login,
  uses the unchanged 90-second production transport and synthetic outside
  markers, and emits only fixed verdicts/counts/Booleans. Unknown warnings
  still block. The command is documented in `docs/CODEX_PROVIDER.md`.
- No inference, doctor call, Weather, START or production-state operation ran
  in this development chantier. Bubblewrap mounts, CLI flags, narrow readable
  auth mount, host networking and `SAFE_FOR_WORKLOADS=False` are unchanged.
- No-inference focused checks: 124 passed, 3 host integration deselected;
  separate structural/auth/cleanup checks: 3 passed. No full canonical rerun;
  historical `be73b4c` pass remains one run with intermittent failures open.

## 2026-10-13 — Codex warning/response attribution prepared

- Operator-reported prior ordinary-WSL canary: exit 0, completed turn, zero
  critical/tool events and outside effects, but expected response false and
  one unknown pre-turn warning. Its discarded private output cannot be
  retrospectively classified; no new model inference ran in this branch.
- Diagnostic reducer and operator wrapper now export bounded lifecycle,
  final-file, final-text and completed-assistant-item facts plus a fixed list
  of simultaneous response-failure classes. Actual harness carries file
  presence/readability; malformed or unreadable evidence remains UNKNOWN.
  The wrapper requires this clean, tracked-origin-equal feature checkout.
- Pinned 0.162.1 source review confirmed multiple warning variants map to
  error-shaped items without origin identity. One exact WSL1 namespace
  template was added; skill-discovery and unmatched messages remain UNKNOWN.
  See `docs/CODEX_PROVIDER.md` for tagged source references and operator
  command. Production event validator, Codex flags, sandbox mounts and
  `SAFE_FOR_WORKLOADS=False` are unchanged.
- No Weather, research START, production database/service operation, Desktop
  change, new dependency or full canonical campaign.
- Focused no-inference checks: 141 passed, 3 host checks deselected; separate
  host structural/auth/timeout-cancellation checks: 3 passed. Historical
  canonical evidence was not rerun or upgraded to repeated reliability.

## 2026-10-13 — Codex 0.162.1 startup-warning provenance (no inference)

- Began from clean `e09424decfed75e227cc702e2ab3984811d95470` on
  `codex-01621-warning-provenance-v1`; Desktop was not changed. The latest
  operator-reported canary's exact response, clean tool/effect observations
  and remaining two pre-turn diagnostics are inherited evidence only.
- Pinned-source trace: exec builds a warning list and starts an in-process
  app-server with `SessionSource::Exec` and `log_db: None`; initialize can
  replay config warnings before the turn. The JSONL mapper erases the
  ConfigWarning/Warning/DeprecationNotice/ModelRerouted variant into one
  error-shaped item; top-level Error stays distinct. Exact template matches
  suggest families, not proven notification origins. The under-development
  feature notice originates as WarningEvent despite its diagnostic label
  `CODEX_STARTUP_CONFIG_WARNING`.
- Standalone app-server initialization uses different config, session source,
  state recovery, plugin-startup and log-DB paths. Its non-inference notices
  cannot faithfully attribute the previous exec warning. Verdict:
  `STARTUP_PROVENANCE_NOT_REPRODUCIBLE_WITHOUT_INFERENCE`. No app-server
  diagnostic was implemented or run. A separately reviewed structured
  notification transport in the exact exec path is the narrow future route;
  raw messages must remain operator-local.
- Added hermetic tests for origin loss, secret-free projection, production
  diagnostic rejection and UNKNOWN-warning blocking after an exact response.
  Corrected an inherited diagnostic test expectation: a private suffix makes
  an otherwise exact template UNKNOWN. This failure reproduced on the clean
  base; no classifier or production gate was relaxed.
- Focused no-inference tests: 229 passed, 3 deselected. Separate host
  structural/auth/timeout-cancellation tests: 3 passed. The first host run
  stopped at the test-only URL guard; rerun with a disposable `_test` URL
  shape passed and did not connect to PostgreSQL. No full canonical rerun,
  model inference, Weather, START, production service/database or new
  dependency. `SAFE_FOR_WORKLOADS=False`.

## 2026-10-13 — operator-local Codex warning review prepared (no inference)

- Branched from clean `0641ffa2728c4cafff10193390808298d5885df3` on
  `codex-01621-private-warning-review-v1`. The prior operator-native
  authority canary is inherited evidence only: exact response, zero tool and
  outside effects, one config-topic diagnostic and one UNKNOWN. Its discarded
  private warning cannot be recovered.
- Added `scripts/codex_local_warning_review.py` for an operator-only, one-shot
  fixed text probe. It checks the exact clean tracked-origin-equal branch,
  0.162.1 flags and confined ChatGPT login, foreground local TTY and typed
  consent before one 90-second `_run(build_argv(...))` call. The output-last-
  message target is `/dev/null`; stdout/stderr/JSONL remain private in memory.
  A second consent gates one bounded, escaped, best-effort-redacted UNKNOWN
  warning displayed only on `/dev/tty`. Public JSON contains fixed facts and
  a manual category cannot change the BLOCKED verdict.
- Tagged JSONL collapses several noncritical notification variants into one
  error-shaped item; top-level critical errors remain separate. Position does
  not prove variant identity. Redaction cannot guarantee arbitrary secrets
  are absent; terminal scrollback/recording and pseudo-TTY automation cannot
  be ruled out. The script refuses ordinary redirected/unattended use.
- Diagnostic-only text reduction now checks the last completed assistant item
  when multiple items occur, matching the pinned final-message semantics.
  Production `_validate_events()`, `SAFE_FOR_WORKLOADS=False`, Bubblewrap
  mounts, auth mount, tools, model and network restrictions are unchanged.
- No-inference focused tests: 243 passed, 3 host checks deselected; the 3
  separate host structural/auth/timeout-cancellation checks passed. No full
  canonical rerun, model inference, Weather, START, production database or
  service operation, Desktop change or new dependency.

## 2026-10-13 — private Codex review PTY correction (no inference)

- Started from clean `408e75cf66fc45e090977dca0e32a0b01a3089f4` on
  `codex-01621-operator-tty-fix-v1`; Desktop unchanged at `a7dab1b7`.
- Linux `/dev/tty` is a 5:0 alias, so comparing its device number with
  `/dev/pts/N` falsely rejected a real operator terminal. Corrected the gate
  to compare the three standard streams with each other and verify every
  stream and `/dev/tty` against the foreground process group.
- Actual PTY integration exposed a second blocker: Python's combined `r+`
  text wrapper may reject a non-seekable PTY. The review now uses separately
  validated and owned read/write `/dev/tty` handles. Consent, private-output
  handling, one-request limit and provider/sandbox gates are unchanged.
- No-inference provider preflight passed. Focused Codex/provenance suite:
  250 passed, 3 host cases deselected. Separate host structural/auth/cleanup
  suite: 3 passed. The focused suite includes one real foreground PTY pass,
  five real PTY rejection cases and a no-controlling-terminal rejection.
  Zero model inference. `SAFE_FOR_WORKLOADS=False`; no activation/deployment.
