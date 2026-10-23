# CMQ.2: direct-controller baseline for Qwen3 4B

This isolated experiment starts from CMQ.1 commit
`2887165866844d26bca1cb30a46e35c6eb0e40e2`. It runs exactly one
operator-consented direct-controller trial against the same reviewed
synthetic `heldout` inventory fixture and independent UI acceptance checker
as the preceding hierarchical trial. It does not alter either planner's
prompt, browser execution policy, fixture, configured default model or
production permissions.

## Recorded hierarchical evidence and accounting

The operator reported six completed model requests, four valid acknowledged
actions, one visible no-change action and no independent task acceptance:
`CLICK C02` changed the UI; `FILL C03` changed it; a second `FILL C03`
produced no observable change; `CLICK C04` changed the UI and revealed C05.
The trial ended `MODEL_CALL_BUDGET_EXCEEDED` before a C05 interaction.
Total model-request elapsed time was 30,617 ms, maximum 21,586 ms.

The source counts every high-level subgoal request and every low-level action
request against one six-call ceiling. It clears the advisory subgoal after a
verified `NO_OBSERVABLE_CHANGE`. Four actions plus six responses are
consistent with four low-level action requests and two high-level requests;
the repeated fill could have caused a replan before the C04 choice. The
historical public report omitted layer counts, so **2+4 is an accounting
hypothesis, not measured historical telemetry**. Future hierarchical CMQ
reports now include the already maintained `HIGH_LEVEL_CALL_COUNT`,
`LOW_LEVEL_CALL_COUNT`, `SUBGOAL_REVISION_COUNT` and fixed
`SUBGOAL_STATUS`, without generated subgoal text.

The no-change fill was executable but ineffective. Its controller-confirmed
effect invalidated high-level advice; it was not task progress. C04 then
revealed C05, but a newly visible control is not independent completion.

## Direct experiment contract

The new `agents.generalist_compact_model_direct_baseline` entrypoint calls
CMQ.1's reviewed Stage A continuation without repeating Stage A inference.
It binds the selected `qwen3:4b-instruct` Q4_K_M model to artifact digest
`0edcdef34593eac1aa2be9c7d06c432dcf81945adca5eca2f27662c18f168ba0`.
The historical partial PSI stall remains a warning. It requires a separate
foreground `MEMORY PRESSURE REVIEWED` acknowledgement, exact clean source
and live-origin equality, fresh loopback model/digest and resource checks,
and the unchanged P1 browser and memory preflight before the separate
foreground `BROWSE ONCE` consent. Any failed gate stops before inference.

The operator is called with `hierarchical=False`. This selects the existing
`OllamaBrowserActionSource`: one schema-constrained action choice per model
call, no high-level subgoal request. The task is unchanged: look up SKU-44
and independently verify the expected 12-unit result in rendered Chromium.
The controller still issues dynamic choices tied to current observation,
binds trusted input values, validates the fresh DOM, suppresses proven
ineffective choices in the exact visible state, detects loops, and allows
at most six total model calls. Browser work remains in Bubblewrap's private
network namespace against the reviewed local fixture. There is no
deterministic fallback, scripted operator sequence, external site or
production capability execution.

The direct result includes model calls and responses, schema and choice
counts, acknowledged actions, no-progress and loop counts, safe SHA-pinned
synthetic action trace, bounded inference latency, observed resource state,
and independent acceptance. If the source digest check fails, the trace is
redacted. The verdict distinguishes independent completion, decision-limited
behavior, and resource/protocol failure. `PRODUCTION_QUALIFIED` remains false.

## Comparison limits and next decision

The hierarchical trial and the future direct trial are separate,
non-randomized operator runs. Model residency, resource contention and UI
timing may differ. A direct success would support that direct selection is
more call-efficient on this four-interaction fixture; it would not prove
hierarchy is generally inferior. Another sequence of legal but ineffective
choices would indicate a shared decision-quality problem. A timeout,
invalid protocol response or sandbox failure cannot be compared as planning
quality. One success is not a statistical estimate or production approval.

Hermetic tests cover the 2+4 accounting possibility, exact direct dispatch,
consent order and fail-closed model/resource gates. A scripted model client
reached independent acceptance in actual confined Chromium despite one
ineffective repeated fill. This is protocol and browser evidence only; no
real Qwen inference ran in development. No database or deployment path
changed, and a canonical runtime suite is unwarranted for this isolated
operator adapter and safe-output projection.

## Single operator command

After publication, replace `REVIEWED_PUSHED_SHA` with the literal final SHA
from the engineering handoff and run from an ordinary foreground WSL
terminal. The entrypoint itself verifies the exact clean branch, local
tracking ref and live origin HEAD. It makes at most six local model requests
only after both foreground consent gates:

```bash
cd /home/corio/Morgoth/dev-morgoth-generalist-compact-model-direct-baseline-v1 && timeout --foreground --signal=INT --kill-after=10s 360s env MORGOTH_LOCAL_CONFIG=/home/corio/Morgoth/morgoth/.env /home/corio/Morgoth/morgoth/.venv/bin/python -m agents.generalist_compact_model_direct_baseline --expected-sha REVIEWED_PUSHED_SHA
```

The Git checkout preflight reads remote branch metadata. Browser navigation
remains local to the reviewed disposable fixture. No model is downloaded,
switched globally, warmed automatically, or run concurrently.
