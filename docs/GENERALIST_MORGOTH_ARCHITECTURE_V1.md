# Generalist Morgoth architecture V1 — reviewable target, not runtime authority

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
