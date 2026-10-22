# Ollama runner failure investigation (2026-10-22)

This investigation starts from P1 backend `03fe55ff008a541eb4fd4d207bde1377a1cc3e3f`.
It is diagnostic only. No inference, browser action, service restart, model
download, configuration change, or production workload ran during development.
The user's historical resource and timeout figures remain **operator-reported**;
the measurements below are later read-only observations.

## Bounded evidence

The local Ollama binary reports version **0.20.6**. `ollama.service` is active,
with **zero systemd restarts**, and the CUDA backend library is installed.
The service's current memory is about **296 MiB**, observed peak about
**3004 MiB**, and its cgroup `oom` and `oom_kill` counters are both **zero**.
These counters do not capture a possible CUDA allocation failure. The current
WSL GPU query reports **6144 MiB total, 484 MiB used, 5511 MiB free**; this is
not a historical measurement at either timeout. Current guest memory available
was about **6196 MiB**, swap free **2292/3072 MiB**, guest memory pressure
`avg10=0`, and the development shell's cgroup memory limit is unlimited.
The configured context remains **4096** and the configured model matches the
operator's `llama3.1:8b` reference. Bounded loopback `/api/tags` and `/api/ps`
GETs responded; the model was installed (reported size **4692 MiB**) and
**not loaded**, with no other resident model. Current CPU/GPU placement and
competing inference sessions are **unknown**. The prior 87%/13% placement and
~5.5 GiB VRAM occupancy were operator-reported observations from another run.

The classifier read only `ollama.service` journal JSON, two windows of at most
1000 records and 2 MB each, and emitted fixed codes and UTC timestamps. It
never prints message text. No window hit its record limit:

| Local time (CST) | Sanitized evidence |
|---|---|
| 10:05:39.608 | Runner start phase recorded |
| 10:05:42.376 | Runner failure/timeout record containing a CUDA mention; **no explicit CUDA error code was identified** |
| 10:05:43–44 | Further runner start and load phases recorded |
| 10:06:39.105 | A single record contains load-failure, timeout and cancellation markers |
| 10:55:56–58 | Runner start and load phases recorded |
| 10:56:55.627 | A single record contains load-failure, timeout and cancellation markers |

The first window has a runner failure record **before** the later cancellation
record. Journal text is not a request-correlated trace, so the records cannot
be conclusively assigned to one client request. In both later records, load
failure and cancellation are in the **same log record**; their internal causal
order is unknowable from this evidence. CUDA is a mention in the first failure,
not a demonstrated GPU-device or allocation error. Neither the bounded Ollama
unit scan nor the service cgroup counters show explicit OOM evidence. Absence
of that evidence is not proof that all allocations succeeded.

## Client and runner timing

The browser action source wraps the shared `OllamaLLMClient.chat` call in
`asyncio.wait_for`: **60 seconds** for a cold first call, **45** for a loaded
first call and **35** for a follow-up. The underlying HTTP client timeout is
**90 seconds**. The direct browser worker waits **70 seconds** for an action;
the P1 hierarchical worker allows **105 seconds** for its two inference
layers; the interactive browser limit is **260 seconds**, with at most six
total model calls. The client's 60-second cold deadline therefore cancels an
unfinished HTTP operation before the transport's 90-second deadline. A
hermetic test confirms this cancellation propagation and zero received
responses. The journal's ~60-second load/cancel marker is consistent with
client cancellation, but does not prove that cancellation was the original
reason the runner did not become ready.

## Ranked hypotheses and action

1. **Runner initialization instability** has the strongest direct local
   evidence in the first window: a failure was logged ~3 seconds after a
   runner-start phase, before the later cancellation marker. The CUDA mention
   raises a GPU-path hypothesis but does not establish a CUDA allocation error.
2. **Cold-load latency or contention** remains plausible in both windows:
   start/load phases were recorded but no complete client response arrived
   before the 60-second deadline. The operator's historical ~5.5/6 GiB VRAM
   use suggests limited GPU headroom, yet no contemporaneous allocation or
   competing-request measurement exists.
