# Weather temperature verification V1

The Weather Domain declares `verification: weather_temperature_verification`.
The trusted scorer compares **MET Norway provider forecasts** with later NWS
station observations. It does not evaluate Morgoth LLM predictions, establish
probabilistic calibration, or measure forecast skill against a baseline.
Crypto has no verification role and returns `UNSUPPORTED`.

Run the read-only, Project-selected command with explicit UTC instants:

```sh
python -m scripts.verify_facts --from 2027-01-02T11:00:00Z --to 2027-01-02T13:00:00Z --as-of 2027-01-02T14:00:00Z --json
```

`--from` is inclusive and `--to` exclusive. The example dates are
illustrative; no historical forecast is fabricated by this command. The
active Project and its Domain are resolved without runtime initialization.
One direct PostgreSQL connection uses `default_transaction_read_only=on`
and one `REPEATABLE READ READ ONLY` transaction. The bounded selection
includes predictions in the interval and observations within the temporal
tolerance on either side. The result is one coherent database snapshot.
The command never creates tables or updates scores. Missing fact tables
return an explicit unavailable error. A corpus exceeding 10,000 selected
rows exits nonzero, before scoring; narrow the interval. There is no partial
report and no assumption that two separate command runs share a snapshot.

## Provenance and maturity

`acquired_at` is the first PostgreSQL insertion timestamp, not necessarily
the HTTP response instant. Only the persisted timestamp counts.
`valid_at` is the forecast target or the observation measurement time.
`source_updated_at` describes MET's provider dataset update; it is not a
model issue/run time. Lead time is `valid_at - acquired_at`, never an
inferred provider-run lead time. Predictions acquired after their target are
`RETROSPECTIVE`. Facts acquired after `as_of` are outside this evaluation.
A target is `NOT_DUE` until the full observation-time tolerance has elapsed
at `as_of`. Neither class counts as a missing observation.

## Matching policy

`temperature_met_nws_v1` requires finite latitude/longitude in valid ranges
and `celsius` on both sides. It evaluates only MET temperature predictions
and NWS actual station temperature observations. No unit conversion is
inferred. The engineering limits are 10 km station distance (haversine) and
30 minutes absolute observation-time offset; validated explicit overrides
are available as `--max-distance-km` and `--max-offset-minutes`. These
limits are V1 choices, not universal meteorological standards.

An observation must describe a time strictly after the forecast was acquired,
and it must satisfy **both** bounds. Selection is independent of forecast
error: shortest station distance, then absolute time offset, then stable
observation time/station/fact-key. For multiple observations at one
station/metric/time, the latest acquisition available by `as_of` wins;
a semantic-key tie break handles equal acquisition times. Distinct forecast
revisions remain distinct. Nearby point forecasts and station measurements
can differ in local conditions and elevation; even a valid pair is not an
identical spatial sample. NWS observations limit V1 to covered US locations.

Status precedence is: `RETROSPECTIVE`, `NOT_DUE`, invalid forecast value,
incompatible forecast unit, missing forecast coordinates, no observation,
no post-acquisition observation, no observation in time, no station in range,
incompatible observation unit, invalid observation value, then `MATCHED`.
The report uses `INVALID_VALUE`, `UNIT_INCOMPATIBLE`,
`MISSING_COORDINATES`, `NO_OBSERVATION`,
`NO_POST_ACQUISITION_OBSERVATION`, `TOO_FAR_IN_TIME`, and `TOO_FAR`
for the respective unmatched states. Unmatched does not mean inaccurate.

For a match, signed error is forecast minus observation; absolute error is
its magnitude. MAE, bias (mean signed error), and RMSE use matched pairs
only; all are null when there are none. Match rate is matched pairs divided
by due prospective predictions; it is null when the denominator is zero.
The report separates candidate, retrospective, not-due, due, matched,
unmatched, unique target, and unique observation counts. Error aggregates
are **revision-weighted descriptive statistics**: correlated forecast
revisions are not independent experiments, and no significance, calibration,
or skill claim follows from these numbers.

The JSON report includes the scorer/policy/schema version, code SHA,
Project/Domain, source pair, interval/`as_of`, effective bounds,
status counts, pair identities and errors, and a SHA-256 digest of selected
canonical fact content. With the same fixed facts and options its scoring
content is deterministic. No report-generation timestamp is mixed into it.

For a clearly synthetic 20 °C forecast and 22 °C observation at the same
place/time, the report has `matched_pairs: 1`, `signed_error: -2`,
`absolute_error: 2`, `mae_celsius: 2`, `bias_celsius: -2`, and
`rmse_celsius: 2`. The SQL test uses an injected future `as_of`; this is
an arithmetic example, not a claim of a captured operational forecast.

MET forecast capture still requires a forecast-tool invocation; automatic
prospective forecast accumulation is **not** implemented. Recorded provider
fixtures fetched after their target remain retrospective. There is no claim
that an operational prospective forecast experiment has run.
