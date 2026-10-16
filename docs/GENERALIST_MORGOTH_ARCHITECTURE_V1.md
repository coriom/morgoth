# Generalist Morgoth architecture V1 — reviewable target, not runtime authority

## V9.5 verified closed-loop synthetic browser planning (2026-10-16)

The operator reported six V9.4 model-selected actions that passed schema and
browser policy, executed in isolated Chromium and produced fresh observations.
The independent task checker did not accept the result before the six-call
budget ended. The discarded choices do not establish repetition or explain
why the model failed to finish.

The opt-in parent controller now requests a bounded post-action observation
inside each worker acknowledgement. It records a transition only after the
worker confirms execution. A deterministic visible-state comparison excludes
revision numbers and ephemeral element IDs, and compares title, visible text,
origin, control labels/types, values and enabled state. An observed change is
reported as `UI_CHANGE_OBSERVED` and `TASK_PROGRESS_UNCERTAIN`; it is never
mistaken for independent acceptance. Identical rendered states are reported
as `NO_OBSERVABLE_CHANGE`. The next Ollama request receives up to five
confirmed transitions, compact added/removed control names, repeated
ineffective actions, remaining call/time budgets and the current action menu.
It receives no model-invented history, previous full page contents or raw
responses. Page labels in history remain untrusted descriptions.

The controller stops with `BROWSER_LOOP_DETECTED` after two consecutive
no-change actions, a repeated identical no-change action in the same state,
or a completed two-state oscillation. A repeated control is allowed when the
intervening rendered state changed; labels alone do not trigger a block.
The policy is deliberately conservative and can stop a useful workflow with
little visible feedback. It does not substitute an alternative action or make
an extra model request. Only the separate fixture acceptance checker establishes
task success. The operator report exposes bounded action, transition,
no-change and loop counts with no page text or model output.

The new switchboard layout was introduced after the generic controller change
in this milestone; it exercises a reversible menu/desk cycle and a six-step
completion path. Scripted mock-model decisions on rendered Chromium verify
transport, state comparison, loop refusal and independent acceptance. They do
not prove actual model planning. The real six-choice operator evaluation is
still separately consent-gated. Its procedure remains nonpersistent; existing
Project-scoped versioned procedure/replay contracts were not changed, and
validated procedures remain pending human approval. No PostgreSQL schema or
receipt path changed in V9.5.

The model-call cap stays six, with the existing 60/45/35-second per-call,
70-second worker and 260-second interaction budgets. The reviewed browser
remains in Bubblewrap's private network namespace with a disposable profile
and synthetic origins only. Chromium's internal sandbox is disabled inside
Bubblewrap and no cgroup memory cap has been established; this is not authority
for untrusted external sites. `SAFE_FOR_WORKLOADS=False`.

## V9.4 task-bound synthetic browser actions (2026-10-16)

The operator reported that one actual V9.3 Ollama choice executed and produced
a fresh rendered observation. The second response was schema-valid but failed
`MODEL_CHOICE_TEXT_INVALID`. Its discarded contents do not identify the chosen
control or text. The source-level protocol still required model-generated text
for every choice, including empty text for clicks; that avoidable argument has
now been removed from the model wire contract.

The controller derives complete candidate actions from the current rendered
observation and explicit synthetic task inputs. A bounded `TaskInput` has an
identifier, label and value supplied by the trusted test caller. A permitted
text field yields one candidate per authorized value; buttons and approved
same-origin links yield click candidates. If there is no trusted value, no
fill candidate is issued. The model returns only `revision` and `choice_id`
under a native JSON Schema whose enumerations match the current observation.
It chooses the field/value pair, but cannot create a new value, element,
operation or destination. Page labels and instructions are untrusted context.

The confined worker independently revalidates the input reference and exact
value against the mission payload, then rereads the DOM immediately before
acting. The fresh check now includes origin and element coordinates as well as
the prior type, name, href, input type and enabled/read-only checks. A wrong
but permitted field/value choice can execute and still fail the independent
rendered acceptance check. Rejected choices cause no retry or fallback.
The fixture protocol is synthetic-only: caller-provided task inputs are **not**
a production human authorization mechanism.

The generic action-selection code was committed at `5c84657` before the
`dualfield` fixture was introduced. That later synthetic UI starts with an
irrelevant button and external link, then reveals two inputs requiring distinct
approved values. Scripted mock-model choices completed five actions in actual
Bubblewrap-confined Chromium; swapped field/value choices failed independent
acceptance. Test-only replay verified a versioned procedure using the existing
`text_template` field and source digest. The same test-only procedure was
persisted to a disposable Project-scoped `morgoth_test` database, then recovered
in a fresh process. The operator experiment remains nonpersistent and pending
human approval. This temporal holdout was authored by the same engineer and
does not prove independent model reasoning or blind generalization.

No live Ollama inference ran during development. The separately consented
operator run is the next evidence for whether the installed model can select
successive useful choices. Existing 60/45/35-second model deadlines, 70-second
worker wait, 260-second interaction, six-model-call cap, memory preflight and
330-second outer command remain in place. Browser authority remains limited to
reviewed local synthetic pages in Bubblewrap's private network namespace;
Chromium's nested sandbox is disabled and there is no cgroup memory cap. No
external website, real account or production capability is authorized.
`SAFE_FOR_WORKLOADS=False`.

V9.4 focused browser and protocol tests passed 74 cases with actual confined
Chromium. Two actual disposable PostgreSQL tests passed for the existing
catalog and new two-value procedure, including fresh-process receipt recovery.
One provenance-aware canonical run on committed code SHA
`a1649ad01219fc0561f85e0ef1284f2ff5193256` matched requested/tested SHA
and complete copied-tree manifests: 2,608 passed, 18 skipped, zero
failures/errors, exit 0, wall 57.4 seconds. The owned scope peaked at
776,912,896 bytes and 87 PIDs, with zero OOM kills or PID-limit hits.
The final follow-up commit records evidence only and was not separately
canonical-tested. Canonical browser skips are separate from the focused
actual-Chromium proof.

## V9.3 grounded browser choices (2026-10-16)

The operator reported that V9.2's first actual local-model action arrived as
a schema-valid proposal after 15,439 ms, including 12,784 ms reported load
time and 35 output tokens. The browser refused it as `FILL_TARGET_INVALID`;
zero actions executed. The raw response was discarded, so the exact selected
element is unknown. The source-level defect was broader: the previous wire
schema let the model combine `fill` with any visible element ID, even when the
only first-page affordances were navigation buttons.

V9.3 derives a bounded action menu from rendered visible controls. The
trusted browser observer records element type, enabled/read-only state and
same-origin link target. The controller issues ephemeral `c0`...`c23` choice
IDs for permitted click/fill operations, plus `stop`; it excludes disabled,
read-only, unsupported input types and off-origin links. The model sees a
compact list of permitted operation, role, accessible name and, for text
fields, current value. It returns only `revision`, `choice_id` and `text` via
the existing native Ollama JSON Schema. The schema's choice enum and revision
are generated from the current observation. The model does not issue an
element ID, action kind or destination. Unknown/stale IDs, invalid text and
extra fields fail closed before the worker sees an action.
The input type, disabled state and read-only state are required, strictly
typed worker-observation fields. Missing or malformed affordance metadata
rejects the observation; it cannot default to an executable control.

Conversion still yields the original strict `BrowserAction`. Immediately
before execution, the confined worker rereads the rendered DOM and compares
element type, role, name, destination, value, input type, disabled and
read-only state against its snapshot. The original same-origin and current
element policy remains authoritative. An admissible click can still be
irrelevant; only the actual rendered acceptance element proves task success.
Page prose, including malicious instructions, is never an authority source.

Public evidence separately counts normalized model responses, parsed
candidates, schema-valid candidates, candidates matching an issued choice,
worker policy acknowledgements, post-action observations and independent
acceptance. The worker acknowledges an action only after a fresh rendered
observation. No rejected choice causes an automatic retry or substituted
action. V9.2's 60/45/35-second request, 70-second worker, 260-second
interactive and six-call limits remain unchanged, as do non-generative
resource preflight and privacy restrictions.

