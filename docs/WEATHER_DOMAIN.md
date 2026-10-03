# Weather Domain V1

Weather is a production Domain pack, separate from Project storage. The pack is
`domains/weather/domain.yaml`; its three data-feed tools are installed in the
catalog but active only when a Project selects `domain: weather`. The default
crypto Project and its rail remain unchanged. No city enum, paid API, API key,
Weather-specific generic-engine branch is involved. The temperature-only
provider-forecast verifier is described in [WEATHER_SCORER.md](WEATHER_SCORER.md).
The generic temporal-fact contract is described in [TEMPORAL_FACTS.md](TEMPORAL_FACTS.md).

## Source decision (official documentation checked 2026-10-01)

| Role | Official source and endpoint | Access and reuse | Operational constraint |
| --- | --- | --- | --- |
| Forecast | [MET Norway Locationforecast 2.0 `/compact`](https://api.met.no/weatherapi/locationforecast/2.0/documentation) | Worldwide; no key, identifying User-Agent. [MET license](https://docs.api.met.no/doc/License.html) is NLOD 2.0 / CC BY 4.0; attribution and license link required. MET explicitly permits commercial use on its [API home](https://api.met.no/). | [Terms](https://api.met.no/doc/TermsOfService): cache to `Expires`, conditionally revalidate with `Last-Modified`, avoid synchronized or excessive traffic. [HOWTO](https://docs.api.met.no/doc/locationforecast/HowTO) limits coordinates to four decimals. |
| Actual observation | [NWS Weather API](https://www.weather.gov/documentation/services-web-api), `/points/{lat},{lon}` → linked `/gridpoints/.../stations` → `/stations/{id}/observations/latest` ([official OpenAPI](https://api.weather.gov/openapi.json)) | No key **today**; identifying User-Agent required. US government service states its data are open/free to use for any purpose. | NWS does not publish its exact general rate limit; use HTTP cache headers. Its documentation says MADIS quality control can delay observations by up to 20 minutes. V1 benchmark is US-only and station availability varies. |

`Morgoth/0.1 (+https://github.com/coriom/morgoth)` is the shared, non-secret
application identity for both providers. The bounded per-tool HTTP cache keys by
the full URL (including coordinates/station ID), respects `Expires`, and sends
`If-Modified-Since` on revalidation. The existing generic source-snapshot cache
keys only by tool name, so the pack explicitly exempts these parameterized tools
from that cache's measurement warning; using it would mix locations.

Sanitized official-format response fixtures in `tests/fixtures/weather/` include
four small captures from one live MET request and the linked NWS point, station
and observation requests on 2026-10-01 at 38.8512, -77.0402. They retain only
fields used by the production parsers; tests have no external HTTP dependency.

The bounded smoke returned HTTP 200 for MET, NWS point, NWS stations and NWS
latest observation. MET `updated_at` was `2026-09-30T19:17:42Z` (535.2 minutes
old at capture); the forecast's first `valid_at` was `2026-10-01T04:00:00Z`.
NWS `observed_at` was `2026-10-01T03:55:00Z` (17.9 minutes old at capture).
Temperature, wind speed/direction and forecast one-hour precipitation parsed;
no full raw payload was retained in the repository.

[Open-Meteo Free terms](https://open-meteo.com/en/terms) restrict its hosted free
API to non-commercial use, so it is outside this production rail. [MET Frost](https://frost.met.no/authentication.html)
requires a registered client ID even for normal open-data retrieval, so it is
also deferred. Neither provider is needed for V1.

## Semantics, rail and measurement

The `location` entity uses neutral location/coordinate/station aliases, with
acquisition coordinates supplied per tool call. Semantic classes are temperature
(3 h), precipitation (6 h), wind (3 h), and general weather (6 h), with a
declared 6 h default. These are contradiction/research windows, not forecasts'
validity periods. Rainfall is a period total and therefore gets a wider window.

The rail contains `get_weather_forecast_met`,
`find_nws_observation_stations`, and `get_nws_weather_observation` only.
The first and third are numerical data sources; station discovery is a linked
lookup, not a measurement source. The pack declares served phrases, field
phrases/contexts, source identities (MET Norway / NWS), explicit units, and one
benchmark metric collector. Coverage diagnostics report zero unexplained gaps.

MET returns at most 12 forecast records. Each record's `valid_at` identifies
instant air temperature (°C), wind speed (m/s) and wind-from direction (degrees).
Every result carries MET credit, a CC BY 4.0 license link and a flag that its
values were normalized/shortened from the source response.
Optional `next_1_hours` precipitation (mm) carries its own start/end period;
missing values are null or an absent period, never zero-filled. MET's
`meta.updated_at` is the provider's last forecast-data update time. It is
exposed as `source_updated_at`, not as an issuance/model-run timestamp. The
prior smoke's 535-minute figure measured this update age only.

NWS station discovery follows the point's official linked station collection
and preserves its order without claiming it is distance-ranked. Latest actual
observation returns source `observed_at`, station ID and coordinates when
provided, temperature (°C), wind-from direction (degrees), and wind speed
normalized from the reported km/h to m/s by division by 3.6. Original NWS
unit codes remain in `source_units`; null readings remain null. NWS
precipitation is excluded from V1 verification because the official API notes
that observation precipitation can be rounded down and that values below
0.4 inches may appear as zero.

The Domain's single hourly metric collector records observed temperature at
the declarative benchmark station `KDCA`. It is a narrow historical series;
`metric_series` records ingestion time and does **not** replace the source
`observed_at` needed for forecast verification. No collector runs in tests.

## Forecast → observation identity

The Domain now declares temperature-only temporal-fact capture. The tool
router records provider-normalized predictions and observations into the
Project's dedicated temporal-fact table at acquisition, separately from
`metric_series`. A forecast tuple contains
`(source, coordinates, metric, value, unit, acquired_at, source_updated_at, valid_at)`;
an observation tuple contains
`(source, station_id, station_coordinates, metric, value, unit, acquired_at, valid_at)`.
For precipitation the forecast additionally supplies the period start/end.
The temperature verifier defines spatial association, observation-time
tolerance and null handling over facts captured prospectively. It uses
persisted acquisition time, not an inferred model issuance time; it does not
claim calibration, global observations or an operational forecast experiment.

## Project boundary and known limits

A Weather Project manifest selects `domain: weather` and must provide its own
PostgreSQL schema, Chroma prefix, vault and runtime directory. Tests create such
a manifest under a throwaway `MORGOTH_HOME`; no production Weather Project is
installed. The two-process proof shows crypto and Weather share the installed
catalog but expose and execute distinct Domain rails. Only the US NWS station
path provides actual observations; no city geocoding, historical forecast
archive or precipitation verification is included. A read-only temperature
verifier is selected by the Weather Domain; it does not collect forecasts or
score precipitation/wind. Python remains the engine; no Tauri or UI change is
required.

An operator-owned manifest could use this shape; it is documentation, not an
installed Project:

```yaml
id: weather_research
name: Weather Research
domain: weather
postgres_schema: weather_research
chroma_prefix: weather_research_
vault_dir: /srv/morgoth/weather_research/vault
runtime_dir: /srv/morgoth/weather_research/state
```
