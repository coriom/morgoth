# Local Management API V1

The management app is an independent FastAPI application for **configuration**.
It does not import the research server, select an engine Project, initialize
PostgreSQL/Chroma/models, start collectors, or process campaigns. A future
Tauri/Rust supervisor can call it over authenticated loopback transport:

```text
Tauri/Rust client → Management API → existing ProjectManager
                                    → manifests and canonical validators
Engine supervisor → separate selected-Project process (later)
```

One explicitly chosen `--home` supplies one catalog. The app never changes
`MORGOTH_PROJECT`; the legacy/default Project remains visible. A valid
configuration is **not** a running or ready engine. All reads are offline and
leave an absent home absent. Project creation publishes only local directories
and a manifest; it does not create a database schema or select an LLM profile.

## Local authority and launch

The future supervisor must provision a private, URL-safe token of at least
32 random bytes in an existing 0600 regular file owned by the launching user.
The API never generates or logs that token. For a disposable demonstration,
create a private token file outside any production home using a secure local
secret generator; never pass the token itself as a command argument.

```sh
python -m scripts.management_api --home /tmp/morgoth-demo-home \
  --token-file /tmp/morgoth-demo-token --port 8765
```

The token file is explicit: the launcher does not read `.env`, a Project UI
token, or a provider credential. It rejects missing, empty, malformed,
symlinked, non-regular, other-user or group/world-readable files. The launcher
binds `127.0.0.1` only, fixes one worker, disables reload and proxy headers,
and offers no remote-bind flag. Every HTTP route requires
`X-Morgoth-Management-Token`; query strings and cookies cannot authenticate.
Only Host `127.0.0.1:<port>` is accepted, and **any** Origin header is rejected,
including `null`. There is no CORS policy, public schema endpoint or browser
client in V1. Loopback, Host/Origin checks and the token complement one
another; they do not defend against a privileged hostile local administrator.
The file and cooperative publication lock are tested on Linux/WSL only.

## Typed routes

All routes use `/management/v1`, stable operation IDs and the checked
`ProjectManager`. JSON output includes no engine-health claim.

| Method and path | Operation | Success |
|---|---|---|
| `GET /status` | `management_status_v1` | API version and management availability |
| `GET /domains` | `management_list_domains_v1` | Installed packs, tagline and validation diagnostic |
| `GET /projects` | `management_list_projects_v1` | Legacy/default plus managed catalog |
| `GET /projects/{id}` | `management_get_project_v1` | Configuration view |
| `GET /projects/{id}/validation` | `management_validate_project_v1` | Canonical validation result |
| `POST /projects` | `management_create_project_v1` | Published configuration and durability flag |

`POST /projects` accepts **only** `id`, `name`, `domain` as JSON, up to 8192
bytes. Extra fields, including paths, namespaces and profiles, fail validation.
The manager generates storage identity from the ID and validates the installed
Domain pack. Invalid installed packs appear as `INVALID_DOMAIN_PACK` in the
Domain listing rather than silently disappearing. `runtime_checked=false`,
`engine_started=false`, and `storage_initialized=false` are deliberate.
`durability_confirmed=false` means publication succeeded but a later sync
failed; it is still HTTP 201. Blind retry then receives `ALREADY_EXISTS`.

Error payloads are `{"error":{"code":"...","message":"..."}}`. Authentication
returns 401, invalid Host 400, Origin 403, unsupported media type 415,
oversized body 413, invalid request 422, unknown Project/Domain 404,
duplicate Project 409, invalid catalog/path/namespace conflict 409,
publication failure 500, unexpected internal failure 500. Raw exceptions,
tracebacks, request-body content and tokens are never returned.

The machine contract is generated from the **actual app factory**, offline:

```sh
python -m scripts.generate_management_openapi > docs/management_api_v1.openapi.json
```

The generator uses a synthetic in-memory token and reads no token file. The
committed artifact is checked against the factory in tests. Future Rust/UI
clients should generate from that contract rather than copy example JSON.
An authenticated synthetic client can POST
`{"id":"research_a","name":"Research A","domain":"crypto"}` and then GET
`/management/v1/projects` from a disposable home; the ASGI tests exercise
that exact manager path with no listening server.

Engine supervision, Project start/stop, managed-profile endpoints, deployment,
Tauri and a frontend client remain deferred.

## Verification incident: non-canonical host run

The prior Project Manager branch ran this **non-canonical host** selection:
`env -i HOME=/tmp PATH=/usr/bin:/bin LANG=C.UTF-8 PYTHONPATH=/tmp/morgoth-project-manager-v1 /home/corio/Morgoth/morgoth/.venv/bin/python -m pytest -q tests/test_project_manager.py tests/test_project_runtime.py tests/test_llm_profiles.py -m 'not integration' --disable-socket --allow-unix-socket --timeout=30`.
It stalled around 63% at
`tests/test_project_runtime.py::test_new_project_config_never_loads_production_dotenv`.
A bounded `-vv -x --timeout=10 --timeout-method=signal` reproduction showed
an asyncio worker waiting in `concurrent.futures.thread._worker/get`; after
pytest reported that node failed, the host process still did not exit and
was stopped with Ctrl-C (exit 130). No root cause was established, so this
is **not** classified as inherited or harmless. During this API work, a
bounded plain host `TestClient` smoke also hung even for a minimal FastAPI
app; the canonical cgroup/netns/bwrap runner passed the new ASGI tests.
Unbounded host pytest was not repeated. This is a test-host incident, not
evidence that the management runtime was initialized or that production
services were touched.
