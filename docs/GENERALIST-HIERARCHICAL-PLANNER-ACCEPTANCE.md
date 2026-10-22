# P1 verification and operator acceptance

The operator-only experiment must run from the exact clean pushed branch `morgoth-generalist-hierarchical-planner-v1`. It reuses the reviewed checkout/remote check, local Ollama model-list and resource preflight, confined-browser preflight, and foreground typed `BROWSE ONCE` gate. It runs one synthetic inventory task with one configured model, at most six total high/low model calls, no retries, no external browser access, no production authority and no persistent speculative plan. It does not silently run the direct baseline.

```sh
cd /home/corio/Morgoth/dev-morgoth-generalist-hierarchical-planner-v1 && /home/corio/Morgoth/morgoth/.venv/bin/python -m agents.generalist_browser_hierarchical_benchmark
```

Output includes fixed classifications and bounded counts, timing, resource and reviewed synthetic trace codes. It excludes raw prompts, model text, page text, credentials and hidden reasoning. The categories are `HIERARCHICAL_TASK_COMPLETED` only after independent rendered acceptance, `HIERARCHICAL_DECISION_LIMITED` for abstention/loop, `HIERARCHICAL_BUDGET_LIMITED` when the aggregate ceiling prevents the next required turn, and `HIERARCHICAL_PROTOCOL_OR_RESOURCE_BLOCKED` for technical failure. `COMPARATIVE_EVIDENCE` remains `INSUFFICIENT_COMPARATIVE_EVIDENCE` after one trial. `PRODUCTION_QUALIFIED=false` in every case.

Development verification and exact tested SHA will be recorded after the final checks. No local Ollama inference is run by the development agent. The operator should return only the emitted bounded JSON for interpretation against the prior direct-action evidence.
