# Changelog

Notable changes. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added
- Edit and delete dashboards and monitors from the UI and API (`/api/v1/dashboards/update|delete`, `/api/v1/monitors/update|delete`), audited.
- New README hero image showing the Datalog flow (services, AI agents and data pipelines into one PostgreSQL, linked investigation including prompt versions).
- Bounded HTTP server (`DATALOG_MAX_CONNECTIONS`, 503 + `Retry-After` on saturation) and configurable PostgreSQL pool (`DATALOG_DB_POOL_SIZE`).
- `benchmarks/load.py` sustained load test; self-hosting section in the README.
- Declarative API route table (`datalog/routes.py`) with a single dispatcher (`datalog/server.py`).
- Linting and formatting with ruff and Biome, pre-commit hooks, Dependabot, CODEOWNERS, `Makefile`, CI install/import checks on Python 3.10–3.13 and a Docker build.

- Interface in Portuguese and English (PT/EN selector, `?lang=en`).
- OTLP/HTTP JSON for traces, logs and metrics.
- SynapseDev visual identity with light and dark themes.
- Pulse overview: monitor status ring, KPI tiles with period-over-period deltas and sparklines, segmented time range (15 min to 30 d).
- Charts drawn at the container's real width with hover readout, peak and latest-point highlights.
- `python -m datalog.seed_full`: one coherent synthetic incident across traces, logs, metrics, monitors, issues, browser, synthetics, profiles, infrastructure, data quality/lineage, AI runs and 14 days of prompt-version history.
- Live service map (size = traffic, pulse = rhythm, flowing edges), Atlas of agents/prompts/models/tools with a data-generated text map, inventory and agent profile.
- Prompts & versions: git-style registry with per-version quality, cost, p95, errors and 7-day usage; line diff with measured effect; 14-day change timeline. LLM Observability: per-prompt table.
- Motion system following the SynapseDev tokens (entrances, chart draw-in, count-up, live loops only for live signals, reduced-motion fallbacks).
- Trace and agent-run waterfalls with a time ruler, summary chips and highlighted error rows; evaluation score bars; alert cards with correlated evidence.
- `pyproject.toml`, CI workflow, security policy, code of conduct and contribution templates.

- Architecture guide (`docs/architecture.md`) with system, dataflow, database, worker, security and UI diagrams.
- Multi-signal executive Pulse overview (logs + OTLP spans + p95 latency + AI & Data cross-signal strips), stacked log severity histograms, multi-series OTLP `p50`/`p95`/`p99` charts, Trace Explorer KPI/latency charts, Services latency ranking chart, AI runs/tokens trend charts & model breakdown, Core Web Vitals tri-zone cards, Synthetic uptime strips and Data coverage progress bars.
- Database migration `014_indexes_and_performance.sql` adding missing FK cascade, retention, expiry and environment indexes; batched OTLP exemplar lookup (eliminating N+1 queries) and graceful `SIGTERM` shutdown.

### Changed
- API messages are English with a stable `code` field (`{"error": ..., "code": "not_found"}`); the Portuguese UI translates them through `i18n-api-pt.js`.
- Retention keeps sweeping in batches while a backlog remains (20 s budget per cycle).
- Unexpected server errors return `500 internal_error` without internal details.
- Repository layout: dependencies and extras declared only in `pyproject.toml` (`pip install -e ".[dev]"`); `requirements/` and the root `run_local.py`/`manage.py` shims removed in favor of `python -m datalog.run_local` and `python -m datalog.manage`; docs renamed to English (`architecture.md`, `api-v1.md`, `features.md`, `data-observability.md`, `benchmark-100k.md`); community files moved to `.github/`.
- Python modules moved into the `datalog/` package.
- Run the server with `python -m datalog`, local development with `python -m datalog.run_local` and operator commands with `python -m datalog.manage` (or `datalog-manage`).
- Fixed `pyproject.toml` optional dependencies and license metadata compatibility, Compose default `DATABASE_URL`, Dockerfile packaging, CI cache paths and post-refactor imports in `examples/`.
- Aligned MCP `serverInfo.version` with `datalog.__version__` and fixed `AIStore.atlas()` child-span error accounting.

## [0.2.0] - 2026-11-18

- Initial public layout: logs, metrics, dashboards, monitors, OTLP Protobuf, LLM and agent runs, evaluations, data quality and lineage.
