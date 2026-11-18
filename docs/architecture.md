# Architecture

Django handles HTTP; app services handle data, providers, and fuel planning. The application uses CSV files and bounded memory caches, with no database.

## Code map

| Location | Owns |
| --- | --- |
| [apps/stations/](../apps/stations/) | CSV enrichment, coordinate provenance, and the station/price catalog. |
| [apps/locations/](../apps/locations/) | US start/destination resolution through Nominatim. |
| [apps/routes/](../apps/routes/) | Initial ORS route retrieval and caching. |
| [apps/fuel_planning/](../apps/fuel_planning/) | Trip workflow, movement model, optimizer, and map. |
| [apps/common/](../apps/common/) | HTTP helpers, provider errors, and request counters. |
| [config/](../config/) | Django settings, URL routing, and deployment entry points. |

Views and scripts delegate to services. Catalog, geometry, distance, and optimizer logic work without Django or HTTP; `fuel_planning/services/cache.py` handles their Django cache integration.

## Request flow

`POST /api/trips/plan/` is the only JSON endpoint:

1. [views.py](../apps/fuel_planning/views.py) and [serializers.py](../apps/fuel_planning/serializers.py) validate the request.
2. [trips.py](../apps/fuel_planning/services/trips.py) resolves both locations and verifies they are in the US.
3. [planner.py](../apps/fuel_planning/services/planner.py) obtains the initial route, reuses a completed plan or runs the local optimizer, and returns the result.
4. [map_snapshot.py](../apps/fuel_planning/services/map_snapshot.py) prepares the stored map data; the view returns JSON and its map URL.

ORS receives start then destination as `[longitude, latitude]`: one attempt on a route-cache miss, zero on a hit. Search makes no further provider calls. The full ORS response stays internal; the public `path` preserves every route coordinate.

The response contains `start`, `destination`, `path`, `stops`, `totals`, `search_complete`, and `map_url`. Provider counts and cache reuse go in headers.

## Station data

[scripts/geocode_census.py](../scripts/geocode_census.py) enriches the raw CSV before requests. It preserves every row/value and appends latitude/longitude, using address matches followed by unique US city/state matches. Unresolved and Canadian rows keep blank coordinates.

The processed CSV and `census_batch_report.json` are required together. The catalog checks their fingerprints against the raw input and retains coordinate quality and price provenance. Price selection and fuel assumptions are in [requirements](assignment.md#confirmed-project-decisions).

## Fuel planning

The [README](../README.md#solution-and-algorithm) explains the search and refill rules. These constraints matter when changing the implementation:

- Keep all eligible stations and distinct route visits, including shared coordinates. The search compares complete stop sequences, with no fixed lookahead depth.
- Count forward route travel and every estimated access/return leg. Skipping a non-purchase stop cannot increase modeled distance; the solver relies on this.
- Use exact `Fraction` quantities and verify `initial + purchased - consumed = final` after reconstructing every traveled leg.
- An explicit `current_station_id` must identify an eligible station within one meter of the resolved start. Shared coordinates alone do not identify a pump.

Station access is assumed, and city coordinates are approximate. Optimality applies to this movement model, not to every possible road itinerary.

## Caching and limits

Route keys include provider, profile, exact ordered coordinates, and options. Completed-plan keys also include dataset, full route metrics, fuel, source context, and solver/model versions. Provider failures and incomplete plans are not reused as successful results.

Caches are bounded and process-local. Routes, completed plans, and map snapshots last up to one hour. The map endpoint reads its snapshot without geocoding or search; missing snapshots return `410`.
