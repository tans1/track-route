# Requirements and decisions

## Assignment

Build an API in the latest stable Django available when starting the project:

- Accept start and finish locations within the USA.
- Return a route map, cost-effective fuel stops using the supplied price CSV, and total fuel spending. Allow multiple stops on long trips. 
- Assume 10 miles per gallon and a maximum range of 500 miles.
- Use free map/routing services and respond quickly. The assignment prefers one routing call and permits two or three.
- Submit GitHub code and a Loom API demonstration/code overview of at most five minutes, within three days of receipt. The receipt date is not recorded here.

For this checkout, `pyproject.toml` and `uv.lock` define the installed versions.
Check compatibility before upgrading an existing project.

## Confirmed project decisions

| Area | Decision |
| --- | --- |
| API | One public JSON endpoint: `POST /api/trips/plan/`. Geocoding and directions are internal services; the returned link opens an HTML map. |
| Provider usage | One initial start-to-destination ORS attempt on a route-cache miss, zero on a hit. All station selection and fuel calculations are local; never reroute through stops. |
| Station export | Preserve every input row and its seven values; append only `latitude` and `longitude`. Prefer Census address matches, then unique US city/state points. Keep provenance in a separate report/cache. |
| Prices | Select the lowest valid listed price per OPIS ID, retain its source row and other quotes, and break ties by earliest source row. USD per US gallon is an assignment assumption; the CSV does not establish currency, units, or fuel type. |
| Fuel | A 50-gallon tank, initial fuel of 0–50 gallons (default 50), and zero arrival reserve. Starting fuel is already paid for; report purchases separately from consumption. |
| Model | Keep every eligible station. Travel forward along the original route with estimated station excursions. Access is assumed; city points remain approximate. Optimality applies to this model. |
| Storage | CSV/report files and bounded process-local caches. No database, admin, authentication, or session scaffolding is needed. |
| Response | Resolved endpoints, full path coordinates, purchase stops, totals, search completion, and map link. Detailed explanations and tank balances stay on the map. |

