# Offline Project Manager V1

The CLI and future desktop adapter share `project_manager.ProjectManager`.
It accepts an explicit `RuntimeHome`, uses the existing `ProjectConfig`,
`load_projects` and `validate_projects`, and never rebinds
`current_project()`. One research engine process still selects one Project
through `MORGOTH_PROJECT=<id>`; management can inspect many Projects without
starting those engines.

```text
CLI or desktop/API adapter
          ↓
same Project Manager service
          ↓
existing ProjectConfig/catalog/validators

Engine supervisor
          ↓
starts a separate selected-Project process later
```

Tauri must use this service, not invent another catalog or rewrite `.env`.
No Tauri, HTTP API, process supervisor or cross-platform packaging is
implemented here.

## Commands

```sh
morgoth projects list [--json]
morgoth projects show research_a [--json]
morgoth projects create research_a --name "Research A" --domain crypto [--json]
morgoth projects validate research_a [--json]
```

`python -m scripts.projects_cli ...` is the equivalent Bash-free entry
point. All commands use `MORGOTH_HOME` through `RuntimeHome`; demos and
tests must set it to a disposable directory. JSON success is emitted alone
on stdout. Errors are structured on stderr with stable codes
`INVALID_REQUEST`, `UNKNOWN_DOMAIN`, `UNKNOWN_PROJECT`,
`ALREADY_EXISTS`, `INVALID_CATALOG`, `NAMESPACE_CONFLICT`,
`PATH_CONFLICT`, or `PUBLICATION_FAILED`. Exit codes are 2 for invalid
requests/unknowns, 3 for an existing Project, 4 for catalog/path conflicts,
and 5 for publication failures.

Example exact `create --json` shape for
`MORGOTH_HOME=/tmp/example/home` (the namespace is ID-derived):

```json
{
  "created": true,
  "durability_confirmed": true,
  "engine_started": false,
  "storage_initialized": false,
  "project": {
    "id": "research_a",
    "name": "Research A",
    "domain": "crypto",
    "legacy": false,
    "manifest_path": "/tmp/example/home/projects/research_a/project.yaml",
    "workspace_root": "/tmp/example/home/projects/research_a/workspace",
    "postgres_schema": "p_1e638fff38311556e6b0d0dbdfc522212f072ca277eb150dc2f96ad65dd6",
    "chroma_prefix": "p_1e638fff38311556e6b0d0dbdfc5_",
    "vault_dir": "/tmp/example/home/projects/research_a/vault",
    "runtime_dir": "/tmp/example/home/projects/research_a/runtime",
    "configuration_valid": true,
    "runtime_checked": false
  }
}
```

The manager never reports READY/RUNNING:
configuration validity is separate from provider setup, database/Chroma
initialization and engine health. List/show/validate are offline reads;
an absent managed catalog lists only the built-in `default` Project with its
historical crypto paths. They do not create a home, lock file or runtime state.
Malformed neighbouring manifests and namespace collisions fail closed.

## Owned layout and publication

For `MORGOTH_HOME=/some/home`, a new ID `research_a` owns:

```text
/some/home/projects/research_a/project.yaml
/some/home/projects/research_a/workspace/
/some/home/projects/research_a/vault/
/some/home/projects/research_a/runtime/
```

The ID is the immutable machine identity. The display name does not set
paths or storage namespaces. PostgreSQL schema and Chroma prefix are stable
SHA-256-derived identifiers from the ID; the canonical Project validators
still reject any collision with an existing Project. The manager validates
the installed Domain pack; there is no fallback to Crypto. It creates an
**empty** owned workspace, never attaches or scans an existing user tree,
and grants no new agent filesystem permissions. It does not create a
PostgreSQL schema or Chroma collection.

Creation takes a cooperative local `flock` in the catalog. It creates
private directories and a fully written, fsynced pending manifest named
`project.yaml.pending`. The existing loader scans only `*/project.yaml`,
so readers cannot discover this staged state. A non-overwriting hard link
publishes the complete manifest in one step; an existing Project directory
is never overwritten. Failure before publication removes only the empty
directories and pending file owned by that attempt. Symlink components and
catalog symlink entries are rejected. Directory and manifest permissions
are restrictive where supported.
If post-publication fsync fails, the Project remains discoverable and the
result reports `durability_confirmed=false`; retrying cannot overwrite it.

This is a cooperative local-process lock, not a defense against a privileged
hostile administrator changing the filesystem. The atomic-link and lock
behavior is currently tested on Linux; Python module availability does not
establish Windows or macOS support.

No managed LLM profile file is written. Absent profile state retains the
existing `legacy` routing semantics; a later explicit profile switch uses
the existing Project-scoped profile manager. Creating a Project does not
select a provider or probe authentication.

Deferred: use/start/stop/open, delete/archive/clone/import, Domain changes,
data migration, custom-profile editing, API/Tauri UI and automatic
database/engine provisioning.
