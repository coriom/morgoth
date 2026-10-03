# Prospective temporal facts

`TemporalFact` is a small, scalar, Project-owned record of a **prediction** or
**observation**. It is separate from `metric_series`, whose intentionally simple
`(metric, value, observed_at, source)` contract and existing consumers are unchanged.

`valid_at` is the time described by the value: target time for a prediction,
real-world measurement time for an observation. PostgreSQL assigns
`acquired_at = clock_timestamp()` on first insert. Neither the provider payload
nor a caller's in-memory timestamp can backdate the persisted record. Only a
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
router invokes the generic extractor only after a successful live tool call,
then writes to the Project-local PostgreSQL schema. Missing optional values
are skipped; malformed declared data is logged as a capture failure without
changing the original tool response. An installed but inactive tool cannot
execute and cannot create facts. No ordinary metric-series value is
automatically promoted. Queries filter kind, metric, source, entity,
dimensions and valid/acquisition ranges, bounded to 10,000 rows.

Scorer roles are validated machine-safe identifiers rather than a finite core
list. Their implementations remain a trusted allowlist in
`analysis.scorer_registry`: arbitrary YAML cannot import Python. Existing
Crypto descriptive/directional/campaign-quality callers keep their current
role names and signatures. A future verifier may declare a `verification`
role without changing generic Domain validation. This layer contains no
matching, accuracy metric or Weather-specific scorer.