The choice-mapping and fresh-DOM policy were committed at `db4f517` before
introducing the relayout fixture. That separately added synthetic page
reorders navigation, places an irrelevant button first, contains a forbidden
external link and exposes two distinct text fields after navigation. The
same controller and scripted mock Ollama response protocol completed it in
four actual Chromium actions. Existing catalog, support and inventory pages
also passed confined rendered tests. These mock responses select candidates
by names visible in each observation; they are protocol tests, **not** live
model reasoning. The fixture was prepared by the same engineer after the
code freeze, so this is a temporal holdout, not an independent blind benchmark.

No persistence code changed. The operator experiment constructs a versioned
procedure in memory and remains pending human approval; it reports
`PROCEDURE_PERSISTED=false`. A source digest mismatch blocks replay on a
different layout. The browser remains synthetic-only inside Bubblewrap's
private network namespace, with Chromium's nested sandbox disabled and no
cgroup memory cap. No real-site, account or production browser authority is
granted, and `SAFE_FOR_WORKLOADS=False`.

V9.3 focused grounded/browser regressions passed 68 tests with actual
Chromium under Bubblewrap. No disposable PostgreSQL run was required because
the persistence path was unchanged. One provenance-aware canonical run on
committed source SHA `575839574470b9d53464fce2a5f649b40d20d814`
verified matching whole-tree manifests and reported 2,604 passed, 15 skipped,
zero failures/errors, exit 0. A later fail-closed review made the new
affordance metadata mandatory rather than defaulted, so a second canonical
run tested corrective source SHA `e1b1a40e6f91161e5250ad3634b36f65cb225d56`.
Requested/tested SHA and complete copied-tree manifests matched; 2,605 passed,
15 skipped, zero failures/errors, exit 0, wall 71.2 seconds. Its owned scope
peaked at 810,438,656 bytes and 88 PIDs, with zero OOM kills or PID-limit hits.
The later documentation-only commit was not separately canonically tested.
Canonical browser skips are not counted as the
actual-Chromium proof. No live Ollama inference occurred in development;
only a separately consented operator run can establish whether the installed
model can choose and complete the multi-step action sequence.

## V9.2 resource-aware browser inference (2026-10-16)

The operator-reported first V9.1 model action timed out before a normalized
response. A rendered page had already been observed; no action was executed.
The discarded private response and later resource snapshots cannot establish
whether loading, queueing, prompt evaluation, generation or another local
condition caused that historical timeout.

