# Compact local-model qualification

**CMQ.1 update:** The original Stage A pressure flag grouped partial PSI
stalls with measured resource exhaustion. The isolated CMQ.1 branch separates
these outcomes and provides a reviewed Stage B-only continuation; see
[GENERALIST-COMPACT-MODEL-CMQ1.md](GENERALIST-COMPACT-MODEL-CMQ1.md).

This is an opt-in experiment on branch
`morgoth-generalist-compact-model-qualification-v1`, based on R2 commit
`cad7dbed7bd99331be9858a990dfa2e58806a0a7`, which contains P1 commit
`03fe55ff008a541eb4fd4d207bde1377a1cc3e3f` in its ancestry. It does
not replace Morgoth's configured model or change browser execution policy.

## Evidence and hypothesis

The operator reported one cold `llama3.1:8b` structured response in
112.346 seconds, including 98.494 seconds of Ollama-reported load time.
That establishes a slow but successful trial. It does not prove every cold
request needs longer than 60 seconds, nor establish whether earlier timeouts
were caused by memory, runner initialization or contention. Earlier browser
trials with the configured model and a separate Llama 3 8B model did not
independently finish the reviewed inventory task. Those trials differ from
the R2 inference-only experiment and cannot be treated as a fair paired
model-quality comparison.

