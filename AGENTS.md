# Agent guide

Read [README.md](README.md) for scope, commands, and layout. Current work is Census
CSV enrichment; retain the Django scaffold for later application work.

## Code and data

- Use `uv run` for Python and `uv add` for dependencies; keep `pyproject.toml` and `uv.lock` together.
- Keep configuration in `config/`, reusable logic in `apps/fuel_prices/services/`, and script entry points thin.
- Preserve `data/raw/`; write outputs to `data/processed/` and reusable cache/reference files to `data/cache/census/`.
- Preserve every row, its order, and all seven original values. Append only `latitude` and `longitude`; put provenance/status in the cache and report.
- Prefer address matches, then a unique US city/state match. Keep Canadian and unresolved rows with blank coordinates and explicit reasons.
- Limit Census batches to 10,000 queries; match responses by request ID, never order. Deduplicate queries without dropping rows.
- Copy prices unchanged; do not infer fuel type, currency, or units.
- Preserve unrelated edits and intentional deletions. Update docs when commands or layout change.
- For processing changes, follow the [CSV skill](.agents/skills/process-fuel-csv/SKILL.md).

## Verification

- Test processing changes with small synthetic fixtures and mocked API responses: `uv run python manage.py test`.
- Check Django/app changes with `uv run python manage.py check`.
- Verify script changes with a small fixture or cached offline input, using temporary outputs to preserve deliverables.
- For docs-only changes, check paths and commands. Report only checks actually performed and any remaining limitations.
