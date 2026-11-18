---
name: build-track-route
description: Build, extend, refactor, or review this Track Route Django project, from initial uv setup and Census station enrichment through US location resolution, ORS routing, local fuel optimization, and the trip API/map. Use for project implementation, algorithm changes, CSV processing, or architecture maintenance. Not for unrelated Django projects or routine API usage questions.
---

# Build Track Route

## Establish the task

1. Read [requirements](../../../docs/assignment.md) and the relevant sections of [architecture](../../../docs/architecture.md). Links are relative to this repository-local skill; run commands from the repo root.
2. Inspect the current files and Git status. Identify which phase/module owns the requested change. Existing phases are implemented; do not recreate them or revive removed endpoints, database scaffolding, or the old app name.
3. For a fresh build, work through the four phases in order. For maintenance, make the smallest coherent change in the owning modules. Follow the scope the user requested; the guide does not require approval between phases.

## Implement within the project boundaries

- Keep the CLI and views thin. Station data belongs in `apps/stations/services/`, request geocoding in `locations/services/`, initial directions in `routes/services/`, and movement/search/workflow logic in `fuel_planning/services/`. Follow the architecture's named entry points rather than adding parallel helpers.
- CSV work: inspect the actual input first. Preserve every row/order/value and append only latitude/longitude. Match Census responses by ID, checkpoint for resume, and keep status/provenance in the report/cache. Test with temporary outputs and mocked responses before regenerating a requested deliverable.
- Provider work: use Nominatim's Search API with US restriction and returned-country verification. Keep its limiter and identifying User-Agent. Fetch only the initial ORS route; route hits make no HTTP attempt. Never request driving routes through stops or make provider calls during local search.
- Algorithm work: first check the movement model and shortcut-closure assumption. Retain every eligible station and route occurrence, exact `Fraction` comparisons,label predecessors, and all traveled detours. Reconstruct and verify fuel conservation. A feasible incumbent is not automatically an optimum.
- Integration work: retain the sole JSON endpoint and its seven response fields. Keep detailed audit/map data internal. Include all result-affecting inputs in cache identity; incomplete searches never become completed-plan hits. Map reads must remain independent of providers and search.
- Keep model optimality, assumed station access, approximate city coordinates, and incomplete search distinct. Use the confirmed price and starting-fuel policies rather than inventing new dataset or cost assumptions.

## Verify and finish

Use `uv run`. Keep verification focused on the existing trip API, optimizer, and CSV workflow modules. Preserve the small independent oracle comparisons with dominance enabled and disabled. Mock external HTTP; do not use live providers as routine tests or overwrite committed datasets to exercise processing.

After Python changes, run the relevant tests, Django checks, and Ruff. For docs-only changes, verify local links, referenced files/functions, and commands. Record live provider or browser checks separately when the task requires them.

Update only the document that owns the changed contract: requirements for product decisions, architecture for behavior/boundaries, build guide for phase gates or commands. Keep the public README focused on readers using or reviewing the app. Report what changed, the checks actually performed, and any remaining limits.
