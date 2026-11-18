# Agent guide

Use the [build-track-route skill](.agents/skills/build-track-route/SKILL.md) when
building, changing, or reviewing this project. It covers the path from an empty
repository to the current trip planner; resume existing work instead of rebuilding it.

## Read what the task needs

| Document | Purpose |
| --- | --- |
| [Requirements](docs/assignment.md) | Assignment, confirmed decisions, and delivery status. |
| [Architecture](docs/architecture.md) | Module ownership, data contracts, algorithm, and cache behavior. |
| [README](README.md) | Public overview of the algorithm, workflow, setup, and API. |

## Repository rules

- Use `uv run` for Python and `uv add` for dependencies. Keep `pyproject.toml` and `uv.lock` together; put development tools in the `dev` group.
- Keep views and scripts thin, domain logic in each app's `services/`, and Django configuration in `config/`. The application is database-free.
- Preserve unrelated edits, intentional deletions, raw input, and deliverables. Verify processing changes with temporary outputs and mocked provider HTTP.
- Keep `.env`, credentials, caches, and downloaded reference data out of Git. The raw CSV, processed CSV, and required provenance report belong in the project.
- Keep tests focused on trip requests, independent optimizer checks, and the CSV workflow. Extend these for meaningful regressions.
- Update the relevant document when behavior or layout changes. Report checks actually run; distinguish offline verification from live provider results.

The only public JSON endpoint is `POST /api/trips/plan/`. It resolves locations, gets one initial ORS route on a cache miss, and plans fuel stops locally. Preserve these [model and API contracts](docs/architecture.md) when changing any layer.
