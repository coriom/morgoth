# Desktop D2 disposable Project read contract

This optional contract is mounted at `/desktop-read/v1` only when the Management launcher receives `--test-pg-socket`. Normal Management V1 startup and its pinned OpenAPI remain unchanged. The existing loopback Host, no-Origin and native bearer-token middleware authenticates every request. No POST, grant, inference, research or runtime control route is added.

The socket must be an owner-private directory. Connections use the current OS user over that Unix socket, the fixed database name `morgoth_test`, a short connection/command timeout and read-only transactions. The adapter never initializes a schema. A missing generalist table is unavailable, not an empty inventory. Managed Projects are resolved from the validated Management catalog; the legacy `default` Project is refused. SQL predicates include the Project ID and only the catalog's validated schema name is interpolated. This is a **development-only** read source. It cannot read production generalist records because the existing optional store itself refuses non-test databases.

Fixed GET routes:

| Route | Output | Bound |
|---|---|---|
| `/desktop-read/v1/projects/{project_id}/missions?limit=10&offset=0` | Historical checkpoint summaries, receipt counts only | 1–10, offset 0–1000, deterministic `updated_at DESC, mission_id DESC` |
| `/desktop-read/v1/projects/{project_id}/missions/{uuid}` | One exact Project-scoped checkpoint | UUID and Project filter; foreign ID is 404 |
| `/desktop-read/v1/projects/{project_id}/capabilities?limit=10&offset=0` | Identity, digest, validation and fixture-approval state | 1–10, offset 0–1000, deterministic `updated_at DESC, identity` |

The projection omits user IDs, raw checkpoint JSON, invocation contracts, receipt bodies, approval-grant rows, operator provenance, internal paths and secrets. `HISTORICAL_CHECKPOINT` is not a live status. `TEST_FIXTURE_ONLY` is not a production approval. A capability's pending state is a stored proposal, not a human queue entry or grant. Malformed/oversized stored records fail closed. The native transport has its own 64 KiB response cap and strict DTOs. A future production read model needs a separate authenticated database authority, permission review, retention and sensitivity policy; this candidate does not imply that work is authorized.
