# P1 verification and operator acceptance

The operator-only experiment must run from the exact clean pushed branch `morgoth-generalist-hierarchical-planner-v1`. It reuses the reviewed checkout/remote check, local Ollama model-list and resource preflight, confined-browser preflight, and foreground typed `BROWSE ONCE` gate. It runs one synthetic inventory task with one configured model, at most six total high/low model calls, no retries, no external browser access, no production authority and no persistent speculative plan. It does not silently run the direct baseline.

```sh
cd /home/corio/Morgoth/dev-morgoth-generalist-hierarchical-planner-v1 && /home/corio/Morgoth/morgoth/.venv/bin/python -m agents.generalist_browser_hierarchical_benchmark
```

Output includes fixed classifications and bounded counts, timing, resource and reviewed synthetic trace codes. It excludes raw prompts, model text, page text, credentials and hidden reasoning. The categories are `HIERARCHICAL_TASK_COMPLETED` only after independent rendered acceptance, `HIERARCHICAL_DECISION_LIMITED` for abstention/loop, `HIERARCHICAL_BUDGET_LIMITED` when the aggregate ceiling prevents the next required turn, and `HIERARCHICAL_PROTOCOL_OR_RESOURCE_BLOCKED` for technical failure. `COMPARATIVE_EVIDENCE` remains `INSUFFICIENT_COMPARATIVE_EVIDENCE` after one trial. `PRODUCTION_QUALIFIED=false` in every case.

Development verification:

- Runtime code SHA `64da3b98ec737672dd5c78a1e48a11b6a9b2b22d` passed one corrected provenance-aware canonical run: requested and tested SHAs matched, copied-tree SHA-256 matched, 2,660 passed, 37 skipped, zero failed/errors, exit 0, wall 80.8 seconds. Owned cgroup peak: 795,090,944 bytes, 88 PIDs, zero OOM or PID-limit hits. The first canonical run at `9ac1d46282d3332c6a48b67b3a07f915d68f71ba` had 2,658 passed, 37 skipped and one failure: it caught a globally extended direct-action worker wait. This was corrected by scoping 105 seconds to the opt-in hierarchical worker while restoring direct action to 70 seconds; the initial failure is retained as evidence.
- Final focused selection, including actual Bubblewrap-confined Chromium and the existing Ollama timeout regression, passed 147 tests. The final follow-up commit adds only the extra changed-layout/abstention tests and this documentation after the canonical code SHA; no runtime file changed. The final focused selection was run on that tree before the follow-up commit.
- Scripted responses completed inventory, catalog, support and changed-layout tasks in four acknowledged actions plus one subgoal call; the earlier deferred two-input fixture completed in five actions plus one subgoal call. These are protocol/browser proofs only. An unsupported scripted objective abstained with one high-level call and zero actions. No live Ollama inference, production action, database mutation or real website request occurred.

The operator should return only the emitted bounded JSON for interpretation against the prior direct-action evidence. A real hierarchical success remains unverified until that separately consented run completes.
