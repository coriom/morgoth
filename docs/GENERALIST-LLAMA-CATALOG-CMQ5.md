# CMQ.5: Llama catalog decision and complete mission

## Question and evidence boundary

CMQ.5 starts from `9ea1b2aa89cdfafa8fa02096336a5c30d7d001fd` on
`morgoth-generalist-cmq4-checkout-accounting-fix-v1`. It asks whether the
already installed `llama3.1:8b` can (A) choose the useful catalog search
control after the trusted work code is filled, and (B) complete one fresh
catalog lookup under independent rendered-UI acceptance. These are separate
outcomes. The direct controller, its prompt and choice schema, action policy,
loop rules, six-call mission ceiling and normal inference deadlines are
unchanged.

The operator-supplied CMQ.4 result is one valid but unproductive Qwen choice,
zero browser missions and zero competitor requests. Its original `/tmp` ledgers
are absent in the available environment; this does not erase the reported
inference. CMQ.4 remains consumed and `EXECUTION_DISABLED=True`. CMQ.5 has a
distinct fixed protocol identity and its own explicit consent and accounting.
It is **not** a paired replay: the historical Qwen request bytes are unavailable.

## Reviewed experiment

The `catalog` fixture is SHA-256 pinned at
`8c1512fdc1ce80102b346b66848d2acb30fdb255af2b05a9e41ad491bcad06f9`.
Its trusted target is `BK-17`; the independent expected rendered answer is
`The Atlas of Rivers`. The action model receives the objective, target,
bounded rendered observation, issued choices and verified history. The answer
is supplied only to the browser acceptance worker.

Stage A scripts two controller-valid **preparation** actions in confined
Chromium: open the catalog section and fill the work-code field. They are not
model decisions. The existing `OllamaBrowserActionSource` builds the third
request from the resulting rendered observation. A synthetic Stop captures
that request without a third interaction. The digest covers its messages,
schema and options. The reviewed mapping identifies Find work as `C04`; the
model's choice is neither forced nor executed in Stage A. A valid other choice
is retained as an unproductive decision, while technical qualification is
judged separately by the existing structured-response, latency and resource
assessment.

Only a technically and resource-qualified Stage A can reach Stage B. Stage B
uses the existing direct operator controller and starts a **new** confined
browser from its initial page. The model selects every scored action. The
worker's source-verified rendered trace and independent task checker are both
required before reporting `SYNTHETIC_MISSION_COMPLETED`. Scripted preparation
and deterministic fixture preflight cannot count as model success.

## Model identity, load and resource contract

`llama3.1:8b` must be installed with Q4_K_M quantization and digest
`46e0c10c039e019119339687c3c1757cc81b9da49709a3b3924863ba87ca666e`.
The development read-only `/api/tags` snapshot matched these fields and listed
four installed models. Identity, residency, memory, swap, cgroup and GPU
preconditions are remeasured at operator time. The snapshot does not prove
future headroom or model competence.

If Llama is not resident, a separate exact `LOAD LLAMA ONCE` consent allows at
most one empty-prompt `/api/generate` request over the validated loopback
endpoint. It uses `stream=false`, per-request `keep_alive=10m`, no proxies or
redirects, a 4 KiB response cap and a 120-second experimental **load-only**
deadline. The response must report the named model, completion, empty text and
zero generated/prompt tokens; generated text or ambiguous envelopes block the
experiment. Load, timing, sampled memory/VRAM and OOM evidence are recorded
separately from semantic inference. Post-load model identity, residency and
resource preflight run again. If already resident, there is no preload.

`MODEL ONCE LLAMA` consent precedes any operational claim. The existing Stage B
path separately requires `BROWSE ONCE`, plus the existing memory-pressure
acknowledgement when its measured warning applies. A refusal before the first
operation leaves a durable cancellation receipt and no consumed claim. There
is no automatic retry, warm-up, model switch, unload, service restart or
provider fallback. Normal semantic deadlines remain 60 seconds cold, 45
seconds first loaded and 35 seconds later; browser worker wait is 70 seconds,
interactive mission limit is 260 seconds. The outer CMQ.5 bound is 540 seconds.
The semantic ceiling is one Stage A plus six Stage B requests, seven total.

The source and fixture, exact clean branch and live remote HEAD, candidate
identity, resource guards and scripted confined-browser acceptance are checked
before consent. A fresh resource gate runs before the scored mission. Unknown
required resource evidence, OOM, hard pressure, incompatible identity or
failed confinement refuse execution. Chromium remains synthetic-only in a
private Bubblewrap network namespace. Its internal sandbox is disabled there;
there is no demonstrated cgroup memory cap, so this is not external-site
qualification.

## Durable accounting and privacy

The experiment uses a fixed directory outside Git:
`/home/corio/.local/state/morgoth/experiments/cmq5-llama-catalog/`.
It checks owner, 0700 directory mode and symlinks; files are 0600, bounded,
atomically replaced and fsynced. One exclusive `campaign.claim` is independent
of commit SHA. Preload and semantic calls are reserved before dispatch, then
completed counts and bounded results are persisted. If an outcome is unknown,
the reservation remains and the claim blocks replay. A new commit or process
does not reset the experiment. Reports omit prompts, page text, raw model
responses, reasoning, credentials and arbitrary paths. No production database
or capability is written.

## Verification and readiness limits

Focused regression run on the development worktree:

```text
/home/corio/Morgoth/morgoth/.venv/bin/python -m pytest -q \
  tests/test_generalist_llama_catalog_cmq5.py \
  tests/test_generalist_mission_qualification_cmq4.py \
  tests/test_generalist_compact_model_qualification.py \
  tests/test_generalist_compact_model_direct_baseline.py \
  tests/test_generalist_browser_operator.py \
  tests/test_generalist_cmq4_checkout_failures.py
125 passed, 1 skipped
```

The two CMQ.5 rendered-browser tests initially failed with
`INTERACTIVE_WORKER_FAILED` under the development sandbox's namespace
restriction. The identical tests passed (2 passed) under the reviewed
confined-browser permissions. They establish rendered-state provenance and
fresh catalog acceptance with scripted decisions. Hermetic tests exercise
identity and consent refusals, empty-prompt load parsing, generated-content
rejection, preload uncertainty, durable reservations, crash behavior, choice
quality versus technical qualification, and independent acceptance. No actual
preload, live inference or real-model browser mission was run during engineering.

An operator should first run `--readiness` on the **final clean pushed SHA**,
then, only if ready, run `--run-once` in an ordinary foreground WSL terminal.
The latter asks for the exact load and inference phrases as applicable, then
Stage B's separate browser consent. Use a bounded shell timeout longer than
the internal 540-second deadline to leave cleanup time. The exact pinned
commands and SHA belong in the final engineering handoff, because a commit
cannot accurately embed its own SHA.

One correct choice is not mission completion. One accepted synthetic mission
is not statistical reliability, learned procedural skill or permission for
production workloads. `SAFE_FOR_WORKLOADS=False` and Human Gate 3 remain in
force.
