# Project LLM profiles

`morgoth models` shows the selected Project's profile, effective registry routes,
provider/profile readiness, shadowed override names and a managed recommendation.
`morgoth models profiles` shows the same available built-in profiles.

```sh
morgoth models use claude
morgoth models use codex   # currently refused: NOT_QUALIFIED
morgoth models use legacy
```

These are future operator actions, not commands executed by this migration.
There is no force flag. No production Project profile or .env was changed.

## Policy and migration boundary

Immutable `LLMProfile` policies live in a registry in `core/llm/profiles.py`.
Claude and Codex each manage thesis, synthesis, reflect, shadow and scout as one
high-quality lane. Chat stays `ollama:default`, using the configured local model.
Adding a policy is registry data, not a branch in the switch implementation.
No new API provider or automatic Claude fallback is introduced.

No state file means legacy. Legacy preserves registry precedence:
`MORGOTH_LLM_<TASK>` > `THESIS_GENERATOR` alias for thesis > Project.llm_overrides
> existing task defaults. Invalid legacy registry values retain their historical
warning/default behavior. Reflect also retains its historical explicit argument
> REFLECT_PROVIDER > MORGOTH_LLM_REFLECT > Project override > default ordering;
shadow retains explicit engine > SHADOW_PROVIDER > MORGOTH_LLM_SHADOW > Project
> default. Those explicit per-invocation choices are specific to legacy mode.

Once a managed profile is selected, its routes take precedence over **all** those
legacy settings, including reflect/shadow explicit provider arguments. The CLI
reports shadowed variable names, never their values. `use legacy` restores the
historical authority; it is intentionally allowed even if historical routes point
at an unavailable provider. The ordinary provider gates and fallback still apply.
A malformed, oversized, symlinked, nonregular or unknown profile state fails
closed; it never silently restores legacy.

## Persistence and concurrency

The canonical Project runtime root owns `llm-profile.json`, containing only:

```json
{"schema_version": 1, "profile": "claude"}
```

No credentials, provider configuration, .env contents or task maps are persisted.
The file is mode 0600, published with temporary file + fsync + atomic replace +
directory fsync. New runtime directories are mode 0700; existing directory modes
are preserved. `.llm-profile.lock` serializes writers without storing profile data.
All task routes come from one state snapshot, so publication cannot split a lane.
An in-flight workload finishes with its already-selected route; the next call
reads the new state. A CLI routing-table display uses one snapshot for all rows.

`switch_profile(project, target)` uses the existing CAMPAIGN_LIVE_SQL predicate:
active status, no ended_at, ends_at later than the database clock. It checks the
Project-selected schema, never public fallback. A brief SHARE lock on campaigns
prevents concurrent INSERT/UPDATE until file publication. No campaign row is
changed, expired or initialized. Database/lock failure refuses the switch.
Selecting the already-active unblocked profile is a no-op and needs no database
operation. A blocked selection is rejected even if its identifier is already stored.
Expired-but-active persisted rows do not block a switch. Direct file edits are
unsupported: callers must use the manager to enforce the campaign guard.

## Readiness and qualification

READY means non-inference preconditions passed: Claude's executable/version
probe, or Ollama's configured local model availability. Login/quota are not tested
by inference. UNAVAILABLE providers and profiles are never recommended.
BLOCKED takes precedence for Codex while SAFE_FOR_WORKLOADS=False, even if its
binary is present and runnable. No environment override or CLI force flag bypasses
that gate. `use codex` refuses before DB access or state creation.
`morgoth env` recommends Claude when its managed profile is READY; it no longer
suggests blocked Codex or instructions to edit multiple .env variables.

## Desktop boundary

```text
Tauri selector
      ↓
Project LLM Profile Manager
      ↓
Task Router
      ↓
Provider Registry
```

Future Tauri calls the same `list_profiles`, `current_profile`, `profile_status`,
`resolve_task_route` and `switch_profile` service. It never rewrites .env.
One Project remains bound per engine process; selecting a profile is independent
of selecting a Project or changing any storage namespace. Python remains the
runtime. No desktop UI, server endpoint or Rust engine is introduced here. Writer locking
currently uses Unix fcntl; desktop portability remains future work.

Thesis/synthesis already resolve per workload (`core/brain.py`); reflect and
shadow now also consult managed policy immediately before their LLM calls.
Chat remains local; scout remains a reserved route. Engines must first load this
code in a normal future deployment. Thereafter profile changes require no restart.
This migration did not restart any currently loaded runtime.

## Verification

Hermetic tests cover atomic failure, immutable routing, two Projects sharing the
same crypto Domain with different profiles, legacy restoration, readiness,
qualification, CLI dispatch without DB initialization, corrupted state, and locks.
Dedicated `morgoth_test` integration tests exercise actual SQL expiration and a
concurrent campaign INSERT blocked until profile publication. Existing Project
isolation, campaign lifecycle and Codex security tests remain enabled.
