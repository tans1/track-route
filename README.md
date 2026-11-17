# Track Route

Python 3.14 / Django project managed with uv. Currently, it adds station coordinates
to a CSV using US Census data. Application features come next.

## Run

```sh
uv sync
uv run python -m scripts.geocode_census
```

Rerun the command to resume interrupted work. Add `--offline` to use existing cache
and city data, `--no-city-fallback` for address matches only, or `--help` for all options.

## Data

| File or folder | Purpose |
| --- | --- |
| `data/raw/fuel-prices-for-be-assessment.csv` | Original input; never modified. |
| `data/processed/stations_geocoded_census.csv` | Every original row and value, in order, plus only `latitude` and `longitude`. |
| `data/processed/census_batch_report.json` | Match type, unresolved reasons, and completion status. |
| `data/cache/census/` | Saved queries and the Census city ZIP; reused on reruns and ignored by Git. |

The [Census batch API](https://geocoding.geo.census.gov/geocoder/Geocoding_Services_API.html)
provides address matches. Unmatched US rows use a unique city/state match from the
[2025 Places Gazetteer](https://www.census.gov/geographies/reference-files/time-series/geo/gazetteer-files.2025.html),
downloaded automatically as a ZIP. City coordinates represent the place, not a
station entrance. Canadian and unresolved rows keep blank coordinates. No API key
is needed; coordinate source: US Census Bureau.

## Code

`scripts/geocode_census.py` is the command entry point. It uses these modules in
`apps/fuel_prices/services/`:

| Module | Role |
| --- | --- |
| `census_batch.py` | API batches, caching, CSV export, and reports. |
| `census_places.py` | City fallback from the Census ZIP. |
| `csv_data.py` | CSV validation and safe file writes. |
| `geo_utils.py` | City-name normalization and coordinate validation. |

Tests live in `apps/fuel_prices/tests/`; Django configuration lives in `config/`.
Geocoding runs without the Django server or database.

## Checks

```sh
uv run python manage.py test
uv run python manage.py check
```

Agent guidance: [AGENTS.md](AGENTS.md) · [CSV skill](.agents/skills/process-fuel-csv/SKILL.md).
