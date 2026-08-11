# Gate 3 Auto-Approve — Design Blueprint

**Status:** AUDIT-ONLY. Nothing in the codebase implements auto-approve as of
commit `1a5e608`. This document defines what would ship and the data
condition under which it activates. No code writes here.

**Core principle.** The dangerous direction (auto-APPROVE writes production
code) is asymmetric: a wrong approve corrupts silently, a wrong reject only
costs a re-proposal. So the default is human, and the auto-tier only
absorbs proposals matching a rule with a ledger-measured zero-false-approve
rate on a large enough sample.

---

## Phase A — The calibration ledger

### A1. Full v2-shadow decision ledger (proposed_by=morgoth, prompt=v2)

| ID       | Status                    | Shadow  | axes                                                                   | Op. decision  |
|----------|---------------------------|---------|------------------------------------------------------------------------|---------------|
| fd8056a3 | rejected                  | APPROVE | api_liveness PASS, field_liveness **WARN**, others PASS               | **REJECT**    |
| 1182ee96 | apply_failed_rolled_back  | APPROVE | api_liveness PASS, field_liveness **WARN**, others PASS               | APPROVE       |
| 54a37dc1 | applied                   | APPROVE | api_liveness PASS, field_liveness **WARN**, others PASS               | APPROVE       |
| 096533da | apply_failed_rolled_back  | APPROVE | all PASS                                                              | APPROVE       |
| 1735f617 | apply_failed_rolled_back  | APPROVE | all PASS                                                              | APPROVE (retry) |
| 580d247c | applied                   | APPROVE | all PASS                                                              | APPROVE       |
| dddbcb5f | tests_failed              | APPROVE | all PASS                                                              | (auto-rejected by gate — never reached operator) |
| f58d3aba | tests_failed              | APPROVE | all PASS                                                              | (auto-rejected by gate — never reached operator) |
| 99aa44ea | rejected                  | APPROVE | api_liveness PASS, field_liveness PASS, semantic_duplication **WARN** | **REJECT**    |
| c158c348 | applied                   | APPROVE | all PASS                                                              | APPROVE       |

**Operator-visible v2 decisions: 8** (excluding the 2 tests_failed that were
gate-rejected before the operator saw them).

### A2. Rule tested against the ledger

**Rule R:** `status_reached=pending_approval AND shadow.verdict=APPROVE AND
all_shadow_axes==PASS AND field_overlap_with_registered_tools==0`.

The `all_axes==PASS` clause is the LEDGER-DERIVED signal: every operator-
REJECT under v2 shadow carries at least one WARN axis. The `pending_approval`
clause implies the deterministic gates (name-coherence, endpoint-dedup,
shape, smoke, static-liveness) already ran and passed — anything failing
those never reaches the operator surface anyway.

### Confusion matrix (v2 shadow, n=8)

|                     | Op. APPROVE | Op. REJECT |
|---------------------|-------------|------------|
| Rule R = YES        | 4 (096533da, 1735f617, 580d247c, c158c348) | **0**  ← the safety-critical cell |
| Rule R = NO (held)  | 2 (1182ee96, 54a37dc1 — both field_liveness WARN, operator judged safe) | 2 (fd8056a3 field_liveness WARN, 99aa44ea sem_dup WARN) |

**Auto-approve capture rate on operator-approves:** 4/6 = 67 %.
**False-approve rate (safety-critical):** 0/4 auto-approves, 0/8 total.
**Held-for-human:** 4/8 (50 %) — includes 2 correct-holds and 2 held-approves
the operator judged safe despite a WARN.

### A3. Honest sample size

**n=8 is not proof.** A zero-false-approve rate on n=8 is a promising signal
but easily consistent with a rule that would fail on the 12th, 20th, or
50th proposal.

