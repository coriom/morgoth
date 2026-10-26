# Desktop D6: Project Markdown vault read contract

`scripts.management_api` mounts `/desktop-knowledge/v1` behind the existing
loopback-only Management token middleware. It mounts independently of the
optional disposable PostgreSQL mission reader. Both operations are GET-only:

- `/projects/{project_id}/documents?limit=20&offset=0&query=` lists at most 20
  documents, with an optional 80-character case-insensitive content filter.
- `/projects/{project_id}/documents/{document_id}` reads an exact opaque ID.

Pages and exact document details use schema version 1,
`PROJECT_MARKDOWN_VAULT` provenance, the
canonical Project ID, filesystem modification timestamp (`recorded_at`), and
read timestamp (`read_at` on pages). A saved Markdown document is **not** an
approved fact. No confidence or human review status is inferred.

Only cataloged managed Projects are accepted. The reserved legacy default is
unavailable because its vault is outside the managed Project tree. A missing
vault returns `SOURCE_UNAVAILABLE`; an existing empty vault returns an empty
page. The reader uses held directory descriptors and `O_NOFOLLOW`, rejects
symlinks and nonregular Markdown files, and has fixed limits: 96 documents,
16 KiB per document, 32 explicit Wikilinks per document, and depth three.
IDs are SHA-256 digests of vault-relative Markdown paths and do not reveal
host paths. Links resolve only to exact vault-relative documents; unresolved
links remain unresolved. No semantic or cross-source edges are invented.

The legacy `/api/wiki/*` interface and compiler are intentionally excluded.
The compiler may write files and invoke Ollama. Persisted theses and
contradictions in PostgreSQL are not exposed because this D6 contract does not
yet establish a separately authorized structured-knowledge read projection.
The existing disposable `morgoth_test` mission reader remains distinct.

The reader performs bounded synchronous filesystem access inside a FastAPI
async route. Thread-pool dispatch through this stack's ASGI test transport
stalled in the current environment; the bounded scan is limited to roughly
1.5 MiB of source bytes. A larger vault fails closed with `SOURCE_TOO_LARGE`.
This is a known latency trade-off and not a general filesystem API.
