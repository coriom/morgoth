# Project runtime boundary (V1)

```text
Tauri/Rust Desktop (future client and process supervisor)
        ↓
local Morgoth API / process controller
        ↓
Morgoth Engine (Python)
        ↓
Project
        ↓
Domain + LLM + tools + isolated memory
```

One project per engine process in V1; selection is fixed before imports/startup.
Multiple desktop projects may later mean multiple engine processes, with separate
ports/lifecycles. Tauri supervises processes; it is not the research engine.
Python remains the core runtime. The desktop does not require a Next.js server.
Sandbox portability is a separate future layer: existing execution sandboxes
remain Linux-only until another platform is proven. No Tauri/Rust migration here.

## Ownership and selection

`core.project.ProjectConfig` (alias `Project`) is immutable, including its maps.
Project owns identity, optional workspace, runtime/storage paths, optional task
LLM routes and string-valued metadata. Domain owns vocabulary, field mappings,
tool/source semantics and domain scoring/configuration. Packs remain YAML data.
Crypto is one domain; any number of projects can select that same pack.

Set `MORGOTH_PROJECT=<id>` in the process environment **before importing Morgoth**.
An absent selector chooses `default`, the existing crypto installation. Empty,
unsafe or unknown selectors fail closed. A conflicting `MORGOTH_DOMAIN` fails
closed at runtime; that variable remains usable for standalone pack inspection.
Changing selection requires a fresh process, never a runtime toggle. Selection
is not loaded from the production `.env`. New projects never load that file;
the supervisor/operator must explicitly supply their runtime settings.

`runtime_home()` supplies an application home (default `~/Morgoth`, overridden by
an absolute `MORGOTH_HOME`), its `projects/` catalog and the engine installation
root. It does not move existing state. Each new project requires an operator-owned
`$MORGOTH_HOME/projects/<id>/project.yaml`. Example (paths must be absolute):

```yaml
id: research_a
name: Research A
domain: crypto
workspace_root: /srv/morgoth/research_a/workspace  # optional
postgres_schema: research_a
chroma_prefix: research_a_
vault_dir: /srv/morgoth/research_a/vault
runtime_dir: /srv/morgoth/research_a/state
llm_overrides: {}  # task -> existing provider:model syntax; no credentials
metadata: {purpose: research}
```

No synthetic project is installed in the live catalog. Tests generate two
throwaway manifests sharing `domain: crypto`. CLI creation/listing is deferred.
Environment routing (including existing aliases) wins over project LLM routes,
then existing task defaults apply. Reflect/shadow retain explicit CLI precedence.
The prior Codex capability refusal, fallback rules and API opt-in stay intact.

## Canonical namespace and isolation

`current_namespace()` is the sole storage resolver and returns the selected
Project. Domain's three old namespace fields are deprecated compatibility data:
only `legacy_project()` imports crypto's values. Other projects MUST provide
all four namespaces; they never inherit Domain storage defaults. Shared
`core.storage_namespace` validation applies to both the legacy fields and Project.
There is no second effective namespace authority.

The entire catalog is validated before storage opens, including unselected
manifests. All processes sharing storage must use this same catalog; independent
catalogs/hosts are not a distributed namespace registry. Manifests are trusted
operator configuration, not generated research content. Validation rejects:

- Duplicate project IDs, PostgreSQL schemas or physical Chroma collection names.
- Unsafe/truncated SQL identifiers (ASCII lowercase, maximum 63 bytes), reserved
  schemas, `public` for new projects, and missing/empty new Chroma prefixes.
- Equal or nested vault/runtime/workspace roots across projects, cross-kind
  collisions and existing symlink aliases; vault/runtime overlap within a project.
- New writable roots overlapping engine files or the historical token directory.
  Storage paths are absolute, resolved and cannot be `/` or contain `$` expansion.

PostgreSQL uses only the project's schema (plus implicit `pg_catalog`), with
search_path set at connection creation **and every pool checkout**. No fallback
to `public` for missing tables. Unqualified engine/init_db DDL and queries share
that pool. New projects require PostgreSQL 13+ (`gen_random_uuid` built in) and do
not install database-wide extensions; legacy pgcrypto bootstrap is unchanged.
This is application namespace isolation, not a hostile SQL tenant boundary:
separate database roles/OS users and protection against external filesystem
mutation are future deployment concerns.