Rough Wilson-lower-bound rule of thumb: to claim "false-approve rate ≤ 3 %"
with 95 % confidence you need zero incidents on **n ≥ 30**. To claim
"≤ 1 %" you need **n ≥ 100+**. This is the standard rule-out-a-rare-event
regime, and the asymmetric-blast-radius nature of Gate 3 justifies the
strict end (a false-approve is a code-in-production bug).

**Activation threshold picked below: N=30** (compromise between statistical
minimum and operator patience).

---

## Phase B — Tiered architecture (design only, INERT ship)

### B1. Two tiers, config-defaulted-off

```
proposal enters gate 3 (status=pending_approval)
         │
         ▼
 ┌───────────────────────────┐
 │  TIER-AUTO ELIGIBILITY    │
 │  · shadow.verdict==APPROVE │
 │  · all axes==PASS          │
 │  · field_overlap==0        │
 │  · target_path in EVOL_ZONE│
 │  · AUTO_APPROVE_ENABLED=on │
 └───────────────────────────┘
     eligible?
        │  yes ─▶ auto-apply (reuses the existing apply path — rollback,
        │            health check, commit LOCAL only, NEVER auto-push)
        │            + writes decision log entry (see B2)
        │
        └─ no ──▶ TIER-HUMAN (current behavior — operator sees dossier)
```

- **TIER-AUTO** is defined by the ledger-derived signature only. Any WARN
  axis, any shape/smoke/liveness gate warning, any relational-judgment
  case (subject-implication, novel behavior class) → TIER-HUMAN.
