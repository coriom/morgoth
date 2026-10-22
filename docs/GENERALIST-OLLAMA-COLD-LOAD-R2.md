# R2: correlated cold-load experiment

This is an isolated, operator-consented diagnostic. It does not change the
browser controller, the normal 60-second first-call deadline, the configured
model, or production authority. No live inference was performed in development.

## Why this experiment exists

The operator reported a request from 03:45:33.703 to 03:46:33.956 UTC on
2026-10-22, with runner start at 03:45:34.220, a load phase at 03:45:36.730,
and a combined load-failure/cancellation log at 03:46:34.138. The final log
was about 182 ms after client exit. No complete response arrived. Neither
service-cgroup OOM nor a CUDA allocation error was established. Before/after
GPU samples are not peak measurements. These facts support a cancellation-
associated final failure but do not explain the preceding 60 seconds.

The probe discriminates a response before or after the normal boundary, a
time-ordered runner failure, a reported ready runner followed by incomplete
inference, observed resource pressure, and unresolved timeout. It cannot
prove causality from shared service logs without a request identifier.

## Contract and ceilings

- The reviewed branch, literal commit SHA, local origin-tracking SHA and clean
  worktree must match. The probe does not contact GitHub; delivery separately
  verifies live remote equality. The configured model must be `llama3.1:8b`
  with context 4096 at a validated loopback Ollama URL.
- Preflight uses only local TCP, GET `/api/tags`, GET `/api/ps`, fixed service,
  guest, cgroup and GPU reads. It requires an installed but nonresident model,
  no other resident model, an active service, at least 2048 MiB guest memory
  available and 512 MiB GPU memory free. A finite cgroup requires at least
  1024 MiB headroom; unknown required measurements block the request.
- A foreground controlling terminal must receive exactly `MODEL ONCE
  EXTENDED`. Refusal, redirection, CI or background execution sends no POST.
- One tool-free, nonstreaming `/api/chat` POST uses the existing browser
  action-proposal JSON Schema, a synthetic two-choice observation, temperature
  zero, `num_predict=384`, context 4096, no proxy, no redirects and no retry.
  The response is capped at 32 KiB and discarded after strict validation.
- The experimental request deadline is 120 seconds; the wrapper deadline is
  123 seconds, HTTP read phase is 125 seconds, and the outer foreground shell
  deadline is 300 seconds including preflight, consent, cleanup and journal
  classification. The browser's regular 60-second deadline is unchanged.
- Up to 26 in-memory resource observations occur at five-second spacing.
  Each has UTC and monotonic elapsed time; delayed samples never trigger a
  burst. The journal reader is limited to the exact request interval plus
  one second before and two seconds after, 1000 records, 2 MB, eight seconds,
  and 64 classified events. A 0.5-second settling interval permits a late
  cancellation record to appear. Only fixed event codes and timestamps leave
  the classifier.

Samples include `/api/ps` listing and reported model VRAM, fixed `nvidia-smi`
GPU used/free memory, guest available memory, swap free, memory-pressure
average, Ollama service memory and service cgroup OOM counters. The report
contains observed extrema and timestamps; absent readings remain null. A
model absent from `/api/ps` may still be loading. GPU used/free is aggregate,
not model placement or evidence of successful generation. Memory-pressure
thresholds are diagnostic flags, not OOM proof. A service log cannot be
conclusively assigned to this request if unrelated clients were active.

## Interpretation

`COLD_LOAD_COMPLETED_WITHIN_NORMAL_DEADLINE` and
`COLD_LOAD_COMPLETED_AFTER_NORMAL_DEADLINE` require a complete normalized
response and are based on client monotonic elapsed time. A malformed action
can still prove transport completion but is separately marked as a protocol
failure; it does not qualify browser planning. A post-60-second completion
shows that the normal deadline was insufficient in this trial only.

`RUNNER_FAILURE_BEFORE_CLIENT_CANCELLATION` requires a classified failure
strictly before client end, without a cancellation marker in that record, a
complete untruncated journal window and no detected UTC/monotonic clock
discontinuity. It is temporal evidence, not definitive request attribution.
`MODEL_READY_BUT_INFERENCE_STALLED` likewise requires an affirmative ready
event before an incomplete timed-out response. `RESOURCE_PRESSURE_OBSERVED`
requires a measured low resource or rising service OOM counter; it does not
identify which resource caused an inference failure. A timeout without such
independent evidence is `EXTENDED_PROBE_TIMEOUT_UNRESOLVED`. Preflight,
transport and protocol failures remain `PROTOCOL_OR_PREFLIGHT_BLOCKED` with
a fixed failure code. Multiple evidence classes may be reported together.

No raw prompt, response, hidden reasoning, journal message, configuration,
credential, URL from page content, process command line or private path is
printed or persisted. Only the reviewed local Ollama endpoint is contacted.
The probe does not warm or retain the model deliberately. It cannot measure
peak pressure between five-second samples, prove CPU/GPU offload fractions,
or determine other clients' inference state. A timeout remains unresolved
unless independently earlier failure or readiness evidence is captured.

## Verification and operator handoff

The R2 implementation was developed from backend commit
`2f810b9ef71d70c16b076d46873c418e9f1e7732`. The final pushed SHA is
provided in the engineering handoff; this file intentionally avoids a
self-referential commit hash. Hermetic tests cover one-request enforcement,
strict schema and transport, consent and resource refusal, cancellation,
bounded sampling and extrema, journal classification, privacy and cleanup.
The final focused command was
`/home/corio/Morgoth/morgoth/.venv/bin/python -m pytest -q tests/test_generalist_ollama_cold_load_r2.py tests/test_generalist_ollama_reliability.py tests/test_generalist_ollama_runner_diagnostic.py`:
47 passed, exit 0. Python compilation and `git diff --check` passed.
The R1 branch's six `INTERACTIVE_WORKER_FAILED` confined-browser tests remain
an independent open qualification issue. R2 does not change browser execution
and does not claim those tests fixed. No PostgreSQL path changed; no canonical
suite is warranted for this opt-in experimental module and narrow diagnostic
helper changes.

The final handoff supplies one command with its literal pushed SHA. Its form is
shown here; substitute only the handoff SHA, not another branch or model:

```bash
cd /home/corio/Morgoth/dev-morgoth-generalist-ollama-cold-load-r2-v1 && timeout --foreground --signal=INT --kill-after=10s 300s env MORGOTH_LOCAL_CONFIG=/home/corio/Morgoth/morgoth/.env /home/corio/Morgoth/morgoth/.venv/bin/python -m agents.generalist_ollama_cold_load_r2 --probe REVIEWED_PUSHED_SHA
```

Run it only from an ordinary foreground WSL terminal after reviewing this
contract. It will prompt for `MODEL ONCE EXTENDED` and make at most one local
request.
Do not run it to warm the model first. The simplest next remedy depends on
the measured result: investigate runner initialization if an earlier failure
appears, investigate resource placement if contemporaneous pressure appears,
consider a separately reviewed timeout policy only if complete inference is
observed after 60 seconds, and otherwise retain the unresolved diagnosis.