The opt-in operator preflight now checks the configured loopback Ollama model
list and [loaded-model API](https://docs.ollama.com/api/ps) without generating
text. It exports only `LOADED`, `NOT_LOADED` or `UNKNOWN`, bounded reported VRAM
size, host available-memory and cgroup headroom. Reported VRAM size does not
prove CPU/GPU placement. Known host memory below 2 GiB or cgroup headroom below
1 GiB blocks before consent. Unknown measurements stay explicit and do not
prove sufficient resources. The development agent's non-inference preflight
could not validate its local Ollama configuration; it made zero model calls.

The [chat API](https://docs.ollama.com/api/chat) exposes total/load duration
and token counts. The existing shared client normalizes those fields, so the
browser report publishes bounded last-request elapsed time, cumulative/max
request time, total/load duration and prompt/output token counts. It publishes
null for separate prompt-evaluation and generation durations because the
current normalized response omits them. A timeout has elapsed-time evidence
but no completed-response durations; prior response metrics are cleared when
the next request starts. Request time can include shared-client queueing and
does not identify a bottleneck by itself.

The model-facing page body was reduced from 600 to 360 characters and labels
from 100 to 80, while retaining every observed actionable ID, name and value.
The strict four-field native JSON Schema, 384-token generation cap, default
model reasoning behavior, no-tools/no-streaming policy and independent action
validation remain unchanged. No alternate provider or automatic retry is used.

The first model request is capped at 60 seconds if `/api/ps` reports it
unloaded or cannot establish residency, 45 seconds if already loaded, and
subsequent requests at 35 seconds. The confined worker waits 70 seconds for
each action, so no permitted request outlives its worker input window. The
parent interactive budget is 260 seconds for at most six calls; it refuses a
new model call unless enough time remains for that call plus five seconds of
confirmation. A reviewed outer 330-second command limit covers preflight,
consent and cleanup. These are finite resource limits, not a claim that the
installed model can complete four actions within them. If it cannot, the next
operator run should report a capacity constraint rather than trigger an
unbounded timeout increase.

The browser remains restricted to one reviewed synthetic app inside its
private Bubblewrap network namespace. A model action is checked against the
fresh rendered observation; the worker acknowledges execution only after an
actual interaction and new observation. Independent UI acceptance, procedure
construction and pending approval remain distinct. The operator-only run
does not persist a procedure and reports `PROCEDURE_PERSISTED=false`.
Chromium's nested sandbox is disabled and there is no cgroup memory cap, so
untrusted external sites remain unsupported. `SAFE_FOR_WORKLOADS=False`.

V9.2 focused tests passed: 52, including actual rendered Chromium in the
private namespace and synthetic slow/timeout/cancellation/resource paths.
The first provenance-aware canonical run on committed SHA `d9b01b7` had
2,593 passed, 10 skipped and one failure: an inherited cleanup test raced
between checking `/proc/<pid>/stat` existence and reading the disappearing
process. This failure is preserved. A narrow test correction accepts only
`FileNotFoundError` or `ProcessLookupError` as already-reaped outcomes; its
direct regression passed. One further canonical run on corrected committed
SHA `376458b` verified equal source/copy tree manifests and reported 2,594
passed, 10 skipped, zero failures/errors, exit 0. This does not prove repeated
runner reliability. Canonical browser skips are not counted as actual Chromium
evidence; the separate focused run supplies that proof. No live Ollama
inference occurred in development.

## V9.1 browser model protocol and evidence (2026-10-16)

The operator reported that V9's first consented local Ollama action attempt
ended as `MODEL_UNAVAILABLE`, with browser isolation established but all later
stage fields false. The private response was discarded. Its exact failure
cannot be reconstructed. Source review did confirm two independent defects:
the adapter combined transport, HTTP, envelope and schema exceptions under
that one code; the operator marked `PAGE_OBSERVED` only after the entire
procedure succeeded, despite receiving a rendered observation before asking
the model for an action.

V9.1 keeps the existing `OllamaLLMClient.chat` transport and native JSON
Schema request. Its small model-facing wire object requires `kind`, `revision`,
`element_id` and `text` as scalars; unused strings are empty. It removes the
previous nullable, conditionally related fields from the generation schema.
The candidate is still converted into the original strict `BrowserAction`,
then checked against the current rendered revision, observed element and
same-origin policy. It is never authority. The bounded prompt contains the
synthetic objective, target, revision, title, at most 600 characters of page
text and the observed controls. The local generation budget is 384 tokens;
tools and streaming remain disabled, and there is no free-text fallback or
automatic retry. [Ollama's structured-output contract](https://docs.ollama.com/capabilities/structured-outputs)
supports sending a JSON Schema in `/api/chat`'s `format` field. Whether this
specific local model can produce the new action object is reserved for the
separately consented operator run.

Failure codes now distinguish connection and transport errors, bounded HTTP
4xx/5xx (including 503), timeout, malformed normalized envelope, absent final
content, truncated generation, JSON syntax, duplicate keys, action schema,
tool activity and browser-policy rejection. `MODEL_FORMAT_UNSUPPORTED` requires
an explicit machine-readable `unsupported_format` error code on HTTP 400/422;
`MODEL_RESOURCE_EXHAUSTED` likewise requires an explicit `resource_exhausted`
code on HTTP 500/503. A bare 400/500 or arbitrary error prose remains its
HTTP status family. Ollama's current local API is not assumed to emit those
optional codes. `HTTP_STATUS` is a bounded
integer or null, and no private response or server error body is exported.
`MODEL_RESPONSE_RECEIVED` counts only a normalized chat envelope; a non-2xx
HTTP response is tracked separately as `HTTP_RESPONSE_RECEIVED`.

The interactive worker now acknowledges an action only after independent
policy validation, actual synthetic interaction and a fresh page observation.
Parent-side stage evidence survives later failure: page observed, model request
attempted, normalized response received, action schema valid, policy accepted,
interaction executed and independent UI acceptance each have separate fields.
The acknowledgement cannot prove external side-effect idempotency. The
operator-only run still does **not** open a Project database, so
`PROCEDURE_PERSISTED=false`; a validated in-memory procedure is pending human
approval but cannot be invoked against a real site. A mocked Ollama response
sequence is tested through actual confined Chromium and the existing
Project-scoped coordinator into a disposable PostgreSQL receipt with fresh
process recovery. That is protocol/persistence evidence, not live-model
semantic learning. The previously verified deterministic procedure remains
separate baseline evidence.

The browser remains limited to reviewed synthetic fixtures inside private
Bubblewrap user/PID/mount/network namespaces. Chromium's internal sandbox is
disabled in that nested environment, and no cgroup memory cap exists. These
controls are **not** a qualification for untrusted external sites. There is no
production browser approval broker, external-origin policy or authority for
real accounts, credentials, uploads, purchases or mutations. The restricted
Codex workload gate remains `SAFE_FOR_WORKLOADS=False`.

V9.1 verification: 110 focused generalist/Ollama tests passed; a separate
actual confined-browser/operator selection passed 41 tests; one disposable
PostgreSQL browser integration passed with fresh-process recovery. The one
provenance-aware canonical run requested and tested clean code commit
`bf00baa1e4c4b39bf83da0e4ba281a4c4749b97b` with equal whole-tree
manifests: 2,583 passed, 10 skipped, no failures or errors, exit 0. Browser
cases skipped by the canonical sandbox are counted only in the separate host
browser run. There was no live Ollama call in development; one operator-native
consent-gated evaluation remains necessary to determine whether the installed
model can actually choose the sequence.

## V9 opt-in synthetic browser procedure lab (2026-10-15)

`agents/generalist_browser_lab.py` is a new **experimental browser modality**,
separate from the generic HTTP GET driver and disconnected from Brain, START,
ToolRouter and production approval. A trusted launcher runs only reviewed local
HTML fixtures under Bubblewrap with private user, PID, mount and network
namespaces. It clears the environment, exposes only system libraries, the
existing Python virtual environment, one reviewed Python worker, one selected
fixture, and a SHA-256-pinned cached Chromium headless shell. `/tmp` is private
and the browser uses a fresh temporary profile. There is no mounted operator
home, browser profile, credential file, upload directory or download target.
The local fixture server and the browser share the private network namespace;
it has loopback but no host or internet route. The worker checks that its
network namespace differs from the launcher, that host-home and `.env` paths
are absent, and that a metadata-address connection fails. Chromium's own
sandbox is disabled for nested execution; Bubblewrap is the enforced boundary.
The reviewed browser binary identity is a hash of an existing local cache, not
an independently established supply-chain attestation.

The generic controller observes rendered visible controls with ephemeral IDs,
accessible labels and bounded body text. It never sends full HTML, scripts,
password/file inputs, hidden fields or coordinates to the proposal source.
`BrowserAction` permits only click, bounded synthetic text entry or stop. Each
action is checked against the current observation revision, visible element
identity and local-origin policy. The browser validates task completion by
inspecting a visible result element, independently of the proposal source's
claim. Browser content remains untrusted; a page instruction cannot add an
action type, destination or permission. The same controller ran four observed
actions on catalog and support fixtures and, after the controller was committed
at `f49e797`, on a third inventory-layout fixture without an app-specific
execution branch. That third fixture was authored by the same engineer after
the freeze, so it reduces sequence contamination but is not an independent
blind benchmark. The deterministic lexical policy, not a live LLM, produced
those measured action sequences.

`OllamaBrowserActionSource` can propose a strict JSON-Schema action from a
bounded observation. The parent-side private pipe keeps Ollama outside the
browser namespace and keeps Chromium inside it; only action candidates cross
into the browser worker. `agents/generalist_browser_operator.py` is an
operator-only, clean-branch and foreground-consent-gated evaluation of one
synthetic app, with six model calls maximum and no third-party API. It has **not
been run**. If the local model fails to plan the observed workflow, the result
remains blocked; fixture-policy success is not model-learning evidence.

The confirmed fixture procedure records source digest, semantic control
preconditions, parameter placeholder, bounded steps, expected visible result
and pending approval. `BrowserMissionCoordinator` claims the Project mission
before browser I/O; a crash after that claim is not replayed automatically.
Only independent UI acceptance causes a capability proposal and atomic local
receipt. A fresh PostgreSQL connection and separate interpreter recovered the
pending mission, exact capability and receipt in disposable `morgoth_test`.
Replay requires an explicit **test-only** authorization flag, exact fixture
digest and fresh semantic preconditions; changed fixture bytes block it.
Neither that flag nor a validated procedure is authenticated production
approval. There is no real-site browser broker, cgroup memory cap, durable
browser-side side-effect idempotency, account identity, upload/download policy
for external sites, or authority to act on external software. The V9 modality
must remain restricted to these reviewed synthetic fixtures.

V9 verification: 127 focused tests passed; one actual disposable PostgreSQL
test passed, including fresh-process recovery. The separate actual-browser
selection used a cached Chromium binary under the new namespace and passed.
The provenance-aware canonical suite tested clean source
`afa3c81adfb6df1e949aa4a9a3a9982a381ebe53` with identical source and
sandbox tree manifests: 2,563 passed, 8 skipped, no failures or errors, exit 0.
The canonical sandbox did not mount the cached browser binary, so that run's
browser skips are not counted as real-browser proof. Historical intermittent
canonical instability remains a separate unresolved concern; one V9 green run
does not establish repeated reliability.

Morgoth's target is a persistent agent that can finish unfamiliar missions by
identifying capability gaps, learning a constrained interface, validating an
implementation, requesting the necessary human grant, then executing and
monitoring it. This document proposes that direction. The current autonomous
runtime is a bounded research system; this branch does not connect the
prototype to autonomous START, add an API, grant permissions, or change the
protected [IDENTITY.md](../IDENTITY.md) or [SPEC.md](../SPEC.md).

## What can be reused now

| Existing contract | Reuse | Gap |
|---|---|---|
| `core/objectives.py` | Persistent objectives with `research`, `capability`, `monitoring`, `optimization` categories and pending/in-progress/done/failed/blocked states | No mission plan, dependency DAG, capability checkpoint, or side-effect receipt |
| `core/brain.py` | Bounded cycle, objective claim/recovery, source-grounded synthesis and thesis gating | Research-specific loop; no general mission dispatcher or capability-acquisition transition |
| `core/scheduler.py` | Persisted one-shot/recurring/triggered tasks and queue | No mission-level dependency or authorization-aware scheduler |
| `core/tool_router.py` | Registered `BaseTool` interface and Domain policy checks | No capability version/approval binding to an invocation |
| `agents/agent_manager.py` | Managed specialized workers and bounded task assignment | No mission coordinator with verifiable delegated outputs |
| `self_modify/{code_writer,code_tester,updater}.py`, `reflect.py`, `shadow.py` | Proposal, testing and human-gated extension patterns | Generated code must never turn its own test result into authority |
| `memory/{persistent,episodic}.py` | Project-local structured and episodic records | No first-class mission/capability provenance and revocation ledger |
| `core/project.py`, `core/domain.py` | One Project storage namespace and pure-data Domain semantics | No per-capability execution profile or versioned service adapter |
| `docs/DESKTOP_RESEARCH_RUNTIME.md` | Native authority, PAUSED/START and Project lease | General mission controls would require separate native and API review |

The current `core/llm/tasks.py` routes CHAT to local Ollama and SYNTHESIS,
THESIS, REFLECT, SHADOW and reserved SCOUT to restricted Codex CLI. Its
`SAFE_FOR_WORKLOADS=False` gate still blocks Codex work. The operator reported
one successful confined **synthetic text-only** qualification on 0.162.1:
exit 0, completed turn, exact response, zero critical/tool/unknown events and
zero observed outside-file effects, with the two reviewed pre-turn advisories.
This is operator-reported evidence, not a live result produced by this branch.
It does not establish task-specific JSON quality, mutable default-model
compatibility, credential confidentiality, or universal sandbox security.

## Target interfaces and authority separation

**Mission Engine.** A mission is an operator-intended outcome with Project ID,
objective category, success criteria, constraints, maximum resource budget and
deadline. A versioned plan contains dependency edges and checkpoints. Each
transition records `mission_id`, previous/new state, cause code, source
provenance and timestamp. Recovery replays only durable verified checkpoints;
it never guesses that an interrupted side effect succeeded.

**Capability Engine.** A capability record binds a name, semantic version,
documentation digest and provenance, explicit input/output schema, required
permissions, validation evidence, isolation profile and revocation state.
States are `PROPOSED → TESTED → PENDING_HUMAN_APPROVAL → APPROVED`, with
`REJECTED`/`REVOKED` terminal branches. A model's description or its own claim
of success is never validation. A version or document digest change invalidates
reuse until revalidated and reapproved.

**Execution Engine.** A broker selected by an immutable capability ID applies
the approved profile for API, file, code, browser or external-app operations.
It receives typed arguments and an operator grant; it returns bounded evidence
and side-effect receipts. Reasoning emits a proposal, never a raw executable
command or permission grant. The broker independently checks Project scope,
target allowlist, resource budget, idempotency key, approval expiry and
revocation before every side effect. Unknown capability types fail closed.

**Memory Engine.** Store factual sources with timestamps and provenance,
procedures with version/test evidence, episodic outcomes with execution
receipts, and mission checkpoints in Project-local namespaces. Retrieval is
context, not authority. Untrusted documentation is data; embedded instructions
cannot alter policies, tool routing or approvals. Secret material stays out of
LLM context and durable diagnostic records.

**Agent orchestration.** A mission coordinator can delegate bounded subtasks
to specialized workers, reconcile their outputs against contracts and limit
concurrency. Delegated claims need independent evidence. Workers cannot
register tools or grant scopes to themselves.

**Governance.** Human Gate 3 remains human-only. Protected core, identity,
permissions and environment remain outside the self-modification pipeline.
Human approval must be an authenticated external record tied to exact
capability digest, Project, scope and expiration; it is not a string in a
model response or a field the agent may set. This branch's fixture grant is
deliberately only an in-memory test simulation and must never be reused as a
production approval interface.

## First executable slice

`agents/generalist_mission_fixture.py` and
`tests/fixtures/generalist_task_service.json` model an unfamiliar local task
service. `python -m agents.generalist_mission_fixture` is an explicit opt-in,
bounded command. It parses a strict data-only local contract, detects a gap,
proposes a capability, validates a fixed adapter on a deep copy, stops at
pending approval, simulates a human fixture grant, resumes from a serialized
checkpoint, sorts synthetic projects and emits two weekly report ticks. The
adapter has no network, disk, account, plugin or generated-code execution.

This proves state transitions and test isolation only. It does **not** persist
checkpoints in PostgreSQL, schedule real recurrence, discover arbitrary docs,
generate an integration, register a `BaseTool`, verify a real human identity,
or exercise an external account. The fixture's Boolean grant is intentionally
forgeable in Python and confers no Morgoth runtime permission. Do not connect
it to autonomous START.

## V2 opt-in persistent slice (this feature branch)

`agents/generalist_capability.py` interprets a strict, data-only subset of
API-style operation definitions: exactly one `list`, `reorder`, and `report`
operation, each bound to a fixed fixture scope and result type. The document
defines service/version, two record field names and expiration tick. It has no
URL, HTTP method, dynamic script, executable code, credential, path, or grant
field. Unknown keys, duplicate JSON keys, extra operations, mismatched scopes,
injection prose and oversized documents are rejected. This is **not** general
OpenAPI support. The same interpreter operates on separate synthetic project
and inventory records; a third service needs a compatible document and local
fixture data, not a service-name branch in the mission engine.

`agents/generalist_mission_engine.py` tracks a separate Mission record and
Capability record. Missions keep Project/user IDs, objective, required and
missing capabilities, step lists, checkpoint, bounded failure codes, resume
eligibility and timestamps. Capabilities bind Project/user, exact version and
documentation SHA-256, operations/scopes, invocation contract, isolated
validation evidence, lifecycle history, compatibility revision, expiry and
test-only approval provenance. A successful copy-based fixture validation
produces `PENDING_APPROVAL`, never `APPROVED`. The only simulated grant is
`TEST_ONLY_APPROVED`; production `APPROVED` has no writer. Revocation and
version/document drift block subsequent invocation. A malformed document or
failed validation leaves a durable safe failure code when storage is available.

`agents/generalist_mission_store.py` adds three **optional** tables in an
existing managed Project PostgreSQL schema: missions, capabilities and
idempotent execution receipts. It uses the initialized `PersistentMemory` pool,
the Project's existing no-public-fallback search path and compare-and-swap
mission revisions. A receipt and checkpoint commit in one transaction. Only
`morgoth_test` is accepted, checked before optional table creation; the demo
also verifies that database name **before** calling `PersistentMemory.initialize()`.
The legacy/default Project is refused. This optional fixture schema is not
added to normal `scripts/init_db.py` bootstrap because doing so would create
generalist tables in live Projects before a production approval contract exists.

`python -m agents.generalist_persistent_demo` requires an explicit disposable
`MORGOTH_TEST_POSTGRES_URL`. It creates two private Project manifests under a
temporary `MORGOTH_HOME`, then runs separate Python processes for prepare,
resume and reuse. The prepare process commits `PENDING_HUMAN_APPROVAL` and
exits. The resume process reads that checkpoint, applies an explicit synthetic
grant, commits an exact-once report receipt and a second periodic tick. A third
process reuses the same exact capability. The command prints only bounded
state/Boolean timeline entries and drops its temporary schemas. The synthetic
operation is deterministic computation over copied fixture records; no external
side effect exists. Thus atomic receipts prove durable fixture idempotency,
**not** safe retries of a real API call. A real broker will need external
idempotency/reconciliation and authenticated human approval before any account
integration.

This slice is not imported by Brain, Scheduler, ToolRouter or Desktop. Research
objectives retain their existing lifecycle. Capability acquisition is a
separate opt-in mission path. No generated code, package installation, real
third-party API, model inference, production database, or production grant is
used. `SAFE_FOR_WORKLOADS=False`; Human Gate 3 is unchanged.

The next separately authorized live Codex acceptance remains a single
source-grounded synthetic structured task in disposable `morgoth_test` with
fallback disabled, exact output schema, source/numeric checks, bounded time,
zero tool events and strict cleanup. It is independent of this fixture
capability path and is **not** executed here. A real generalist connector is a
later milestone after an authenticated approval broker, per-operation scope,
versioned adapter review, non-idempotent recovery design and explicit Project
UI/API contracts are reviewed.

## Recovery and observability contract for a future implementation

Persist a mission transition and its idempotency key before external work.
Persist the broker's receipt after work. On restart, reconcile uncertain
operations with the external system under the same grant; never blindly replay
a non-idempotent action. Store bounded status/error codes and source IDs rather
than raw provider output or credentials. Monitor in-process liveness separately
from expensive provider readiness. Block a mission on missing capability,
expired grant, revoked version or unverifiable side effect, and expose the
specific safe reason to the operator.

## Migration and acceptance milestones

1. **This branch:** fixture-only acquisition, approval boundary, reuse and
   resume tests; no runtime wiring.
2. Review a persistent Project-local mission/capability schema and the
   authenticated native approval contract. Decide revocation, expiry,
   idempotency and cross-Project isolation before database or API changes.
3. Add one real, low-risk read-only connector behind the reviewed broker,
   isolated tests, and an explicit human grant. Demonstrate interruption and
   recovery with an external fixture service before any account integration.
4. Qualify restricted Codex output for a separately authorized disposable
   `morgoth_test` structured task. Require exact model/metadata drift policy,
   source-grounded schema acceptance, zero fallback/tool events and cleanup.
5. Only after the above, consider a general mission dispatcher and Desktop
   visibility. Autonomous capability installation and privilege self-escalation
   remain forbidden.

The first live Codex workload proposed by the current provider plan remains a
single fabricated THESIS extraction in disposable `morgoth_test`: one prompt,
two fabricated sources, fallback disabled, bounded time, schema and numeric
fidelity checks, explicit abstention accepted, exact child cleanup. It requires
separate human authorization and does not run in this milestone.

## Major risks and required controls

Prompt injection in documentation; malicious or stale schema; generated-code
escape; credential exposure; confused-deputy Project crossing; unsafe retry of
non-idempotent actions; forged approvals; model hallucination; service version
drift; runaway budgets; memory poisoning; and agent-to-agent authority
laundering are distinct failure classes. Each needs a structural boundary and
adversarial test. A successful synthetic turn cannot retire them. The current
Codex process can read its narrow read-only `auth.json` mount and shares host
networking; neither is credential or network isolation. Ollama remains the
local CHAT/tool-loop provider; Claude and API keys are not target dependencies.

## Reviewable protected-file drafts — do not apply automatically

**IDENTITY.md draft intent:** “Morgoth is a persistent general-purpose agent
that pursues operator-defined missions through evidence-grounded planning,
bounded capability acquisition and verified execution. Its current deployed
ability remains research; broader execution requires a separately approved
capability and permission profile. Morgoth cannot alter its own protected
core, identity, permissions, secrets, approval gates or human Gate 3.” Retain
the existing no-trading and source-grounding constraints until a human reviews
each replacement clause.

**SPEC.md draft intent:** add versioned Mission, Capability, Grant, Execution
Receipt and Checkpoint contracts; define reasoner/broker separation, Project
isolation, per-operation approval, revocation, recovery and budget semantics.
Mark current research features as the first Domain implementation, not the
entire product identity. Reconcile the legacy Phase 1 self-modification prose
with today's human-controlled protected-zone policy. These are proposed text
directions only; neither protected file is changed here.

## V3 opt-in public read acquisition (2026-10-14)

`agents/generalist_orchestrator.py` supplies one driver-neutral, opt-in mission
interface: receive, inspect inventory, discover a gap, parse a data-only
contract, validate against an isolated response, wait for a caller-supplied
exact grant, claim a step, execute and commit a receipt. It works with the
existing task and inventory fixture driver and a separate public Issues driver;
there is no service-name switch in the orchestrator. The existing research
Brain, scheduler, ToolRouter, Project bootstrap and Desktop are untouched.
Only managed disposable `morgoth_test` Projects can use the optional store.

The new narrowly reviewed [GitHub Issues list operation](https://docs.github.com/en/rest/issues/issues#list-repository-issues)
is a **manually extracted subset**, not arbitrary OpenAPI learning: GET on
`api.github.com`, `/repos/{owner}/{repo}/issues`, `state=open`, `per_page<=5`,
and exactly number/title/state/created/updated result fields. The checked-in
contract names the public `octocat/Hello-World` repository. GitHub's Issues
list can include pull requests; the adapter filters entries with a
`pull_request` key. The contract records the authoritative documentation URI,
its own SHA-256 digest, version, operation, scope and expiry tick. A changed
document/version requires new validation and an exact new grant. Documentation
and issue titles remain untrusted data; no instruction or authority is taken
from either. The isolated validation fixture proves parser behavior, **not**
live response fidelity or connectivity.

The HTTP adapter is GET-only, fixed-host HTTPS, no redirect following, no
credentials/cookies/proxy environment, one request, eight-second timeout and
128 KiB decompressed response cap. It handles 401/403/404/429/5xx and invalid
JSON/schema with fixed error codes and never returns raw server error bodies.
It rejects private DNS answers before connecting, but the preflight is not
atomic with the TLS connection; the fixed hostname and TLS certificate check
remain the primary destination binding. There is no generic URL executor.
No shell, browser, plugin or generated code is installed or run.

The orchestrator's Project/revision CAS records
`EXTERNAL_OPERATION_ATTEMPTED` **before** calling a driver. Only a confirmed
result and mission completion commit atomically with the local receipt. A crash,
timeout, invalid response or failed receipt transaction leaves an attempted
mission that is not automatically replayed. The local receipt does not prove
exactly-once remote execution; a future mutating adapter needs reviewed remote
idempotency and reconciliation. A concurrent second claimant is rejected.
Receipt recovery is tested after a fresh pool and a separate-process mocked
demo. Reuse of the exact capability version is tested; revocation and stale
document/version block subsequent invocations.

`InvocationGrant` is an exact Project/capability/version/digest/scope/expiry
constraint supplied by the **trusted opt-in caller**. It is not an authenticated
human approval record and is forgeable by arbitrary Python code. The one
explicit public GET authorized for this development task can use it only in a
disposable test Project. The production `APPROVED` state has no writer and no
autonomous runtime imports this path. Before a real user-selected capability
could be activated, add a separate authenticated human approval broker and
bind grant issuance to Project, operation, destination, budget, expiry and
revocation. No test-only grant should cross that boundary.

`python -m agents.generalist_public_read_demo --mock-public-read` exercises
the full prepare/execute/recover sequence in separate child interpreters and
removes the temporary Project schema. `--live-public-read` is an explicit
single public GET mode with the same isolation and bounded safe output. This
development environment could not resolve `api.github.com`; therefore **real
network proof is blocked by environment**. The mocked and disposable SQL
proofs must not be described as a live API success.

The next separate Codex task acceptance remains a single synthetic,
source-grounded structured extraction in disposable `morgoth_test`, with
fallback disabled, bounded output, strict schema/source checks, no tool events
and explicit authorization. It is independent of the public read prototype.
`SAFE_FOR_WORKLOADS=False` remains unchanged. Future browser, CLI/code,
file/document and desktop modalities need separate brokers with the same
versioned contract, Project scope, grant and receipt semantics; this GET
adapter does not provide those capabilities.

**Protected-file update proposal (not applied):** amend `IDENTITY.md`'s
research-primary description to define an operator-directed generalist mission
agent while explicitly retaining source grounding, immutable core and human
approval. Amend `SPEC.md` to name Mission/Capability/Grant/Attempt/Receipt as
separate versioned concepts and require authenticated approval plus remote
idempotency/reconciliation for mutating operations. Keep no-trading and human
Gate 3 rules until independently reviewed.

## V4 disposable authorization and one public read (2026-10-14)

The V3 `InvocationGrant` remains usable only for local fixture operations.
`PublicIssuesDriver` now requires `ProjectApprovalBroker` and a persisted grant
UUID; a caller-constructed dataclass cannot authorize its external GET. The
broker is deliberately **test-only**: its optional table is provisioned only
inside a managed Project schema after verifying the actual database is
`morgoth_test`. It is not an API, a production credential issuer, a Brain tool,
or a Human Gate 3 replacement. The operator command obtains explicit consent
through the existing foreground controlling-terminal check. The local OS
session/UID and private disposable database form this proof's trust boundary.
A same-UID process with test-database access could forge rows; a production
approval service still needs authenticated operator identity, protected grant
issuance and a reviewed audit channel.

Each grant binds a unique approval UUID, Project, mission, capability/version,
documentation digest, operation, fixed `GET`/`api.github.com`/public Issues
endpoint, `state=open` and `per_page<=3`, one `public_issues.read` scope,
operator-session provenance, a maximum 120-second lifetime, one invocation,
128 KiB response budget, eight-second total deadline and revocation flag.
The broker locks the grant and capability rows, rechecks the exact contract and
validation state, then spends the grant and writes the mission's `ATTEMPTED`
checkpoint in one SQL transaction. Concurrent claimants, expired/revoked or
drifted grants, stale mission revisions and changed capabilities cannot send a
request. Revocation linearizes against claim; it cannot retroactively stop a
request already claimed. A crash after claim or after receiving a response
without a local receipt leaves an uncertain attempt that is never auto-replayed.
The atomic local receipt remains distinct from remote exactly-once execution.

The fixed HTTP reader now accepts only public `octocat/Hello-World`, at most
three returned entries, and an eight-second total DNS/connect/read deadline.
It validates the HTTPS hostname with the normal trust store, inherits no proxy
environment, sends no cookies or authorization, follows no redirect, and caps
decoded content at 128 KiB. GitHub's [Issues list contract](https://docs.github.com/en/rest/issues/issues#list-repository-issues)
includes pull requests; they are excluded from the issue report. Explicit
401/403/404/429/5xx, timeout, redirect and schema refusals remain fixed codes.
The DNS public-address preflight and subsequent HTTPS connection are two
separate resolutions; a DNS change between them could point the socket at a
private address, although TLS hostname validation still applies. We have not
proved an atomic IP/hostname binding. Arbitrary hosts and URLs remain
unsupported. Direct DNS in this development environment failed, so **no live
GitHub GET** is claimed here.

`agents/generalist_contract_extraction.py` now parses a tiny, bounded local
machine-readable GET subset for collection and detail shapes without service
branches or executable hosts. It rejects instructions, servers, POST, path
traversal, duplicates and unsupported schemas. This demonstrates transferable
**contract extraction from supplied documentation**, not autonomous document
discovery or automatic network authority. The GitHub executable contract is
still the separately reviewed, manually bound adapter; extracted contracts
cannot add a destination or grant themselves approval. The task-management
and inventory fixture services continue through the same mission engine.

The operator-only command `python -m agents.generalist_operator_public_read
--one-public-get` verifies a clean origin-equal reviewed branch, creates an
owned private PostgreSQL 16 cluster and Project, asks one exact terminal
consent, makes at most one public GET, verifies the receipt in a new process,
then stops/removes its owned resources. It prints only fixed verdicts, count,
hash and recovery Boolean. Both its main and internal recovery paths verify
the actual database name is `morgoth_test` before initializing
`PersistentMemory`; unexpected errors become fixed public verdicts. Do not
run it in an unattended agent terminal or
substitute a pseudo-TTY. Its authorization does not make a capability
production executable.

Before the first real user-approved capability, add a production-authenticated
human grant boundary, an OS-enforced destination binding or independently
reviewed restricted network egress, durable operator audit/revocation, and
remote idempotency/reconciliation for mutating operations. Browser, CLI/code,
file and desktop modalities each require separately reviewed execution
brokers. `SAFE_FOR_WORKLOADS=False`; no research or Codex workload activation.

## V5 supplied-document generic GET acquisition (2026-10-14)

The V4 public read was reported by the operator, not repeated here:
`PUBLIC_READ_CONFIRMED`, one request, two issues, fresh-process receipt
recovered, `PRODUCTION_APPROVAL=false`, response SHA-256
`4ee918a0925299beb38d254621d13ef74326cb603c91ec64bfa8ef8d86b8964c`.
It remains evidence for the fixed GitHub adapter, not a live proof of V5.

`agents/generalist_generic_read.py` now extracts one operation from a strict,
data-only OpenAPI **3.0.3 subset** supplied as a local document: a collection
GET with a bounded object array or a detail GET with typed path parameters.
Required query/path inputs are bounded strings, integers or Booleans; returned
objects project at most twelve typed fields from at most ten collection rows
and 128 KiB of JSON. Duplicate keys, extra document instructions, unsupported
schema features, unbounded arrays, unsafe paths, HTTP writes and redirects
are refused. The raw documentation SHA-256, source identity, service identity,
capability/version and operation identity are distinct. This is deterministic
extraction from **supplied** documentation, not autonomous discovery or
general OpenAPI support.

The same `DeclarativeHttpReadDriver` validated and executed two previously
unregistered synthetic APIs: an album collection with integer query limit,
and an artifact detail with a string path identifier. No service-specific
Python driver was added. The generic driver currently requires an exact
`httpx.MockTransport`; it cannot make a live network request. Its reviewed
request object is supplied by trusted test code, separately from the document,
and binds host, concrete endpoint, query, operation and scope. The existing
Project SQL broker now compares that reviewed request to the persisted
capability and one-use grant, atomically spends the grant and records
`ATTEMPTED` before mock I/O. Both services recovered their committed receipts
in a fresh interpreter and reused the compatible capability on a later
mission with a new grant. Changed documentation/version, operation, host,
scope, query, revocation, forged grant UUID and wrong receipt grant are blocked.
The same-UID disposable-test broker is still **not authenticated production
approval**. A local atomic receipt still cannot establish remote exactly-once
execution.

The previous three nonfatal `self_modify_proposals` bootstrap warnings have a
specific ordering cause: `memory/persistent.py` attempts three optional
`ALTER TABLE self_modify_proposals` statements before its later
`CREATE TABLE IF NOT EXISTS` on a fresh schema. The table is absent at that
point. This protected storage file was left unchanged; a future narrowly
reviewed migration-order correction can eliminate the warnings without
altering the generalist test-only schema.

For a **future**, separately consented real-world transfer, the official
[Open Library Works API](https://openlibrary.org/dev/docs/api/books) documents
read-only JSON works, e.g. `GET /works/{work_id}.json` on `openlibrary.org`.
An operator-selected work ID such as `OL45804W`, an independently reviewed
destination/response contract, a production-authenticated grant boundary,
and a network destination binding are required before enabling a live generic
transport. No Open Library request ran here. Open Library usage guidance also
asks clients to keep API use low volume; consult its
[developer guidance](https://openlibrary.org/developers/api) during review.

The opt-in mission interface remains `receive → assess → execute`. The research
Brain, Scheduler, tool router, local Ollama, protected identity/specification,
Human Gate 3 and Codex workload gate are unchanged. The generic modality
demonstrates declarative API learning and mock execution only. Browser, file,
CLI and desktop modalities remain unimplemented; neither an untrusted document
nor a model can issue a production grant or expand network access.

V5 verification on code SHA `5259eaadc1af88cf84859199ff73d0158c9776e8`:
123 focused no-network tests and 24 owned disposable PostgreSQL tests passed.
One canonical run verified that requested=tested SHA and the complete copied
tree manifest matched; 2,415 passed, 2 skipped, zero failures/errors, exit 0.
The historical intermittent canonical-runner failures are not thereby proven
resolved. No live third-party call or model inference occurred in V5.

## V6 reviewed Open Library transfer candidate (2026-10-14)

The next opt-in path uses the **same** `DeclarativeHttpReadDriver`, OpenAPI
subset parser, Project mission store and exact-operation SQL approval broker.
The official [Open Library Books API](https://openlibrary.org/dev/docs/api/books)
identifies the Works JSON example `/works/OL45804W.json`. A committed,
operator-reviewed offline excerpt records that canonical source identity and
its own SHA-256. A separate, digest-checked data-only contract proposal binds
the selected work ID, `GET`, `openlibrary.org`, one concrete endpoint, the
`title` field and optional bounded `key`/`first_publish_date` fields. Unknown
response fields are discarded. The source digest is included in the persisted
capability/grant digest; changing the snapshot or proposal invalidates reuse.
The excerpt is **not** a fetched live document or independent API-behavior
proof. Prose-to-contract extraction remains a reviewed proposal boundary,
not autonomous semantic learning. Document instructions cannot mint grants
or drive code execution.

`PinnedPublicTransport` is the only newly permitted non-mock transport for
this generic driver. It is pinned to reviewed `httpx 0.28.1` and `httpcore
1.0.9` internals and fails closed on version drift. The one-host backend
resolves `openlibrary.org` once, rejects the entire answer if any address is
nonpublic, dials one checked IP and retains the original hostname for TLS SNI
and certificate verification. It refuses another host, method, port, Unix
socket, request attempt, credentials and proxy use. The client has no proxy
environment inheritance, redirects or retries. A hard eight-second total
timeout and 128-KiB JSON cap apply; compressed responses are refused before
decoding. DNS-to-connect rebinding through
a second hostname resolution is removed for this path; shared host networking
and ordinary public-network availability remain. No credential is needed or
forwarded. The operator workflow makes at most one GET, below Open Library's
[default one-request-per-second unidentified limit](https://openlibrary.org/developers/api),
and does not scrape or bulk-fetch.

The disposable `agents.generalist_operator_read_acceptance` command verifies
the pushed clean feature branch, exact fixture digests and reviewed request,
creates its own private `morgoth_test` PostgreSQL cluster/Project, validates an
isolated sample, and stops at `PENDING_HUMAN_APPROVAL`. Only foreground
controlling-terminal consent issues a one-use, 120-second, Project/mission/
capability/version/source-digest/operation/destination-bound grant. The broker
atomically consumes it with the `ATTEMPTED` checkpoint before external I/O.
The generic driver then validates the response and commits a receipt. A fresh
interpreter recovers that receipt; a later mission can select the same compatible
capability only with a **new** grant. All disposable files and SQL resources
are removed. An ambiguous post-claim failure is not replayed. This local
same-UID operator grant is not production-grade authentication, and a local
receipt does not prove remote exactly-once execution.

The operator-only real GET is **prepared, not executed in development**.
Offline mock/SQL evidence does not imply that Open Library currently returns
the proposed schema, that the endpoint is reachable from ordinary WSL, or
that Morgoth is production qualified. No Open Library live response, Codex
inference or autonomous task ran. `SAFE_FOR_WORKLOADS=False`; Brain,
Scheduler, Ollama routing, protected identity/governance and Human Gate 3 stay
unchanged. Browser, CLI, file and desktop capabilities still need separately
reviewed brokers and permission contracts; one supported API does not prove
universal software learning.

V6 validation: 147 focused no-network generalist tests passed (22 integration
tests deselected); 25 selected generalist tests passed against an owned,
disposable `morgoth_test` PostgreSQL cluster. One provenance-aware canonical
run tested code SHA `0a3f2db933668b3f1f5831c063d1824eb5d9523d` and an
equal complete copied-tree manifest: 2,435 passed, 2 skipped, no failures or
errors, exit 0. The development sandbox could not create a PostgreSQL Unix
socket or a systemd cgroup, so these checks used approved local-process access;
neither touched production state. The historical intermittent canonical
failures are not disproved by one pass.
The final operator command's Git remote check uses no stdin and disables Git
terminal prompts while retaining existing SSH configuration. That small
follow-up has focused negative tests and was not included in the
canonical-tested code SHA.

## V7: opt-in mission-driven documentation selection (2026-10-15)

The operator reported a V6 Open Library result of
`LIVE_PUBLIC_READ_CONFIRMED`: one generic public GET, a schema-valid result,
verified documentation digest and fresh-process receipt recovery; response
SHA-256 `1f8295bba7533eb01dc40ba7564c255b54fa68fa6cfa1b65ec8fd1b0d671a6`.
That result was **not reproduced in V7**. The operator's consent was for that
one GET, not blanket destination approval.

`agents/generalist_mission_planner.py` is a narrow, opt-in planning adapter
over the existing Project mission store and orchestrator. It accepts a bounded
natural-language objective, persists `MISSION_RECEIVED`, chooses a unique
relevant source from a reviewed local catalog, checks the offline snapshot and
OpenAPI-subset candidate against independent content digests, and verifies
that the selected operation and typed inputs match the separately reviewed
request. The existing driver validates a mocked response and records the
versioned capability as `PENDING_HUMAN_APPROVAL`. Checkpoints retain the
catalog ID, source identity and digest, replay time, contract digest, bounded
outcome/criteria/uncertainty, and the existing capability identity. Crash
recovery reopens that checkpoint; a compatible validated capability is reused
on a later mission. No catalog item, document, fixture planner or model
proposal can mint an approval or turn an endpoint into an executable host.

The fixture planner performs **lexical catalog matching** and mechanically
extracts one supported GET from a pinned candidate. It is independently
automated documentation-to-contract selection within a tiny supplied catalog,
not general semantic understanding or autonomous web discovery. Two missions
use the same code: public bibliographic metadata from an operator-reviewed
Open Library snapshot and a held-out synthetic transit route/status document.
The latter is fixture evidence, not a real service observation. Ambiguous or
unsupported missions remain blocked with a fixed reason. The local
`OllamaProposalSource` adapter is prepared but has **not** been run against a
live model. Its JSON output is strict, bounded and independently checked
against catalog relevance and operation identity. It receives only a synthetic
mission and catalog summaries in the optional operator probe, no document
body, tool schema, credentials or execution authority. A model-generated
proposal would still require exact schema validation and permission review.

`python -m agents.generalist_discovery_demo` demonstrates the complete
fixture-only path in an owned disposable `morgoth_test` cluster: planning,
offline validation, pending permission, explicitly simulated **test-only**
approval, one mocked GET, durable receipt, fresh-interpreter recovery and
capability reuse. It performs zero external network requests. The simulated
grant is not an authenticated production operator grant. A real external GET
still requires a separately reviewed destination policy, foreground local
operator consent and the existing SQL broker. An atomic local receipt does not
establish remote exactly-once behavior. The generalist planner is not
registered with Brain, Scheduler, ToolRouter or autonomous START.

The optional single-call `agents.generalist_ollama_plan_probe` requires a
foreground local terminal and typed consent. It uses a synthetic mission,
loopback Ollama, no tools, a 20-second client bound, fixed proposal metadata
only, and no external service request. Its output can demonstrate one actual
model-generated *candidate*; it does not approve execution or complete a
mission. The existing Ollama client logs request payloads at DEBUG, so this
dedicated probe removes its process-local Loguru sink before invoking it.
No live Ollama call was made in this development milestone.

Offline snapshots are replayed; the stored replay timestamp is **not** an
original retrieval timestamp. Online documentation retrieval, independent
semantic extraction from prose, production-grade human approval, browser/file/
CLI/desktop modalities, natural-language completion verification and durable
multi-step planning remain future work. The host and endpoint review in this
fixture is data-only and not a network permission. The protected identity,
specification and human Gate 3 stay unchanged. `SAFE_FOR_WORKLOADS=False`.

The three nonfatal `self_modify_proposals` warnings observed while initializing
a brand-new disposable Project arise because `memory/persistent.py` attempts
three optional ALTERs before its later CREATE. That engine is protected in this
phase; no migration or production schema was changed. A separate reviewed
small migration-order correction would be appropriate if those warnings need
removal. They did not invalidate the disposable SQL result.

V7 verification: 167 no-network generalist tests passed (23 integration tests
deselected), and 23 selected tests passed against one owned disposable
`morgoth_test` PostgreSQL cluster (3 nonintegration tests deselected). The
bounded offline demo passed with one mock request, zero network requests,
pending-state and receipt recovery in distinct interpreters, and later reuse.
The canonical source-verification result is recorded in the V7 progress entry.

A post-canonical review found and corrected a misleading inventory label. The
planner now performs a bounded Project-scoped SQL inventory query **before**
calling a proposal source; an exact existing capability is checked against the
catalog's validated contract identity. Reuse clears the capability-gap field,
while a missing capability records the gap only after its required operation
is identified. The first canonical result on `c05d771` predates this
correction; the corrected source requires its own canonical evidence.

Corrected V7 candidate `178dcfa78c2fede7da6ddb99dafa763d411bc4f9`
passed one provenance-aware full canonical run. Requested/tested SHA matched;
the complete copied-tree manifest matched
`557b32e63d4b322f2d50832f683a239ea2eec0a633f3087796fbf9e6397d0880`.
The result was 2,459 passed, 2 skipped, zero failures/errors, exit 0,
62.9-second runner wall. Owned scope peak was 808,579,072 bytes and 95 PIDs,
with zero OOM kills or PID-limit hits. This single pass does not resolve the
historical intermittent-runner concern. The final evidence commit changes
documentation only.


## V7.1 local semantic proposal contract (opt-in)

The first operator-consented Ollama run ended at `MODEL_PROPOSAL_BLOCKED`.
Its private response was discarded, so the actual failed stage **cannot be
recovered**. Two source defects are confirmed independently: the model was
asked for an `operation_id` absent from its catalog choices, and a model
selection was compared to an unrelated lexical keyword selector. Neither
fact alone proves the cause of that historical run.

The opt-in proposal source now supplies only catalog ID, reviewed description,
digest-verified operation ID, operation shape and result field names. It never
passes documentation bodies, hosts, URLs, executable instructions or approval
data as model directions. For model planning, a separately supplied trusted
`MissionRequirements` record defines the requested result shape and required
schema fields. The independent check verifies the model's catalog and operation
against the pinned document and exact reviewed request, then requires those
fields in the supported response schema. The fixture proposal source remains
explicitly lexical. Passing this check establishes bounded schema feasibility,
not that the model understood all semantic nuance or acquired permission.
Unsupported, ambiguous or misleading choices remain blocked. A valid candidate
can enter the Project-scoped pending-approval checkpoint; it cannot execute.

The operator probe checks loopback configuration and installed-model listing
before any generation. It reports fixed failure classes with separate
`INFERENCE_ATTEMPTED`, `RESPONSE_RECEIVED` and independent-validation Booleans.
It accepts only one complete, tool-free, strict JSON response with no duplicate
keys or extra proposal fields. The opt-in bound is 90 seconds and 640 generated
tokens; this allows more cold-load and reasoning time than the former 20-second,
320-token contract, but is not a claim about the configured model's speed.
Ollama documents an API `format` field for JSON/schema output; Morgoth's
existing shared client does not expose that field, so this branch retains
strict parsing and does not change the protected shared client. A reasoning-only
response with no final text is classified as incomplete; an explicit
`done_reason=length` is classified as a generation-budget failure. See
https://docs.ollama.com/capabilities/structured-outputs and
https://docs.ollama.com/api/chat for the upstream protocol.

The actual operator acceptance still requires one foreground `PLAN ONCE`
consent. It performs no external API request, capability execution, Project
mutation or production activation. A successful run would prove one local
model-generated candidate for the tested mission only. In this development
environment the reviewed local config passed, but the configured loopback
socket was unreachable; no live Ollama inference was attempted. The disposable
PostgreSQL demo reported `DISPOSABLE_POSTGRES_UNAVAILABLE`, so this branch does
not claim a fresh SQL integration pass. `SAFE_FOR_WORKLOADS=False` remains.

Candidate `9c5a7845bfa11b514819115be048c442059523fd` passed one
provenance-aware canonical run: requested/tested SHA and complete copied-tree
manifest matched; 2,499 passed, 2 skipped, zero failures/errors, exit 0.
Owned scope peak was 778,608,640 bytes and 87 PIDs, with zero OOM or PID-limit
events. This is one clean run, not a repeated-reliability claim.

## V7.2 opt-in Ollama schema contract (2026-10-15)

The V7.1 operator attempt reached the configured local model and returned a
response, but strict decoding classified it `INVALID_JSON`. Its private output
was discarded; its exact syntax, reasoning behavior and cause are unknown.
V7.2 sends `PlanningProposal.model_json_schema()` as Ollama `/api/chat`'s
optional top-level `format` only for the opt-in planning request. Normal CHAT
and research requests retain their existing payloads. The schema requires all
five fields, forbids extra fields and bounds string and criteria-array lengths.
The request still has no tools, is nonstreaming, has a 90-second limit and a
640-token generation cap. Ollama's structured-output API supports a JSON Schema
in `format`, but support by the operator's particular model is not yet proven.
The optional model-specific `think` parameter is omitted: Ollama documents
`/api/show` as the way to discover supported values, and the development
sandbox cannot reach the configured loopback service. A reasoning-only reply
without final content remains blocked; there is no unrestricted fallback.

Syntax, duplicate keys, Pydantic schema, reviewed catalog/operation identity,
mission/result-shape compatibility and authority are separate checks. The
operator report now exposes fixed Booleans for these stages, without model
text, reasoning or local configuration. A known exact Ollama HTTP 400 format
error maps to `MODEL_FORMAT_UNSUPPORTED`; other 400 responses remain generic
service failures, with private bodies neither logged nor published. A
schema-valid proposal never grants permission or performs a capability action.
The final operator check remains one foreground `PLAN ONCE` request; no
production gate changes, external API calls or autonomous START are permitted.

Candidate `f1cc40b97803fbfac3ef4741a59a914c070a6b40` passed one
provenance-aware canonical run with matching source SHA and whole copied-tree
manifest: 2,510 passed, 2 skipped, zero failures/errors, exit 0. The final
evidence commit only updates documentation. No live model request was made in
this development milestone, and `SAFE_FOR_WORKLOADS=False` remains.

## V8 bounded mission-driven discovery (2026-10-15)

The operator reported one V7.2 local Ollama `MODEL_PROPOSAL_VALIDATED` result:
valid JSON/schema, reviewed catalog operation, independent mission compatibility,
no grant or execution. This is one model-generated selection from a two-item
reviewed catalog, not evidence of open-ended documentation discovery.

`agents/generalist_discovery.py` adds an opt-in, restart-safe acquisition path.
A bounded `DiscoveryIntent` proposes an outcome, information fields, search
terms, missing capability and uncertainty without seeing catalog document or
operation IDs. The operator-supplied `MissionRequirements` remain a separate
acceptance oracle. The reviewed offline index exposes short descriptions and
terms only; search returns at most three metadata hits, retrieval verifies
source/contract digests and stays within a 49,152-byte total budget. The
supported OpenAPI 3.0.3 GET subset is parsed deterministically, checked
against independent reviewed destination binding and response fields, tested
against an isolated mock response, persisted by Project-scoped CAS checkpoints,
and left at pending approval. An interrupted interpretation claim can be
reclaimed at a later tick once; same-tick workers cannot issue two model calls.
After two uncompleted claims, planning blocks. No real external operation is
started by this path.

The held-out corpus was added after the initial generic discovery engine commit
`0278e2a`; the later optional model choice stage was added with the held-out
evaluation. This is temporal separation for the initial engine, not a frozen
final-code blind evaluation. Its poisoned `decoy` document ranks above the relevant
bibliographic document on lexical overlap, yet the separately supplied
`detail/title` requirement selects the compatible document. A different
`collection/level` mission selects an unfamiliar reservoir contract with the
same code; a `detail/status` mission finds two compatible documents and blocks
as ambiguous. These are fixture-intent demonstrations, not live semantic-model
proof. Their result depends on the independent oracle and the quality of the
bounded search terms. Documentation search is confined to an operator-reviewed
offline corpus, not unrestricted web discovery. Malicious snapshot prose is
never promoted to instruction or authority. Prose-to-contract drafting is not
accepted as executable in V8: exact source quotations alone cannot establish
that a proposed API schema or destination is correct. The first executable
path remains a machine-readable, tightly supported OpenAPI subset.

An opt-in `generalist_discovery_probe` offers at most two foreground-consented
local Ollama requests: intent/search-term proposal, then a choice among only
the short metadata of the search hits. It supplies the mission and empty
capability inventory, not the answer document or operation ID, and emits only
fixed status/Boolean fields. The second choice is untrusted: a chosen decoy
is blocked by the independent oracle rather than automatically accepted.
The development session did not run it. A positive operator result would prove
one model-generated intent and independently validated offline acquisition
candidate, not broad semantic competence, browser procedure learning or a
production permission grant.

The first future browser/UI modality needs a reviewed observation format,
origin-bound navigation policy, state/action schema, isolated replay fixtures,
explicit operator-granted action scopes and durable side-effect receipts. It
must not treat retrieved page instructions as authority. The V8 intent and
retrieval boundary can be reused, while its HTTP GET contract parser and
executor cannot validate browser actions. `SAFE_FOR_WORKLOADS=False`, human
Gate 3 and all existing provider restrictions remain unchanged.

## V8.1 semantic intent repair (2026-10-15)

The operator's first V8 local Ollama attempt made one intent request and
returned `INTENT_SCHEMA_INVALID`; its private response was discarded. Its
specific defect is unknown. Source review found a separate, reproducible
contract defect: `uncertainty` permitted one character in Pydantic but the
secondary validator required eight. Abstention also required invented search
and API-field claims. Neither finding proves the historical response contained
that defect.

`DiscoveryIntent` now describes the desired outcome, information need,
conceptual result shape, short natural-language search phrases, missing ability,
completion criteria and uncertainty. Only `abstain` is structurally required;
`{"abstain":true}` is a valid abstention. A non-abstaining but empty object is
schema-valid and separately blocked as `INTENT_INCOMPLETE`. There is no second
length or identifier validator contradicting the generated Ollama JSON Schema.
Technical result-field names come solely from the independently supplied
`MissionRequirements` and the retrieved, digest-checked OpenAPI-subset contract.
The model never supplies an operation ID, destination permission or grant.

The intent parser distinguishes syntax, duplicate-key, missing-field,
wrong-type, constraint, extra-field, incomplete-content, timeout and service
failures with fixed codes. No private response or raw exception is emitted.
Checkpoints store fixed statuses, trusted catalog identities and bounded
counts/Booleans, rather than the model's outcome, search text, criteria or
uncertainty prose. Search deterministically tokenizes at most four bounded
phrases, returns at most three index hits and does not treat rank as authority.
The optional second Ollama call sees only those short hit summaries; source
hashes, exact destination binding, independent contract compatibility and
isolated validation still decide whether a capability can remain pending.

The V8.1 fixture tests cover a differently worded bibliographic goal, a
collection result, a misleading lexical-top decoy, ambiguity and valid
abstention. They use programmed intent/choice responses; they are not proof of
real model understanding. The foreground-consented operator probe now reports
separate intent, search-hit, retrieval, extraction, validation and approval
stages. Its mission contains no document or operation identifier, it allows
at most two local tool-free model calls, and it never executes a capability or
third-party request. The corpus remains operator-reviewed and offline; prose
to executable contract, unrestricted web discovery and production approval
are still unsupported. `SAFE_FOR_WORKLOADS=False` remains unchanged.