- **TIER-HUMAN** is permanent for the relational-judgment classes — the
  `cee50584` class (where a proposal's `subject` implies novel semantics
  a syntactic gate can't check) stays here until there is a proven
  Gate 2.5 semantic-verifier with its own ledger.
- The whole tiered gate ships with `AUTO_APPROVE_ENABLED=false`. First
  release is inert; the default flip happens only when the activation
  criterion (C) is met.

### B2. Audit trail (mandatory)

Every TIER-AUTO decision writes a structured entry to a new table
`auto_approve_decisions`:

| column          | type          | meaning                                                     |
|-----------------|---------------|-------------------------------------------------------------|
| id              | uuid PK       | log id                                                      |
| proposal_id     | uuid FK       | which proposal was auto-approved                           |
| decided_at      | timestamptz   | when                                                        |
| rule_version    | text          | the signature schema this decision matched (e.g. "R1-v2")  |
| signature_dump  | jsonb         | shadow verdict, all axes, field-overlap count, target_path |
| apply_outcome   | text          | applied / apply_failed_rolled_back / null while running    |

CLI: `morgoth audit [--since TIME]` streams the log with a human-readable
diff of each auto-approved change. **The operator's role shifts from
deciding-each to auditing-a-journal** — that is the actual scaling win,
not the raw approve rate.

Auto-apply commits **LOCAL only**; a manual `git push` remains the only
way changes leave the machine. This preserves the current human-in-the-
loop irreversibility boundary at the network edge.

### B3. Irreversibility guards (invariants, grep-lock in NEXT slice)

1. **Same apply path.** TIER-AUTO reuses `_apply_proposal_bg` with the
   full rollback + health check — no fast path, no skipped step.
2. **Cannot bypass a deterministic gate.** The auto-tier only reads the
   status the deterministic gates produced; it never re-runs them or
   overrides them.
3. **Kill switch.** `AUTO_APPROVE_ENABLED=false` (env, hot-reloadable)
   halts the auto-tier immediately; in-flight decisions finish, no new
   ones start.
4. **Shadow-mutation grep-lock extended.** The existing invariant "shadow
   verifier never mutates state" gets a sibling: "auto-approve never
   bypasses a deterministic gate." Both enforced by a grep-lock test
   (built in the next slice, not this audit).

---

## Phase C — Activation criterion

**TIER-AUTO activates when ALL of the following hold, measured on
`auto_approve_decisions` + `self_modify_proposals`:**

1. Signature R has produced **zero false-approves across ≥ 30 operator
   decisions** (i.e. the shadow-had-APPROVE-and-all-PASS-and-zero-overlap
   set has been operator-approved 30 times in a row with no rejects).
2. Shadow verifier's own false-APPROVE rate is **zero** across that same
   30-decision window (independent count — a false-APPROVE from shadow
   can push into TIER-AUTO if unchecked).
3. No apply-time rollback rate spike in the window (≤ 20 % — matches
   current baseline of ~33 % but tighter).

**Current state:**
- Signature R measured: 4 auto-approves / 0 false-approves on **n = 8**.
- Required: **n ≥ 30**.
- **Not activatable. Not close.** ~4× more decisions needed before the
  activation criterion can even be evaluated.

**What this audit did NOT do:**
- Did not add `AUTO_APPROVE_ENABLED` env or read it anywhere in code.
- Did not create `auto_approve_decisions` table.
- Did not modify `_apply_proposal_bg`, `submit_terminal`, gate stack,
  shadow, or apply.
- Did not commit any behavior change — only this design document.

**Next slice (not this one):** the grep-lock invariants (B3) as tests,
and the `auto_approve_decisions` schema migration + `morgoth audit`
command — both still shipping inert until (C) is met.

---

## Implementation notes (this slice)

Files shipped inert on top of the audit blueprint:

- `self_modify/auto_approve.py` — `classify_tier`, `evaluate_criteria`,
  `should_auto_apply`. Pure functions. The classifier reads caller-
  supplied dicts (no DB, no HTTP, no filesystem).
- `memory/persistent.py` — adds the `auto_approve_decisions` table
  (CREATE IF NOT EXISTS at pool init, non-fatal on failure).
- `self_modify/cli.py` — adds `audit` subcommand: `--now` classifies
  current `pending_approval` rows through Rule R and prints the tier;
  `--write` persists each classification to `auto_approve_decisions`
  in OBSERVATION mode (flag_state records the env, criteria_state
  records what's missing). `--since 'N days'` streams the log.
- `scripts/morgoth-cli.sh` — routes `morgoth audit` to the above.
- `tests/test_auto_approve.py` — the 50-test safety spine:
    · 8-row ledger separation (parametrized: each ledger row → expected tier)
    · property-fuzz across 5 axes × 5 non-PASS values (25 combos, all → HUMAN)
    · double-gate combinatorics (flag off / criteria bad / classifier HUMAN)
    · grep-lock invariants (see §B3 above, all 5 enforced structurally)

Nothing ELSE was touched: not the apply path, not shadow's logic,
not any gate's verdict, not the cycle loop, not the reflect prompt,
not the detector. The audit subcommand is the ONLY new call site,
and it is operator-invoked (never runs automatically).

## Grep-lock invariants (structural tests, present in this slice)

1. `classify_tier` never mutates a gate verdict or shadow row (grep-negative
   on UPDATE/DELETE against shadow_verdicts and self_modify_proposals,
   grep-negative on `submit_terminal(`).
2. `auto_approve.py` does not reimplement apply (grep-negative on
   `subprocess.run`, `"git"`, `systemctl`, `def apply_proposal`,
   `_run_live_pytest`).
3. `AUTO_APPROVE_ENABLED` default is False (grep-positive on the
   `os.environ.get(...) or ""` fallback pattern).
4. Any non-PASS axis → HUMAN (parametrized property test across all axes
   × {WARN, FAIL, SKIP, unknown, ""}).
5. `classify_tier` reads no DB / HTTP / file I/O (grep-negative on
   `pool.acquire`, `.execute(`, `.fetch(`, `httpx.`, `open(`, `Path(`
   inside the function's source).

If any of these breaks, the build fails — no silent regression path.
The activation flip in phase (C) is the only remaining lock that can
change; both must fall (flag ON + data OK) for anything to be applied.

---

Baseline commit: `1a5e608`. Ledger snapshot date: 2026-08-07. n=8 v2
operator decisions.
