# Datalog Architecture

This document describes the technical architecture of **Datalog** (SynapseDev / SynapseAI), including ingestion and query flows, security and privacy boundaries, PostgreSQL storage and migrations, background workers, and the zero-dependency web interface.

---

## 1. System Overview

Datalog is a **modular monolith backed by a single PostgreSQL 18 database**, built on Python's standard library (`ThreadingHTTPServer`), `psycopg 3` (`ConnectionPool`), and `opentelemetry-proto`. It requires no external message brokers or multi-database clusters to run in development or production.

```mermaid
flowchart LR
    subgraph Producers["Telemetry Producers"]
        SDK["Datalog SDK (Python)"]
        OTEL["OTel SDKs (Python/Node/Go...)"]
        COL["OTel Collector Contrib\n(gRPC/HTTP + Disk Queue)"]
        RUM["BrowserRelay + browser.js\n(Web Vitals & SVG Replay)"]
        PROF["Profilers\n(profiling.py / profiling.cjs)"]
        OL["OpenLineage / SQL Audit"]
        WH["Warehouses\n(PostgreSQL / Snowflake / BigQuery / Databricks)"]
    end

    subgraph Server["Datalog Server (datalog/app.py)"]
        HTTP["ThreadingHTTPServer\n(/v1/*, /api/v1/*, /internal/metrics)"]
        AUTH["Auth, RBAC, Quotas & Privacy\n(access.py, sso.py, privacy.py)"]
        DOM["Domain Stores\n(otlp, signal_store, metric_query, ai,\nagent_runs, agent_workflows, alerts,\ninvestigation, platform_signals, data_*)"]
        BG["5 Background Workers\n(evaluator, deliver, probe, retain, quality)"]
    end

    subgraph Storage["Storage"]
        PG[("PostgreSQL 18\nMigrations 001–014")]
    end

    subgraph Consumers["Consumers & Outputs"]
        UI["Web UI SPA\n(21 views · Pure SVG · EN/PT · Light/Dark)"]
        HOOK["Webhooks / Slack"]
        MCP["MCP Clients\n(POST /api/v1/mcp)"]
        PROM["Prometheus\n(GET /internal/metrics)"]
    end

    SDK --> HTTP
    OTEL --> HTTP
    OTEL --> COL --> HTTP
    RUM --> HTTP
    PROF --> HTTP
    OL --> HTTP
    WH --> BG

    HTTP --> AUTH --> DOM --> PG
    BG <--> PG
    BG --> HOOK
    DOM --> UI
    DOM --> MCP
    HTTP --> PROM
```

---

## 2. Backend Modules (`datalog/`)

The [`Store`](../datalog/app.py) class composes all domain stores over a shared `psycopg_pool.ConnectionPool` with transactional context blocks (`with store.connect() as db:`):

| Layer | Modules | Responsibility |
| --- | --- | --- |
| **HTTP & CLI** | `app.py`, `__main__.py`, `run_local.py`, `manage.py`, `operational.py` | HTTP routing (`/api/v1/*`, `/v1/*`, in-memory static assets), graceful `SIGTERM` lifecycle, operator CLI and Prometheus operational metrics (`/internal/metrics`). |
| **Security & Privacy** | `access.py`, `sso.py`, `privacy.py`, `validation.py`, `retention.py` | `scrypt` + SHA-256 sessions, OIDC/PKCE SSO, RBAC (`admin`, `editor`, `viewer`), ingestion keys, scoped tokens (`read`, `evaluate`, `invoke`), per-minute quotas, attribute allowlists and per-signal retention. |
| **OTLP & Core Signals** | `otlp.py`, `signal_store.py`, `metric_query.py`, `trace_media.py`, `investigation.py` | OTLP/HTTP Protobuf & JSON receivers (gzip up to 4 MiB), deterministic deduplication, cumulative-to-delta conversion and explicit/exponential histogram merging (`p50`/`p95`/`p99`) with batched exemplar lookup, error grouping and regression comparison. |
| **Alerts & Dashboards** | `alerts.py`, `dashboard_frames.py` | Monitor evaluation (threshold, `@otlp.span.errors`, OTLP, SLO burn rate, online evals, data quality, synthetics), idempotent webhook/Slack delivery, silences and tabular dashboard frames. |
| **AI & Agents** | `ai.py`, `agent_runs.py`, `agent_workflows.py`, `model_execution.py`, `content_checks.py`, `sdk.py`, `instrumentation.py`, `evaluate.py` | AI snapshots, Agent Atlas, immutable prompt registry & per-version stats, datasets/splits, paired comparison, playground, span replay, model gateway with fallback, online evaluators and CI evaluation CLI. |
| **Platform Signals** | `platform_signals.py`, `browser_relay.py`, `browser_journeys.py`, `profiling.py`, `profiling.cjs`, `mcp_server.py` | Core Web Vitals with Good/Needs Improvement/Poor bands, masked SVG session replay, HTTP & Playwright synthetic probes, CPU/memory profiles, OTLP resource inventory and read-only MCP server. |
| **Data Observability** | `data_quality.py`, `data_lineage.py`, `data_products.py`, `warehouse_sources.py`, `sql_history.py`, `reconcile_rows.py` | Freshness, volume, schema drift, column rules, key/row reconciliation, read-only connectors (PostgreSQL, Snowflake, BigQuery, Databricks), OpenLineage, SQL audit lineage (`sqlglot`), data products and column-to-BI impact. |
| **Operations & Demo** | `backup.py`, `demo.py`, `seed_demo.py`, `seed_full.py` | Transactional `pg_dump`/`pg_restore` backups and coherent synthetic scenario seeders. |

