# Agent guide

Read [README.md](README.md) for commands and layout. Current scope: Census station
CSV enrichment and a US start/destination location endpoint. Routing comes later.

## Code and data

- Use `uv run` for Python and `uv add` for dependencies; keep `pyproject.toml` and `uv.lock` together.
- Keep configuration in `config/`; use `apps/fuel_prices/services/` for CSV logic and `apps/locations/services/` for request geocoding. Keep views and scripts thin.
- Preserve `data/raw/`; write outputs to `data/processed/` and reusable cache/reference files to `data/cache/census/`.
- Preserve every row, its order, and all seven original values. Append only `latitude` and `longitude`; put provenance/status in the cache and report.
- Prefer address matches, then a unique US city/state match. Keep Canadian and unresolved rows with blank coordinates and explicit reasons.
- Limit Census batches to 10,000 queries; match responses by request ID, never order. Deduplicate queries without dropping rows.
- Copy prices unchanged; do not infer fuel type, currency, or units.
- Preserve unrelated edits and intentional deletions. Update docs when commands or layout change.
- For processing changes, follow the [CSV skill](.agents/skills/process-fuel-csv/SKILL.md).

## Request locations

- Use Nominatim `/search`, with `countrycodes=us` and `addressdetails=1`; accept only results whose returned country code is `us`.
- Cache normalized queries and misses in memory; never cache provider failures as misses. Validate both inputs before searching.
- Public Nominatim requires one process with the current limiter, at most one request per second, an identifying User-Agent, attribution, and throttling backoff. Keep the endpoint configurable; use a suitable provider/shared limiter before adding workers or hosts.
- Mock provider requests in tests. Cover input errors, country verification, cache reuse/expiry, concurrency, and provider failures.

## Verification

- Test processing changes with small synthetic fixtures and mocked API responses: `uv run python manage.py test`.
- Check Django/app changes with `uv run python manage.py check`.
- Verify script changes with a small fixture or cached offline input, using temporary outputs to preserve deliverables.
- For docs-only changes, check paths and commands. Report only checks actually performed and any remaining limitations.
