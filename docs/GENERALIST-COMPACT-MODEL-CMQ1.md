# CMQ.1: compact-model pressure triage and conditional Stage B

This is an opt-in experimental continuation of commit
`e4424df2d29ae28172896d13dd0ece2fbe484b4a`. The normal model,
browser controller, six-call budget, 60/45/35-second model deadlines,
global configuration, and production authority are unchanged.

## Confirmed finding

The operator reported one Qwen3 4B Q4_K_M structured reply in 49,537 ms,
including 45,038 ms of model loading. Its response was complete and schema
valid. Sampled minima were 2,191 MiB GPU free, 6,113 MiB guest available,
and 1,746 MiB swap free. The Ollama service cgroup OOM count stayed at zero;
its ending OOM-kill count was zero. The maximum system-wide `memory some`
10-second average was 17.68%. The historical sample did not include `full`
or cgroup-local PSI; these values are **unknown**, not zero. The historical
result is operator-reported and was not reproduced by development tests.

The original R2 diagnostic classifier emits `RESOURCE_PRESSURE_OBSERVED`
for any one of these conditions: GPU free under 512 MiB, guest available
under 1,024 MiB, swap free under 128 MiB, system-wide memory `some avg10`
at least 10%, or a rising Ollama service cgroup OOM count. The compact-model
Stage A gate rejected that whole class. For this reported trial the supplied
values isolate the 17.68% `some` reading as the observed trigger. The refusal
was conservative, but it conflated measured partial stalls with exhaustion.

[Linux PSI documentation](https://docs.kernel.org/accounting/psi.html)
defines `some` as at least some tasks stalled, while `full` means all
non-idle tasks stalled simultaneously. A `some` average is contention
evidence, not an OOM event or proof that the model caused it. Even `full`
is a performance-stall measurement, not by itself an OOM diagnosis.

## Experimental rule

Stage A now separately requires one complete schema-valid response within
60 seconds, known GPU/guest/swap minima above the existing R2 floors, known
service OOM evidence with no increase, and no other failure classification.
Unknown required extrema or counters block. The legacy R2 class is accepted
only when the resource summary independently explains it as PSI warning;
an unexplained `RESOURCE_PRESSURE_OBSERVED` still blocks. `memory some >=10%`
or measured `memory full >0%` remains visible as
`RESOURCE_PRESSURE_WARNING`; it is **not** treated as a clean run.

The experimental collector now records bounded system-wide `memory full`
and, when available, cgroup-local memory `some` and `full`. Missing PSI
stays `null`. No arbitrary system-wide `some` value is declared safe.

For the single reviewed Stage B handoff, the code binds the operator-reported
Stage A summary to the exact Qwen artifact SHA-256
`0edcdef34593eac1aa2be9c7d06c432dcf81945adca5eca2f27662c18f168ba0`.
This summary is historical evidence, not a token or permission grant. It is
not re-inferred. Before Stage B the operator must type
`MEMORY PRESSURE REVIEWED` on a foreground controlling terminal. The harness
then rechecks the exact clean checkout against live origin, installed model
digest and quantization, loopback service, GPU and guest/cgroup headroom,
and swap. It refuses unknown or below-floor swap, any newly observed service
OOM count, and system-wide or local `full avg10 >=10%` using the established
R2 contention threshold as a conservative stop. Fresh `full` can be unknown
if the kernel does not provide it; the report says `null` and the hard
headroom/OOM gates still apply. The existing P1 operator path independently
rechecks model resources and confined Chromium before asking for its separate
foreground `BROWSE ONCE` consent.

If either consent is declined or any fresh check fails, no browser inference
runs. The reviewed command performs no Stage A inference, retry, model
switch, download or warmup. Stage B can still fail because of model decision
quality, cold reload, resource contention, browser isolation or six-call
exhaustion. A valid action does not prove mission success. Only P1's
independent rendered-state checker can establish this synthetic task's
completion; it cannot qualify production use.

## Verification and boundaries

Hermetic tests exercise PSI parsing, warning/hard/unknown classification,
warning consent order, fresh hard refusals, exact digest and checkout checks,
and no browser entry after a failed gate. R2's legacy diagnostic class remains
unchanged so historical reports remain interpretable. The existing P1
hierarchical Chromium file was run under a permitted local namespace: its
scripted decisions exercised four synthetic fixtures and safety cases. Those
tests prove controller behavior, not actual Qwen planning. A nested
development sandbox run first returned six `INTERACTIVE_WORKER_FAILED`
cases; the same unchanged file passed when local namespace creation was
permitted. Neither outcome is a live-model result.

No PostgreSQL path or core runtime was changed. A canonical suite is not
needed for this isolated diagnostic harness and read-only sampler. The
operator's next run, if separately approved, is the first actual Qwen
hierarchical Stage B decision-quality test under CMQ.1.

## One operator command

Use the literal pushed commit SHA from the engineering handoff in place of
`REVIEWED_PUSHED_SHA`. The module itself verifies exact clean local HEAD,
local tracking ref, and live GitHub origin HEAD. It performs non-generative
resource and model checks before either consent. Run it from an ordinary
foreground WSL terminal:

```bash
cd /home/corio/Morgoth/dev-morgoth-generalist-compact-model-pressure-v1 && timeout --foreground --signal=INT --kill-after=10s 360s env MORGOTH_LOCAL_CONFIG=/home/corio/Morgoth/morgoth/.env /home/corio/Morgoth/morgoth/.venv/bin/python -m agents.generalist_compact_model_qualification --reviewed-stage-b --expected-sha REVIEWED_PUSHED_SHA
```

The checkout check reads the GitHub branch HEAD through Git; the browser
remains restricted to one local synthetic fixture and no external website.
The command makes at most six local model calls **after** both human consents.
The 360-second outer limit
includes the existing 260-second interactive browser limit and cleanup.
The result remains `PRODUCTION_QUALIFIED=false` regardless of task outcome.
