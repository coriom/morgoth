# Prospective temporal facts

`TemporalFact` is a small, scalar, Project-owned record of a **prediction** or
**observation**. It is separate from `metric_series`, whose intentionally simple
`(metric, value, observed_at, source)` contract and existing consumers are unchanged.

`valid_at` is the time described by the value: target time for a prediction,
real-world measurement time for an observation. PostgreSQL assigns
`acquired_at = clock_timestamp()` on first insert. This is the **first database
insertion time**, not necessarily the instant Morgoth received the HTTP response.
Neither the provider payload nor a caller's in-memory timestamp can backdate
the persisted record through the application. This is a conservative
application-level provenance guarantee, not tamper-proof external timestamping
or protection against a privileged database administrator. Only a
prediction with persisted `acquired_at <= valid_at` is eligible for later
prospective verification; a retrospectively fetched prediction remains stored
but ineligible. Observations need not precede their valid time. No scorer
currently evaluates either class.

`source_updated_at` is optional provider metadata about dataset freshness. It
never implies model issuance. `source_record_id` is optional provenance when a
provider exposes one; no generic `issued_at` is inferred. The current code SHA
is attached when available. A bounded flat `dimensions` map carries entity
context without source-specific SQL columns. Keys are machine-safe; values are
finite scalar, short, and screened against credential-like names. The stable
SHA-256 identity includes Project, Domain, kind, source, tool, metric, value,
unit, entity, dimensions, valid time and provider update/record identity. It
does **not** include acquisition time: repeated acquisition of the same
provider fact retains the first database timestamp. A new provider update,
record ID or value remains distinct. If a provider gives no version marker
and repeats exactly the same value, its unobservable revision cannot be
distinguished; that limitation must be reported, not fabricated.

`Domain.fact_captures` selects an active, sourced tool, fields, entity,
dimensions and timestamp selectors. Source and unit come from existing
`tool_sources` and `field_units`, avoiding duplicate authority. The tool
router invokes the generic extractor only after a successful active-rail tool
execution, then writes one validated result's facts in one PostgreSQL
transaction in the Project-local schema. A failed extraction or insert leaves
no partial new batch; earlier committed facts remain. The original tool's
`success` and `result` are unchanged. For declared capture tools only,
`metadata.temporal_fact_capture` reports `captured` with `facts_processed`
(including deduplicated facts) or `failed` with zero. Logs name the exception
class, never the raw result. Missing optional values are skipped. Tools
without capture declarations retain their result envelope unchanged.

The generic source-cache early return is **not captured**; `bypass_cache=True`
executes the tool and uses the canonical capture path. Web-search cache hits
likewise return before capture. Weather's tool-internal HTTP cache is in-memory:
its parsed result still passes through the tool execution path, but identical
provider facts deduplicate by semantic key. A cache replay never supplies
`acquired_at`; a first database insert is stamped at that later insertion
time. Denied or inactive tools cannot execute or capture. No ordinary
`metric_series` value is automatically promoted.

`list_temporal_facts()` filters kind, metric, source, entity, dimensions and
valid/acquisition ranges. It returns a complete result of at most 10,000
records, sorted by `(valid_at, acquired_at, semantic_key)`, or raises
`TemporalFactQueryOverflow` when a 10,001st matching record exists. The
bounded query is one PostgreSQL statement with one statement snapshot; two
separate calls are **not** a consistent cross-call snapshot. Callers must
narrow filters for larger corpora. There is no silent truncation or pagination.

The Weather pack schedules NWS observations via its existing metric collector.
MET forecast facts are captured only when the forecast tool is invoked;
automatic prospective forecast accumulation is **not** yet guaranteed. No
Weather scorer or ongoing forecast experiment is part of this layer.

Scorer roles are validated machine-safe identifiers rather than a finite core
list. Their implementations remain a trusted allowlist in
`analysis.scorer_registry`: arbitrary YAML cannot import Python. Existing
Crypto descriptive/directional/campaign-quality callers keep their current
role names and signatures. A future verifier may declare a `verification`
role without changing generic Domain validation. This layer contains no
matching, accuracy metric or Weather-specific scorer.
