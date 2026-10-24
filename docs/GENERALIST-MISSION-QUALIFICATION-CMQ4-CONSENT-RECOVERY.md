# CMQ.4 consent-only recovery

Base: `6317968a2a7fe3baf69461d74b730a18d9833631`.
Branch: `morgoth-generalist-mission-qualification-cmq4-consent-recovery-v1`.
This correction changes only the pre-inference attempt lifecycle. The paired
choice input, eight-cell task matrix, model identity rule, browser controller,
prompts, resources, deadlines, six-call mission limit and independent task
acceptance are unchanged. The original CMQ.4 branch and ledger remain intact.

## Original evidence and eligibility

The actual original file is
`/tmp/morgoth-cmq4-6317968a2a7fe3baf69461d74b730a18d9833631.json`.
Read-only inspection found a regular, single-link, current-user-owned `0600`
file of 1,633 bytes at SHA-256
`91f42ba231f1393e33169e19af085e0350ee75be61bd026918bdf0e2a0aba973`.
Strict JSON had every expected top-level field and all eight scheduled cells.
The first Qwen choice was `BLOCKED/MODEL_CONSENT_REQUIRED/0`; the remaining
seven were `BLOCKED/PREREQUISITE_OR_PRIOR_CELL_BLOCKED/0`.
Global dispatched and reserved counts and both model counts were exactly
zero; `REAL_EXPERIMENT_EXECUTED` was exactly false. The original frozen
input hash was
`e64eb1d2671866fdac507d5f650df6ba44fd5dd6c402beaa53f0252a40efb660`.
The subsequent failed invocation is not used as evidence for the original
record. No model reply or browser action was established by that attempt.

The code pins the source commit and complete ledger SHA-256. It reads the
file through a no-follow descriptor, checks ownership, regular-file type,
single link, exact `0600` mode and a 16 KiB size ceiling, then uses strict
duplicate-key-rejecting JSON parsing. It requires the full top-level and
eight-cell shape, exact zero counts, no unknown or reserved attempt, and the
specific first-consent refusal. A changed, partial, ambiguous or
inconsistent record blocks recovery. The original bytes are never rewritten.

## Corrected boundary

The successor branch does not permit an ordinary `--run` simply because it
has a new commit and ledger filename. `--recover-consent-only` is explicit.
It first verifies the exact clean pushed checkout and unchanged original
ledger, freezes the same installed candidate identities, repeats the
non-generative resource/fixture checks and renders a **new** controller-built
post-fill observation. Its input hash can differ from the original because
the contextual time field is sampled anew. The successor record retains
both the original input hash and the new paired input hash. The two models
still receive one identical new frozen input within that campaign.

After a fresh Qwen model/resource preflight, the existing foreground
`MODEL ONCE COMPACT` phrase is required. A missing or mistyped response
returns `CONSENT_REFUSED_BEFORE_INFERENCE`, with zero requests, no successor
ledger and no ownership claim; a new explicit invocation remains possible.
Each refusal leaves a bounded, mode-`0600` cancellation receipt containing
only a fixed reason and source identifiers. It does not reserve an attempt.
The phrase `MODEL ONCE COMPT` remains invalid. No terminal input is supplied
automatically.

Only after correct consent does the runner recheck source and original
evidence, create one exclusive claim named by the **original** source SHA
and ledger hash, and write the first request reservation to the successor
ledger before dispatch. The claim is independent of the successor commit:
later commits cannot silently reset the original attempt. A concurrent or
second recovery loses the exclusive create and sends no request. A crash
after the claim, an unknown browser call count, any reservation or any
dispatched request stays blocked. No partial campaign replay or reset flag
exists. The original file is neither deleted nor modified.

## Operator handoff

Use the literal final pushed SHA from the engineering handoff in place of
`FINAL_PUSHED_SHA` in an ordinary foreground WSL terminal. The inspection
reads the original ledger and checks exclusive ownership; it makes no model
request:

```bash
cd /home/corio/Morgoth/dev-morgoth-generalist-mission-qualification-cmq4-consent-recovery-v1 && env MORGOTH_LOCAL_CONFIG=/home/corio/Morgoth/morgoth/.env /home/corio/Morgoth/morgoth/.venv/bin/python -m agents.generalist_mission_qualification_cmq4 --inspect-recovery --expected-sha FINAL_PUSHED_SHA
```

Only if it reports `RECOVERY_ELIGIBLE`, run the single explicit recovery:

```bash
cd /home/corio/Morgoth/dev-morgoth-generalist-mission-qualification-cmq4-consent-recovery-v1 && timeout --foreground --signal=INT --kill-after=10s 2400s env MORGOTH_LOCAL_CONFIG=/home/corio/Morgoth/morgoth/.env /home/corio/Morgoth/morgoth/.venv/bin/python -m agents.generalist_mission_qualification_cmq4 --recover-consent-only --expected-sha FINAL_PUSHED_SHA
```

That command repeats the preparation, resource, digest and confinement
checks before any inference. Later model and mission consents are unchanged.
The established 38-request aggregate ceiling, 19 per model, and six per
mission remain in force. If Qwen remains resident before the competitor
block, the operator must make any residency intervention deliberately and
separately; the runner never unloads a model. It does not retry inference.

Engineering tests use synthetic ledgers, mocked model transports and
scripted confined Chromium only. On the candidate tree, the focused command
below passed 68 tests (exit 0), including the read-only check of the actual
original ledger and confined scripted Chromium cases:

```bash
/home/corio/Morgoth/morgoth/.venv/bin/python -m pytest -q tests/test_generalist_mission_qualification_cmq4.py tests/test_generalist_compact_model_qualification.py tests/test_generalist_compact_model_direct_baseline.py tests/test_generalist_browser_decision_diagnostic.py
```

The final pushed source SHA is reported in the handoff. No real inference, model
warm-up, real-model browser mission, external website or production action
was performed. Human Gate 3 and `SAFE_FOR_WORKLOADS=False` remain unchanged.
