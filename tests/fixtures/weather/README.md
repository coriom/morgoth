# Weather parser fixtures

`*_official.json` are minimized responses captured 2026-10-01 from one
[MET Norway Locationforecast 2.0 compact](https://api.met.no/weatherapi/locationforecast/2.0/documentation)
request and one linked [NWS Weather API](https://www.weather.gov/documentation/services-web-api)
point → station collection → latest observation chain at 38.8512, -77.0402.
Only fields exercised by the production parsers remain. No credentials, HTTP
headers, unrelated station records or full forecast payload are retained.

Credit: **Data from MET Norway**; [CC BY 4.0 license](https://creativecommons.org/licenses/by/4.0/).
The fixture removes unused fields and shortens the forecast series, so it is
modified from the original response. NWS data are US government open data.

The four fixtures without `_official` are synthetic edge cases in the same
official GeoJSON shape; they test missing/null values and exact semantics.