Chroma uses both `<runtime_dir>/chroma_db` and prefixed physical collections;
legacy callers cannot override a new project's state directory. Logical names
remain stable; another project's physical name is rejected. Wiki paths come from
Project. New project logs, token (`state/auth/ui_token`) and workspace paths are
isolated. Global permission editing and legacy backup spawning are refused for
new projects; shared-code self-modification is disabled in their runtime config.
Human proposal gate 3 remains human-only. No proposals are approved or applied.

Legacy/default keeps `public`, unprefixed collections, engine `data/chroma_db`,
`~/Morgoth/vault`, engine `data` state/logs, relative `logs/morgoth.log`, and
`~/.morgoth/ui_token`. Existing `.env` loading, tools and crypto semantics remain.
Rollback selection: start a future process with `MORGOTH_PROJECT=default` (or
unset it). This change does not restart the current service or migrate any data.

## Audit inventory and deliberately retained paths

Initial audit refers to backend `c28a278` (line numbers at that revision):

| Existing implicit project boundary | Resolution |
|---|---|
| `core/domain.py:54,111,233,277`; `domains/crypto/domain.yaml` | Pack root/selection and deprecated storage fields; Project now owns instance selection |
| `core/config.py:17,94,185` | Engine source root, dotenv/perms, data/log/Chroma paths; new project config redirects writable paths |
| `memory/persistent.py:126,137`; `scripts/init_db.py:69` | Domain schema and pooled SQL; canonical Project namespace including pool reset protection |
| `memory/episodic.py:63,87`; `api/server.py:91`; `analysis/campaign_quality.py:899` | Shared Chroma defaults; canonical constructor isolates new projects even with legacy caller arguments |
| `scripts/compile_wiki.py:48`; `api/routes/wiki.py:8,131` | Domain vault/imported wiki paths; canonical Project vault |
| `api/token.py:26`; `api/routes/admin.py:28,36` | Global token/perms; per-project token and read-only inherited permissions for new projects |
| `main.py:27`; `core/brain.py:2221,2235`; `self_modify/updater.py:115` | Relative log sink, configured file/log writes/backups; project paths, legacy unchanged |
| `core/llm/registry.py:29`; `self_modify/reflect_llm.py:83`; `self_modify/shadow.py:550` | Process-wide routing; optional Project overrides below explicit environment/CLI |
| `core/backup_watchdog.py:28,109` | Legacy installation backup paths; disabled for new projects, unchanged for default |
| `scripts/morgoth-cli.sh:21,125,130`; `backup_morgoth.sh:24`; `compile_wiki_cron.sh:9`; `setup_pm2.sh:3` | Historical operator scripts retained; not a multi-project process controller |
| `self_modify/gates.py:62,63,675,705,944`; `apply.py:56`; `artifact_runner.py:38`; `canonical_runner.py:23,39`; `gate_selftest.py:37` | Shared source/interpreter/sandbox roots retained; project packaging and sandbox portability deferred |
| `core/version.py:26`; tool/plugin discovery from engine source | Read-only installation identity, intentionally distinct from writable project workspace |

No domain vocabulary, research/thesis/fidelity logic, data tools, campaign rows,
service lifecycle, observation scripts or production environment files changed.
The current Next.js proxy still serves the legacy installation; multi-project
UI/process control and historical operator-script portability are future work.

## Proof and safe validation

`tests/test_project_runtime.py` covers immutable config, fail-closed selection,
legacy paths, namespace/missing-field/SQL-identifier/symlink collisions, pool reset,
Chroma routing, isolated config/logs, LLM precedence, forbidden global mutations
and two independent subprocesses with explicit environments (no parent secrets).
`tests/test_project_runtime_integration.py` additionally runs both projects against
**only `morgoth_test`** with temporary schemas, identical knowledge keys and a
public-only sentinel table. It proves isolated writes/reads after pool reuse,
rejects public fallback, preserves extension ownership, and cleans its test state.
The canonical sandbox suite excludes this explicitly marked integration test.

```sh
source .venv/bin/activate
python -m self_modify.canonical_runner
MORGOTH_TEST_POSTGRES_URL='postgresql:///morgoth_test?host=/var/run/postgresql' \
  python -m pytest -q tests/test_project_runtime_integration.py
```

Crypto pack locks, synthesis, extraction, fidelity, backtest, campaign-quality and
session-report regression tests run hermetically in the canonical suite. Live
report/scorer wrappers are not read-only: they call `PersistentMemory.initialize`
(`self_modify/cli.py:1259`, `scripts/campaign_cli.py:169`), and quality may initialize
Chroma (`analysis/campaign_quality.py:900`). Therefore those live commands are
excluded while the campaign runs. No production DB or external LLM is used.
