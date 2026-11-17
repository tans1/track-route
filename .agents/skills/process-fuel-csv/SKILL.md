---
name: process-fuel-csv
description: Update and validate Track Route's Census CSV export, including address batches and US city fallback.
---

# Process station CSVs

Follow [AGENTS.md](../../../AGENTS.md) for data rules and verification, and
[README.md](../../../README.md) for commands and paths.

- Keep `scripts/geocode_census.py` thin; place processing logic in `apps/fuel_prices/services/`.
- Use `csv_data.py` for validation and atomic writes, and `geo_utils.py` for city matching and coordinate checks. Preserve identifier and price strings.
- Send headerless ID/street/city/state/ZIP records to `/geocoder/locations/addressbatch`, up to 10,000 per request. Match results by ID; coordinates arrive as longitude, latitude.
- Prefer address matches, then a unique Census Places city/state match. City points are approximate. Record unsupported-country reasons for Canadian rows and reasons for other unresolved rows.
- Cache completed batches, including misses. Keep transport failures distinct from no-match results, and preserve pending versus complete report status.
- Test with synthetic data and mocked responses, including reordered results, duplicate queries, city fallback, Canadian rows, and interrupted runs. Inspect an actual script export using temporary output paths.

Preserve every source row and value; append only `latitude` and `longitude`.
Keep provenance in the cache/report. Update docs when behavior changes; leave
superseded providers and deleted plans removed unless requested.