---

## 3. Database Schema & Migrations (`datalog/migrations/`)

On `Store` startup, a transactional advisory lock (`pg_advisory_xact_lock(8103144310)`) applies numbered SQL migrations `001_*.sql` through `014_*.sql` exactly once and records them in `schema_migrations`:

- `001`–`003`: logs, JSON metrics (`metric_definitions` with immutable kind), dashboards, monitors, evaluations, AI tables (`ai_traces`, `ai_spans`, `ai_scores`, `ai_prompts`, view `ai_trace_rollups`) and demo refresh state.
- `004`–`006`: `otlp_spans`, users/projects/members/sessions/keys/retention policies, `otlp_metrics`, datasets and agent evaluations.
- `007`–`010`: integrated observability (alert routes, silences, deliveries, SLOs, scoped tokens, audit log, `online_scores`, `review_cases`, model prices, prompt aliases), platform signals (`browser_events`, `profiles`, `synthetic_checks`, `synthetic_results`, `issue_triage`), online judges and profile time windows.
- `011`–`013`: data assets, quality observations, OpenLineage job runs, data products, BI reports, span media (`trace_media`) and validation workflows.
- `014`: performance indexes for `ON DELETE CASCADE` FKs (`online_scores`, `review_cases`), retention (`agent_evaluations`, `synthetic_results`), expiry (`oidc_flows`, `project_tokens`) and environment facets (`logs`, `metrics`, `otlp_spans`).

---

## 4. Background Workers

The server starts 5 resilient daemon threads that shut down cleanly on `SIGINT`/`SIGTERM`:

1. **`evaluator` (10 s)**: runs online AI evaluators (`store.workflows.evaluate()`) and recalculates monitors (`store.evaluate()`).
2. **`deliver` (2 s)**: delivers queued webhook/Slack alerts with retries and idempotency keys.
3. **`probe` (5 s)**: executes due HTTP and Playwright synthetic probes (`FOR UPDATE SKIP LOCKED`).
4. **`retain` (60 s)**: enforces per-project and per-signal retention in 5,000-row batches and cleans expired tokens/OIDC flows.
5. **`quality` (5 s)**: runs scheduled data quality scans against authorized read-only sources and re-evaluates affected monitors.

---

## 5. Frontend Architecture (`datalog/web/`)

Static UI assets are loaded into memory from `datalog/web/` and served with a strict CSP (`script-src 'self'; style-src 'self'`), **zero external JavaScript dependencies**, and modular scripts:

- **Visual Identity & Themes (`brand.js`, `style.css`)**: semantic CSS custom properties in `:root` and `:root[data-theme=dark]`, self-hosted Geist / Geist Mono fonts, `prefers-reduced-motion` support.
- **Internationalization (`i18n-en.js`, `i18n.js`)**: Portuguese source markup translated on-the-fly to English (`en-US`) via `MutationObserver` and a 1,200+ entry dictionary without modifying user telemetry data.
- **Pure SVG Chart Engine (`charts.js`)**: responsive (`ResizeObserver`) interactive visualizations with hover readouts:
  - `Charts.series` and `Charts.multiSeries` (overlapping `p50`/`p95`/`p99` curves with crosshair and Min/Avg/Max/Latest summary strip)
  - `Charts.bars` and `Charts.stackedBars` (severity-stacked log histograms)
  - `Charts.horizontalBars` (proportional rankings for services, models and issue groups)
  - `Charts.kpi`, `Charts.spark`, `Charts.delta`, `Charts.ring`
  - `Charts.vitalCard` (Core Web Vitals tri-zone Good / Needs Improvement / Poor cards)
  - `Charts.uptimeStrip` (synthetic probe availability strips)
  - `Charts.serviceMap` (live service dependency topology)
- **View Modules**: `app.js` (shell, hash router and 21 views), `traces.js`, `workspace.js`, `ai.js`, `agentic.js`, `platform.js` and `data.js`.

---

## 6. Security & Confinement Model

- **Authentication**: no hardcoded default credentials; passwords require 12–256 bytes, hashed with `scrypt` (`n=2^17`, bounded concurrency semaphore) and `HttpOnly; SameSite=Strict` cookies (`__Host-` + `Secure` when `DATALOG_PUBLIC_ORIGIN` is HTTPS).
- **CSRF**: `Origin` validation + required `X-Datalog-Request: 1` header on session mutations.
- **Anti-SSRF**: external URLs (alert destinations, model adapters, synthetic targets, data sources) are strictly operator-configured via environment variables, enforced with `NoRedirect`, HTTPS outside loopback, and origin isolation in Playwright journeys.
- **Privacy by Default**: OTLP attributes pass through an explicit allowlist (`privacy.py` and `collector/datalog.yaml`), known secret keys are scrubbed, and log/AI content capture is opt-in per project.