3. **Client cancellation as the cause of the final load-failure log** is
   plausible, especially in the second window, because the load-failure and
   cancellation markers occur together at the client deadline. It does not
   explain the earlier first-window runner failure by itself.
4. **Prompt processing or generation stall** and **HTTP transport trouble**
   remain possible but are not established. Current non-generative loopback
   responsiveness does not prove inference transport was healthy then.

No planner, model, service, context, timeout or GPU setting was changed. There
is no justified runtime remedy yet. In particular, increasing the timeout or
reducing context would alter the experiment without showing whether the
runner started successfully. Preserve the six-action cap and memory guards.

## Operator diagnostic and conditional probe

After this branch is pushed, the following **single non-generative** command
checks the exact clean local/remote branch, validated private loopback model
configuration, GET-only model metadata, numeric service/GPU resources and
sanitized historical journal windows. It does not reveal `.env` contents:

```bash
cd /home/corio/Morgoth/dev-morgoth-generalist-ollama-runner-diagnostics-v1 && MORGOTH_LOCAL_CONFIG=/home/corio/Morgoth/morgoth/.env /home/corio/Morgoth/morgoth/.venv/bin/python -m agents.generalist_ollama_runner_diagnostic
```

If the non-generative report still leaves loading versus cancellation
unresolved, one **separately consented** probe is justified. The command below
first repeats the preflight, then requires exact foreground `MODEL ONCE`
consent. It performs at most one local-loopback, tool-free, schema-constrained
request using the browser action transport and existing 60-second cold or
45-second loaded deadline, with no browser and no retry. It records the exact
client request interval, safe response/load/evaluation metrics if complete,
post-request residency/resource facts, and only fixed journal codes for that
interval. It does **not** silently warm or retain the model before measuring.

```bash
cd /home/corio/Morgoth/dev-morgoth-generalist-ollama-runner-diagnostics-v1 && MORGOTH_LOCAL_CONFIG=/home/corio/Morgoth/morgoth/.env /home/corio/Morgoth/morgoth/.venv/bin/python -m agents.generalist_ollama_runner_diagnostic --probe
```

Interpretation: a server failure recorded before the client request deadline
supports a runner-start problem; a load failure only when the client cancels
does not distinguish slow load from cancellation-induced cleanup. A complete
response with Ollama-reported load duration separates loading from prompt and
generation durations. If the request times out again without a distinct prior
server error or completed response, the root cause remains unresolved; the
next investigation should use a separately reviewed, request-correlated
server trace rather than another browser-planner patch.

The diagnostic's branch pin is an operator safeguard, not model approval or
production authority. It cannot execute browser actions. No generalist
workload is qualified, and `SAFE_FOR_WORKLOADS=False` remains required.

## Development verification

The final focused command was:

```text
/home/corio/Morgoth/morgoth/.venv/bin/python -m pytest -q tests/test_generalist_ollama_runner_diagnostic.py tests/test_generalist_ollama_reliability.py tests/test_generalist_browser_resources.py
```

It passed **43/43**, exit 0. The tests include synthetic journal privacy and
classification cases, hard journal byte/deadline limits and child reaping,
local metadata boundary checks, foreground consent refusal, a single mocked
structured response, request cleanup, and proof that the client cold deadline
cancels an in-flight mocked transport without claiming a server outcome.
Python compilation of the changed modules/tests passed, exit 0.

One broader selection was accidentally run with
`tests/test_generalist_browser_hierarchy.py`: **51 passed, 6 failed**, exit 1.
All six failures were actual confined-Chromium cases ending in the existing
`INTERACTIVE_WORKER_FAILED` refusal before an accepted action. This branch did
not change browser execution. The failure's environment cause was not proved;
it is retained as a verification limitation, not hidden by a rerun. No
PostgreSQL or canonical suite was run because persistence, browser execution
and shared production runtime behavior were unchanged.
