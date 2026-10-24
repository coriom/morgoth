# CMQ.4 checkout and partial-attempt accounting correction

Base: `a0c89a832bd224b84ab3fc4a88d40fa96642710b`.
Branch: `morgoth-generalist-cmq4-checkout-accounting-fix-v1`.
This is a correction and non-generative diagnostic branch. `--run` and
`--recover-consent-only` refuse with `CAMPAIGN_ALREADY_CONSUMED`. It does not
create a new execution authorization, reset a ledger, or change the campaign
matrix, prompts, model identities, resource gates or inference deadlines.

## Evidence boundaries

The operator supplied a summary of a partial attempt: one dispatched and
recorded Qwen choice (`C03`, structured-valid, technically qualified, not
useful at the frozen state), zero Qwen browser mission calls, zero competitor
calls, and a terminal `CAMPAIGN_UNEXPECTED_FAILURE`. The reported Qwen choice
lasted 44,584 ms, including 40,777 ms of model loading, with a resource
pressure warning. `QWEN:heldout` refused at `PREFLIGHT_CHECKOUT`; there was
no `BROWSE ONCE` prompt. These are **operator-supplied facts**, not a raw
transcript or independently reread ledger contents. A later empty `ollama ps`
observation is not evidence of residency at the time of the attempt.

The original ledger, successor ledger and recovery claim named in the task
were absent from `/tmp` in this engineering workspace. They were not deleted,
renamed, overwritten or reconstructed. Their current bytes and successor
ledger accounting could not be independently inspected or copied. The
operator-supplied summary was saved separately outside Git as
`/home/corio/Morgoth/cmq4-incident-evidence/operator-supplied-summary-2026-10-24.txt`
(SHA-256 `97a2ddfd267e36c1a673189e6d966c65eea6c3ba21cd816fd4c6846e0caa7654`).
It is explicitly labeled as a summary. The original ledger hash
`91f42ba231f1393e33169e19af085e0350ee75be61bd026918bdf0e2a0aba973`
is the supplied pinned value, not a current local remeasurement.

The executed worktree remained clean at the expected commit. Its local HEAD,
tracking ref and independently queried remote branch all matched
`a0c89a832bd224b84ab3fc4a88d40fa96642710b`; its origin URL was the
reviewed repository. The three imported modules (`generalist_browser_operator`,
`generalist_compact_model_qualification`, and
`generalist_mission_qualification_cmq4`) resolved under
`/home/corio/Morgoth/dev-morgoth-generalist-mission-qualification-cmq4-consent-recovery-v1/agents/`;
the operator's `ROOT` was that same worktree.
Both existing read-only checkout gates passed during a live-remote check in
this engineering session, taking approximately 4.9 and 5.4 seconds. In the
restricted network sandbox, both refused before reaching a live remote.
Neither current observation establishes the historical cause.

## Confirmed code defect and correction

`generalist_browser_operator._git` used `subprocess.run(..., timeout=8)`.
The browser checkout gate caught `subprocess.SubprocessError`, while the
compact checkout gate called `_git` directly. A `TimeoutExpired` could
therefore become a fixed browser refusal but escape compact preflight and
surface as `CAMPAIGN_UNEXPECTED_FAILURE`. This is a reproduced *possible*
path, not proof that the operator's historical Git request timed out.
The eight-second read-only Git timeout is unchanged.

The shared Git subprocess boundary now converts timeout, nonzero exit,
oversize output and subprocess failure to `PREFLIGHT_CHECKOUT` with fixed
`CHECKOUT_OPERATION` and `CHECKOUT_FAILURE` categories. Both checkout gates
still require the exact reviewed origin, branch, local HEAD, tracking HEAD,
clean worktree and live remote HEAD. Mismatches and dirt remain refusals.
No raw Git stderr, exception text, SSH configuration or model content enters
public diagnostics. `--diagnose-checkout --expected-sha <final-sha>` exercises
both real gates, reports bounded elapsed times and module-root consistency,
and has no model transport or ledger access.

The campaign now writes a terminal failure report when **this process** owns
both the exclusive source-derived recovery claim and the successor ledger.
Known dispatched counts and recorded cells remain intact; outstanding
reservations remain explicit, with execution status unknown where necessary.
A terminal-write failure is reported without clearing the claim or prior
reservation. The initial ledger create remains exclusive, so an unowned
ledger is never finalized by this path. File and containing-directory fsyncs
strengthen the existing ledger write boundary. There is no campaign resume.

## Verification and interpretation

Focused checkout, campaign, compact qualification, direct-baseline and
browser-operator tests passed: **107 passed, 1 skipped**. The skip is the
integration test that needs the absent original `/tmp` ledger. Tests use a
mocked Git subprocess boundary, mocked model transport and, for applicable
existing cases, scripted confined Chromium. No live Ollama inference or
real-model mission was performed. A simulated one-request partial attempt
retained one recorded Qwen result after a browser refusal and a later compact
Git timeout. A simulated failed terminal write retained the unknown
reservation and blocked replay; simulated cancellation likewise retained
an unknown reservation and terminal failure record. These tests establish correction behavior,
not the cause of the historical checkout refusal.

The current campaign is consumed and remains partial. Any further real-model
experiment requires a separately reviewed execution plan with provenance and
request accounting. This branch itself refuses campaign execution even if
volatile `/tmp` evidence is missing. Human Gate 3 and
`SAFE_FOR_WORKLOADS=False` remain unchanged.

## Non-generative checkout verification

From the final clean pushed worktree, use the final SHA from the handoff:

```bash
cd /home/corio/Morgoth/dev-morgoth-generalist-cmq4-checkout-accounting-fix-v1
/home/corio/Morgoth/morgoth/.venv/bin/python -m agents.generalist_mission_qualification_cmq4 --diagnose-checkout --expected-sha FINAL_PUSHED_SHA
```

`CHECKOUT_GATES_PASSED` is a current checkout result only. It is not a model
qualification and does not reopen the consumed attempt.
