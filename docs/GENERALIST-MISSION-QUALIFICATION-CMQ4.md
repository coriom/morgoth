# CMQ.4: paired choice and complete synthetic missions

## Scope and evidence boundary

Base: `fd98627c28de1b6b012dc5d2e8c60d02f98e7c0b` on
`morgoth-generalist-compact-model-decision-triage-v1`. This branch adds an
opt-in experiment around the existing direct controller. It does not change
the direct prompt, action meaning, six-call limit, loop policy, suppression,
browser isolation, configured model or normal inference deadlines. The one
CMQ.1 helper change accepts an explicit fixture and task for its already
reviewed Stage B gate; existing callers retain their defaults.

The earlier operator-reported Qwen direct and hierarchical missions failed
independent acceptance. CMQ.3 showed that the current post-fill low-level
request includes the filled value, verified history and available search
action. Exact historical requests were discarded. A new correct frozen-state
choice will be a narrower observation, **not** mission completion. Only the
confined worker's independent rendered result establishes a completed mission.

## Frozen matrix and candidate selection

The campaign fixes this order before any real request:

| Block | Choice-only | Scored fresh-browser missions |
| --- | --- | --- |
| Qwen | heldout post-fill | heldout, catalog, relayout |
| Competitor | the identical heldout post-fill request | heldout, catalog, relayout |

The three trusted tasks are: `SKU-44` / `12 units` on heldout, `BK-17` /
`The Atlas of Rivers` on catalog, and `SKU-44` / `12 units` on relayout.
Their objectives are task-appropriate and their expected answers are sent
only to the independent browser worker, never to the action model. Fixture
bytes are SHA-checked against the reviewed trace manifest. The relayout
distractor field remains present. `heldout` is a known regression fixture;
catalog and relayout are also reviewed fixtures, not blind data.

The installed inventory is read from the configured validated loopback
`/api/tags` without generation. Qwen must match its exact supplied digest
and Q4_K_M quantization. The competitor rule is fixed in code: among
installed non-Qwen `llama` family Q4_K_M models with complete bounded name,
size and digest metadata, choose the smallest artifact, then sort by name.
This family and quantization had prior structured browser-action evidence;
the rule avoids making a trial inference to select a candidate. On the
development host's read-only metadata snapshot, it selected `llama3.1:8b`
at digest `46e0c10c039e019119339687c3c1757cc81b9da49709a3b3924863ba87ca666e`.
This identity must be reverified at operator time. Installation is not
evidence of current resource fitness or task competence. Its approximately
4.6 GiB artifact may fail the existing host/cgroup/GPU headroom guard.
At one non-generative development snapshot, both models were unloaded,
`OTHER_LOADED_MODEL_COUNT=0`, guest available memory was about 6.4 GiB,
swap free about 3.1 GiB, GPU free about 5.3 GiB and the cgroup memory limit
was reported unlimited. The existing resource predicate returned true for
both *at that instant*. These volatile readings do not qualify either future
probe or any browser mission; every real cell repeats its own preflight.

If no alternative satisfies this fixed rule, all cells remain reported as
blocked; there is no download, replacement candidate or silent change.
After a model's own choice-only request, the mission stage requires a
complete schema-valid response within the normal first-call deadline and
the existing CMQ.1 sampled resource assessment. A technically valid but
unhelpful choice still permits the missions. The competitor never inherits
Qwen's historical Stage A attestation or measurements. Unknown required
sample minima or OOM counters block its missions.

## Faithful paired request

The preparation path uses actual Bubblewrap-confined Chromium. Two scripted
controller-valid actions open the heldout section and fill the trusted
target. The worker acknowledges both and returns a fresh rendered
observation. These are preparation actions, not scored autonomous decisions.
The third call goes through the unchanged
`OllamaBrowserActionSource.propose` request construction with a mock client;
its exact system/user message objects, schema, tools-disabled setting,
nonstreaming flag, temperature and token budget are captured once in memory.
That mock returns Stop, so no third browser action executes. The SHA-reviewed
diagnostic verifies the post-fill request has the expected four remaining
contextual calls, visible filled target, verified fill effect, current search
candidate and schema/choice equality. One SHA-256 digest covers the exact
source-built request fields. Both real choice requests reuse those same
objects; only `model` differs. The shared loaded configuration also supplies
the same Ollama `num_ctx` for both. Ollama may apply different internal model
templates; the digest cannot establish template equality. The contextual
remaining time is a frozen snapshot, while real transport deadlines and
request counts are enforced separately.

No raw request, page text, response or model reasoning is written to the
ledger or stdout. The paired result includes only SHA, model identity,
reviewed synthetic control code, strict-format status, bounded timing and
resource summaries. The reviewed `C04` search choice is counted as useful
for this frozen state, but it is never executed in Part A.

## Real-run authority and attempt accounting

`--prepare` performs checkout, installed metadata, fixture preflights and
the scripted rendered-state capture. It makes **zero** real model requests.
`--run` repeats those gates, creates a mode-0600, no-overwrite attempt ledger
under `/tmp/morgoth-cmq4-<reviewed SHA>.json`, and cannot be rerun for that
source SHA. The ledger stores only bounded fixed-status facts. It reserves
one request before each choice-only call and up to six before each mission;
after a normal return it replaces the reservation with the actual dispatched
count. A crash leaves an explicit unknown reservation and prevents an
accidental replay. It never claims reserved requests were actually sent.
An unexpected browser-wrapper failure explicitly marks the call count
unknown and retains the six-call reservation; it cannot be recorded as zero.
Two probes plus six missions at six calls each form a 38-request ceiling,
19 per model. Timeouts, malformed replies and other dispatched requests
count. There is no warm-up, retry, JSON repair, fallback or extra planning
request.