The candidate `qwen3:4b-instruct` is listed in the [official Ollama tags](https://ollama.com/library/qwen3/tags)
as a 2.5 GB Q4_K_M artifact. Smaller artifact size makes it plausible on the reported
RTX 3060 6 GB, but does not prove the full model and context fit in VRAM or
that it will make better decisions. The harness accepts any separately
selected installed model with an explicit expected quantization. It checks
the exact installed digest and quantization via bounded [`/api/tags`](https://docs.ollama.com/api/tags); the
model-artifact digest is reported to make the local trial reproducible. It never
downloads, installs, changes the default model, or warms a model silently.

## Two sequential consent gates

The command starts with non-generative preflight: exact clean branch and
reviewed SHA against the local origin-tracking ref, validated loopback URL,
bounded GET `/api/tags` and `/api/ps`, installed digest/quantization, guest
and cgroup headroom, and GPU free memory. Unknown required resource evidence
blocks inference. The local origin ref is checked without a GitHub request;
the engineering handoff separately verifies live remote equality.

**Stage A** requires foreground typed `MODEL ONCE COMPACT`. It makes exactly
one tool-free, nonstreaming, schema-constrained local `/api/chat` request
using the same synthetic observation and R2 action-selection contract.
The experimental request deadline is 120 seconds, with a 123-second wrapper
and a 125-second HTTP read timeout. It uses R2's five-second, at-most-26
in-memory resource samples and bounded journal classifier. No browser action
is executed. The report distinguishes cold versus loaded residency, response
validity, client elapsed time, Ollama-reported load/prompt/generation times,
and measured memory/VRAM extrema. `STAGE_A_QUALIFIED` requires one complete
valid structured choice within the normal 60-second cold boundary and no
observed resource-pressure flag. A complete answer after 60 seconds is useful
reliability evidence, but it does not automatically advance to Stage B.

**Stage B** happens only after Stage A qualifies and the installed model
digest and quantization are rechecked. The existing P1 operator path then
checks the confined browser fixture and selected-model headroom before
requesting separate foreground `BROWSE ONCE` consent. It runs exactly one
reviewed synthetic inventory task through the existing hierarchical source,
strict current action catalog, fresh DOM checks, loop protection, private
Bubblewrap network namespace, and independent rendered acceptance checker.
The total Stage B limit remains six model calls across high and low levels,
not six per level. Normal 60/45/35-second model deadlines, the 105-second
hierarchical worker wait and 260-second interactive ceiling remain unchanged.
Stage A may leave the model resident; Stage B latency must therefore be
interpreted as a subsequent trial, not an independent cold load. The outer
foreground shell deadline is 480 seconds, including both consents and cleanup.

No raw prompt, model response, page text, reasoning trace, journal message,
credential or browser profile is exported. The synthetic trace contains only
SHA-verified fixture control codes and worker-acknowledged effects; it is
removed from the public result if source verification fails. Browser success
requires independent UI acceptance. A valid JSON action alone is not task
success, and one task completion is not production qualification.

## Local development verification and limits

The restricted development namespace could not reach the local Ollama
loopback endpoint. A separately authorized bounded GET-only local read then
returned `CANDIDATE_INSTALLED=false` from responsive `/api/tags` and
`/api/ps`; the candidate is **not currently installed** in that observed
environment. That read measured 6874 MiB guest memory available and 5476 MiB
GPU memory free; the development cgroup was unlimited. These are point-in-time
headroom measurements, not evidence that the candidate will load. No live
model inference was performed. The installed inventory and resources must be
checked again under the operator's ordinary WSL conditions.

Five existing real-browser tests initially reproduced
`INTERACTIVE_WORKER_FAILED` inside the nested development sandbox. An
unchanged Bubblewrap test under the permitted local execution context passed
5/5; the complete existing hierarchical file then passed 16/16, including
the six formerly failing confined-browser cases. This qualifies that
environment for synthetic Chromium, not external websites. The earlier
failures remain recorded as environment-dependent evidence, not erased.

The Stage A harness and gate tests use mock HTTP and scripted browser
results. They prove transport, accounting and authorization order, not
Qwen's actual semantic competence. Focused hermetic selection: 58 passed;
focused harness plus real confined-browser selection: 72 passed. The final
pushed SHA is supplied in the handoff. No PostgreSQL path was changed. The
canonical suite is unnecessary because runtime controller, persistence and
normal model routing are unchanged.

## Operator commands and decision

The final engineering handoff provides literal-SHA commands. The
`--preflight-only` command is non-generative. It currently reports
`MODEL_NOT_INSTALLED`, so the experiment stops before consent or inference.
The optional operator download action is
`ollama pull qwen3:4b-instruct`, after confirming the official model tag and
available disk/network resources. The harness never runs that command.

The qualification command performs both stages in one process with two
separate typed consent gates. If Stage A fails or exceeds the normal
deadline, Stage B is never attempted. If the candidate loads and answers
reliably, produces valid structured choices, and independently completes
the synthetic task, that supports a future fairer comparison. A single
fast JSON response cannot establish good planning. If it remains decision
limited, compare a separately chosen installed model or planning design
under the same task and budget; do not replace the default automatically.

The reviewed command forms are below. Replace `REVIEWED_PUSHED_SHA` with the
literal SHA in the final engineering handoff. The first command makes only
local read-only metadata requests; the second requests the two separate
foreground consents if all preceding gates pass.

```bash
cd /home/corio/Morgoth/dev-morgoth-generalist-compact-model-qualification-v1 && timeout --foreground --signal=INT --kill-after=10s 90s env MORGOTH_LOCAL_CONFIG=/home/corio/Morgoth/morgoth/.env /home/corio/Morgoth/morgoth/.venv/bin/python -m agents.generalist_compact_model_qualification --model qwen3:4b-instruct --expected-sha REVIEWED_PUSHED_SHA --expect-quantization Q4_K_M --preflight-only
```

```bash
cd /home/corio/Morgoth/dev-morgoth-generalist-compact-model-qualification-v1 && timeout --foreground --signal=INT --kill-after=10s 480s env MORGOTH_LOCAL_CONFIG=/home/corio/Morgoth/morgoth/.env /home/corio/Morgoth/morgoth/.venv/bin/python -m agents.generalist_compact_model_qualification --model qwen3:4b-instruct --expected-sha REVIEWED_PUSHED_SHA --expect-quantization Q4_K_M
```
