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