Each model probe requires foreground `MODEL ONCE COMPACT` consent. After the
first model block, foreground `CONTINUE MODEL BLOCK` explicitly controls the
transition; it is not inference consent. If Qwen remains loaded, the normal
guard blocks the competitor. The operator may choose a separate, deliberate
residency intervention in another terminal before continuing; the campaign
does not unload or restart a service. Every scored mission then passes a
fresh digest/resource/OOM and confined-browser preflight, requires separate
`BROWSE ONCE` foreground consent, and starts in a fresh worker/browser state.
If measured PSI pressure is a warning, the existing separate
`MEMORY PRESSURE REVIEWED` acknowledgement is also required. A failed hard
resource, other-loaded-model, identity, sandbox or consent gate blocks.

The direct controller retains 60 seconds for a first cold call, 45 seconds
for a first loaded call, 35 seconds subsequently, 70 seconds worker wait,
260 seconds per interactive mission, and six model calls per mission. The
choice-only request uses the same first-call deadline. The outer shell
limit in the handoff is 2400 seconds, including human transition and cleanup.
No R2 extended diagnostic timeout enters this campaign.

## Reporting and interpretation

Every scheduled cell is emitted as recorded or blocked with a fixed reason.
The report separates preparation, consent, resource/identity, timeout or
transport, invalid response, valid but ineffective choice, loop, budget,
safe abstention and independently accepted mission. A valid schema, control
choice or acknowledged action cannot set production qualification.
Comparisons are paired for the frozen source request only. Missions are
sequential, not randomized or matched for residency, contention or latency;
one run cannot establish statistical superiority or general autonomy.

## Operator commands

Replace `FINAL_PUSHED_SHA` with the literal final commit from the engineering
handoff. Run these from an ordinary foreground WSL terminal. The module
checks local HEAD, origin tracking and live remote branch equality and a
clean tree. The first command performs no inference:

```bash
cd /home/corio/Morgoth/dev-morgoth-generalist-mission-qualification-cmq4-v1 && env MORGOTH_LOCAL_CONFIG=/home/corio/Morgoth/morgoth/.env /home/corio/Morgoth/morgoth/.venv/bin/python -m agents.generalist_mission_qualification_cmq4 --prepare --expected-sha FINAL_PUSHED_SHA
```

Only if preparation reports ready, the single-run experiment is:

```bash
cd /home/corio/Morgoth/dev-morgoth-generalist-mission-qualification-cmq4-v1 && timeout --foreground --signal=INT --kill-after=10s 2400s env MORGOTH_LOCAL_CONFIG=/home/corio/Morgoth/morgoth/.env /home/corio/Morgoth/morgoth/.venv/bin/python -m agents.generalist_mission_qualification_cmq4 --run --expected-sha FINAL_PUSHED_SHA
```

The run may block before or during the competitor block if the previous
model remains resident or fresh headroom is inadequate. It does not
automatically unload the earlier model. If intervention is considered, the
operator must review effects on other local users and perform it separately
before typing `CONTINUE MODEL BLOCK`. A blocked or interrupted campaign is
not silently resumed; a new reviewed experiment would be needed.

No real inference or real-model browser mission was executed during CMQ.4
engineering preparation. Actual qualification remains pending operator
execution and independently observed mission acceptance. Desktop,
PostgreSQL, protected governance, Human Gate 3 and
`SAFE_FOR_WORKLOADS=False` remain unchanged.

## Engineering verification

The focused test selection exercises CMQ.4's frozen request, strict parsing,
identity and consent refusals, resource/timeout accounting, confidentiality,
scenario wiring and the unchanged compact/browser regressions. Actual
Bubblewrap-confined Chromium verifies three fixture preflights, the rendered
post-fill request and independently accepted scripted missions. Scripted
decisions prove the controller and harness only. The test count and exit
status are recorded here and in the final engineering handoff: the following
selection returned **154 passed, 0 failed, 0 skipped, exit 0** in 76.30 s
on the CMQ.4 source worktree before commit. It was run with permitted local
user namespaces so Bubblewrap confinement remained intact:

```bash
/home/corio/Morgoth/morgoth/.venv/bin/python -m pytest -q tests/test_generalist_mission_qualification_cmq4.py tests/test_generalist_compact_model_qualification.py tests/test_generalist_compact_model_direct_baseline.py tests/test_generalist_browser_decision_diagnostic.py tests/test_generalist_browser_operator.py tests/test_generalist_browser_hierarchy.py tests/test_generalist_browser_goal_directed.py tests/test_generalist_browser_grounded.py tests/test_generalist_browser_closed_loop.py tests/test_generalist_browser_benchmark.py
```

No PostgreSQL or backend canonical suite is required: no database path,
browser controller, core client or production runtime was changed. The
CMQ.1 continuation receives bounded task parameters while its existing
callers retain exact default behavior.
