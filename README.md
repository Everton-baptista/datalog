# Datalog

[![CI](https://github.com/Everton-baptista/datalog/actions/workflows/ci.yml/badge.svg)](https://github.com/Everton-baptista/datalog/actions/workflows/ci.yml) [![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE) ![Python 3.10–3.13](https://img.shields.io/badge/python-3.10%E2%80%933.13-blue.svg) [![PRs welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](CONTRIBUTING.md)

![Datalog: services, AI agents and data pipelines flow into one PostgreSQL store, and a failing span links to its log, AI evaluation and data lineage](docs/images/datalog-hero.svg)

Self-hosted observability for applications, AI agents and data pipelines, licensed under [Apache-2.0](LICENSE) and maintained by **SynapseDev**.

- [Documentation index](docs/README.md) — complete index of guides, contracts and benchmarks.
- [Architecture guide](docs/architecture.md) — module topology, ingestion/query flows, PostgreSQL migrations, background workers, security model and visualization engine.
- [v1 protocol contract](docs/api-v1.md) — HTTP/OTLP endpoints, RBAC roles, quotas, privacy and retention semantics.
- [Features guide](docs/features.md) — Collector stack, cross-signal investigation, AI workflows, SSO and recovery tests.
- [Data observability guide](docs/data-observability.md) — quality checks, OpenLineage, SQL history lineage, row reconciliation and column-to-BI impact.
- [100k-span benchmark](docs/benchmark-100k.md) — local pilot results and comparison with chDB and DuckDB.

The web interface uses the SynapseDev visual identity, with light and dark themes (following the system preference or the top-bar toggle) and **English / Portuguese** language support (PT/EN selector or `?lang=en`).

## Screenshots

### Multi-signal Overview & Live Service Map
![Overview — Pulse, KPIs and Live Service Map](docs/images/screen-overview.png)

### Trace Explorer (KPIs, Volume & p95 Latency Trend)
![Trace Explorer](docs/images/screen-traces.png)

### Services & APM (`p50`/`p95`/`p99` Latency Ranking)
![Services & APM](docs/images/screen-services.png)

### Agent Runs & AI Ecosystem Atlas
![Agent Runs & Atlas](docs/images/screen-agents.png)

### Browser Monitoring & Core Web Vitals (Light Theme)
![Browser & Core Web Vitals](docs/images/screen-browser.png)

## Layout

The server and UI live in the [`datalog/`](datalog) package, SQL migrations in `datalog/migrations/*.sql`, dependencies and optional extras in [`pyproject.toml`](pyproject.toml), and CI reporters in `reporters/`. After `pip install -e .`, the console commands `datalog` and `datalog-manage` are available. See [CONTRIBUTING.md](CONTRIBUTING.md) for the full layout.

## What it does

- **21 responsive UI views** with zero external JS dependencies and pure SVG charts: Overview (multi-signal Pulse, monitor ring, live service map), Log Explorer (stacked severity histogram, JSON attributes, facets, patterns, Live Tail), Trace Explorer (KPI strip, volume/p95 charts, waterfall), Services/APM (`p50`/`p95`/`p99` & latency comparison), Metrics Explorer (legacy & OTLP `p50`/`p95`/`p99` with clickable exemplars), Dashboards (shared filters & CSV frame inspector), Monitors, LLM Observability, Agent Runs (with AI Atlas), Prompts & Versions (line diff & 14-day timeline), Evaluations, Connect App, Distributed AI Compare, Projects & Access, Issues, AI Operations (Playground, Online Evaluators, Review Queue), Browser (Core Web Vitals tri-zone cards & masked SVG session replay), Synthetics (uptime strips & Playwright journeys), Profiles (Python & Node.js flamegraphs), Infrastructure (hosts/containers/pods) and Data.
- **OpenTelemetry OTLP/HTTP**: traces, logs and metrics in Protobuf (`application/x-protobuf`) or JSON (`application/json`), with optional gzip (up to 4 MiB wire/decompressed, 2,000 items/request).
- **LLM & Agent observability**: hierarchical snapshots (`datalog.sdk.DatalogClient`), GenAI/OpenInference span projection, versioned datasets with splits, paired comparison, Jest/Vitest CI reporters (`reporters/`), model gateway with fallback, and span replay.
- **Data observability**: read-only connectors (PostgreSQL, Snowflake, BigQuery, Databricks), freshness/volume/schema-drift/rule checks, key/row reconciliation, OpenLineage, SQL audit lineage (`sqlglot`), data products and column-to-BI impact.
- **Access control & MCP**: `scrypt` sessions, optional OIDC/PKCE SSO, RBAC (`admin`, `editor`, `viewer`), audit log, per-project ingestion keys, scoped tokens (`read`, `evaluate`, `invoke`), per-signal retention, quotas, attribute allowlists and read-only MCP (`POST /api/v1/mcp`).
- **Storage**: PostgreSQL 18 only, with connection pooling and transactional migrations (`datalog/migrations/*.sql`).

## Run locally

Requires Python 3.10+ and PostgreSQL 18 (`pg_config` on `PATH`).

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m datalog.run_local
```

Create the first admin account from another terminal (passwords require 12–256 bytes; Datalog ships with **no hardcoded default user or password**):

```sh
export DATABASE_URL="$(cat .data/database-url)"
.venv/bin/python -m datalog.manage create-user admin --adopt-existing
```

`python -m datalog.run_local` initializes a private Unix-socket PostgreSQL cluster in `.data/postgres` (no TCP listener, development only). Stop it with `.venv/bin/python -m datalog.run_local --stop-db`.

### Managing users and access

Datalog intentionally does not create default credentials in code. Operators provision accounts through `python -m datalog.manage` (or `datalog-manage` after `pip install -e .`), which prompts interactively for a password (12–256 bytes, stored as a `scrypt` hash):

```sh
export DATABASE_URL="$(cat .data/database-url)"   # or your PostgreSQL DSN

# 1. Create the first administrator with access to all existing projects:
.venv/bin/python -m datalog.manage create-user admin --adopt-existing

# 2. Create a new user (starts without access to existing projects):
.venv/bin/python -m datalog.manage create-user alice

# 3. Grant a role on a project (admin, editor, or viewer):
.venv/bin/python -m datalog.manage grant local alice editor
.venv/bin/python -m datalog.manage grant demo alice viewer

# 4. Reset a user's password (also revokes active sessions):
.venv/bin/python -m datalog.manage reset-password alice

# 5. Link an OIDC SSO identity to an existing user:
.venv/bin/python -m datalog.manage link-oidc https://idp.example sub-123 alice

# 6. Run a manual retention sweep:
.venv/bin/python -m datalog.manage sweep
```

Once a user exists, project administrators can also add, change, or remove their project role directly in the UI under **Projects and access**. When running with Docker Compose (`--profile stack`), prefix the same commands with `docker compose --profile stack exec datalog python -m datalog.manage ...`.

### Docker Compose or external PostgreSQL

```sh
cp .env.example .env
# Set POSTGRES_PASSWORD and DATABASE_URL in .env
set -a && . ./.env && set +a
docker compose up -d --wait postgres
.venv/bin/python -m datalog            # options: --port (4310), --bind (127.0.0.1), --max-connections (64), --db-pool-size (10), --database-url
```

Use `docker compose --profile stack up -d --wait postgres datalog` to run the full containerized stack (defaults `DATABASE_URL` to `postgresql://datalog:${POSTGRES_PASSWORD}@postgres:5432/datalog` unless `DATALOG_COMPOSE_DATABASE_URL` is set), and add `--profile collector` to enable the OpenTelemetry Collector Contrib bridge (gRPC/HTTP + persistent disk queue).

## Self-hosting in production

Datalog is self-hosted: each team runs its own instance. What the release already provides, and what each operator owns:

| Area | Built in | Operator's responsibility |
| --- | --- | --- |
| Network & TLS | `--bind` / `DATALOG_BIND` (default `127.0.0.1`); `DATALOG_PUBLIC_ORIGIN` enables `Secure`/`__Host-` cookies and strict origin checks | Terminate TLS at a reverse proxy (Caddy, nginx, Traefik, a cloud load balancer) and forward to Datalog |
| Authentication & authorization | `scrypt` sessions, optional OIDC SSO, per-project RBAC, ingestion keys, scoped tokens, audit log | Create the first admin (`datalog-manage create-user`), connect your IdP |
| HTTP server | Bounded thread-per-connection server: at most `DATALOG_MAX_CONNECTIONS` (default 64) requests are processed at once and at most 4× that many sockets stay open; beyond either limit it answers `503` with `Retry-After` instead of queueing without limit. 10 s socket timeout, 4 MiB body cap | Size `DATALOG_MAX_CONNECTIONS` and `DATALOG_DB_POOL_SIZE` (default 10) for your traffic; run several replicas behind the proxy for more throughput (background workers coordinate with `SKIP LOCKED`) |
| Quotas & backpressure | Per-project request/byte quotas (`429` + `Retry-After`), `503` when PostgreSQL is unavailable; the optional Collector adds a memory limiter and a persistent retrying queue | Tune quotas per project in *Projects & access* |
| Retention | Per-project and per-signal retention; the sweeper deletes in short batches and keeps going while a backlog remains (20 s budget per minute) | Pick retention periods; plan PostgreSQL disk, backups (`python -m datalog.backup backup file.dump`) and `VACUUM` like any database |
| Capacity | [`benchmarks/pilot.py`](benchmarks/pilot.py) (100k-span pilot) and [`benchmarks/load.py`](benchmarks/load.py) (sustained concurrent ingestion/read load with p50/p95/p99 and status codes) | Run `python -m benchmarks.load --url … --key … --workers 32 --duration 60` against your own hardware before rollout |
| Resource management | Create, edit and delete dashboards and monitors from the UI or API (`/api/v1/dashboards/update`, `/delete`, `/api/v1/monitors/update`, `/delete`), all audited | — |

Known limits of this release: tables are not natively partitioned in PostgreSQL, so very high ingestion volumes (hundreds of millions of rows per day) should use the Collector's sampling (`collector/tail-sampling.yaml`) and shorter retention, or a dedicated PostgreSQL tuned for append-heavy workloads. The HTTP server is the standard-library server hardened as above; keep it behind a reverse proxy rather than exposing it directly to the internet.

## API overview

All data routes accept `?project=local` by default and validate a session or project-scoped key. Control APIs live under `/api/v1/` (with `/api/` aliases). See [docs/api-v1.md](docs/api-v1.md) for the complete contract, roles, quotas and retention rules.

| Method | Route | Behavior |
| --- | --- | --- |
| GET | `/api/health` | Checks PostgreSQL connectivity |
| GET | `/api/v1/overview` | Multi-signal summary (logs, OTLP, AI, data), histograms, services and facets |
| POST / GET | `/api/v1/logs` | Ingest JSON logs / search with `q`, `level`, `service`, `env`, `trace_id`, `span_id`, `attrs`, `pattern`, `facets` |
| GET | `/api/v1/logs/tail` | Cursor-based live tail (`after`) |
| POST / GET | `/api/v1/metrics` | Ingest gauge or delta-count samples / query catalog and aggregated series |
| POST | `/v1/traces`, `/v1/logs`, `/v1/metrics` | OTLP/HTTP Protobuf (`application/x-protobuf`) or JSON (`application/json`), optional gzip |
| GET | `/api/v1/traces`, `/api/v1/trace`, `/api/v1/trace/span` | List OTLP traces (with summary & histogram), timeline waterfall and span detail |
| GET | `/api/v1/services`, `/api/v1/otlp/metrics`, `/api/v1/otlp/series` | Service dependencies & APM (`p50`/`p95`/`p99`), raw OTLP points and aggregated series with exemplars |
| GET / POST | `/api/v1/dashboards`, `/api/v1/monitors` | Multi-panel dashboards and threshold / SLO / AI / data / synthetic monitors |
| POST | `/api/v1/dashboards/update`, `/api/v1/dashboards/delete`, `/api/v1/monitors/update`, `/api/v1/monitors/delete` | Edit or delete by `id` (editor role; audited; check-managed monitors change through their source) |
| GET / POST | `/api/v1/ai/traces`, `/api/v1/ai/overview`, `/api/v1/ai/scores`, `/api/v1/ai/prompts` | Complete AI snapshots, usage/cost/latency trends, scores and immutable prompt versions |
| POST | `/api/v1/mcp` | Read-only JSON-RPC MCP endpoint scoped to a revocable `read` project token |
| GET | `/internal/metrics` | Prometheus operational metrics (requires `DATALOG_INTERNAL_METRICS_TOKEN`) |

Default project quotas are 600 requests/min and 128 MiB/min, with a 31-day maximum query window and 4 MiB max request body.

## Seed synthetic demo data

```sh
# Full multi-signal incident across all 21 views (traces, logs, metrics, browser, profiles, data, AI):
.venv/bin/python -m datalog.seed_full --project demo --hours 24

# AI-only demo (180 runs across 6 agents, prompts, scores, logs & metrics):
.venv/bin/python -m datalog.seed_demo

# Distributed agent A/B comparison fixture:
.venv/bin/python -m examples.agent_comparison
```

## Send telemetry

Open http://127.0.0.1:4310/, sign in, go to **Projects and access**, create an ingestion key and export it as `DATALOG_INGESTION_KEY`:

```sh
export OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf   # http/json also works
export OTEL_EXPORTER_OTLP_HEADERS="authorization=Bearer%20${DATALOG_INGESTION_KEY}"
export OTEL_EXPORTER_OTLP_TRACES_ENDPOINT='http://127.0.0.1:4310/v1/traces?project=local'
export OTEL_EXPORTER_OTLP_LOGS_ENDPOINT='http://127.0.0.1:4310/v1/logs?project=local'
export OTEL_EXPORTER_OTLP_METRICS_ENDPOINT='http://127.0.0.1:4310/v1/metrics?project=local'
```

JSON API example (logs and metrics):

```sh
curl -X POST 'http://127.0.0.1:4310/api/v1/logs?project=local' \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer ${DATALOG_INGESTION_KEY}" \
  -d '{"service":"my-api","env":"dev","level":"info","message":"Hello, Datalog!"}'

curl -X POST 'http://127.0.0.1:4310/api/v1/metrics?project=local' \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer ${DATALOG_INGESTION_KEY}" \
  -d '{"name":"http.latency_ms","kind":"gauge","value":42,"service":"my-api","env":"dev"}'
```

Python SDK (`datalog.sdk.DatalogClient`, synchronous, `fail_open=True` by default, 10s timeout; for OTLP/streaming auto-instrumentation use `/v1/*` and `datalog/instrumentation.py`):

```python
from datalog.sdk import DatalogClient

telemetry = DatalogClient(project="local", service="support-agent", env="dev")
with telemetry.trace("support-request", agent="support", input={"question": "status"}) as run:
    with run.span("answer", "llm", model="your-model", input={"question": "status"}) as step:
        step["output"] = "ok"
    run.output("ok")
```

## Development

```sh
.venv/bin/python -m pip install -e ".[dev]"
make lint   # ruff + Biome
make run    # local PostgreSQL + Datalog
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the full workflow.

## Documentation & Benchmarks

- [Documentation index](docs/README.md)

[Contributing](CONTRIBUTING.md) · [Code of conduct](.github/CODE_OF_CONDUCT.md) · [Security](.github/SECURITY.md) · [Trademarks](TRADEMARKS.md) · [Changelog](CHANGELOG.md) · [Third-party licenses](THIRD_PARTY.md)

## Contributing

Contributions are welcome — bug reports, docs, translations and code. Read [CONTRIBUTING.md](CONTRIBUTING.md) for the development setup, how to add an API route, coding conventions and good first contributions. Please follow the [Code of Conduct](.github/CODE_OF_CONDUCT.md) and report vulnerabilities privately as described in [SECURITY.md](.github/SECURITY.md).

## License

Datalog is licensed under the [Apache License 2.0](LICENSE). See [NOTICE](NOTICE), [THIRD_PARTY.md](THIRD_PARTY.md) and [TRADEMARKS.md](TRADEMARKS.md).
