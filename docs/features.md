# Features Guide

How to run and verify each Datalog capability. All endpoints share the same project isolation, OpenTelemetry identifiers and RBAC model.

## 1. Operational Foundation

- **Backup, Restore & HA**: Docker Compose can run PostgreSQL, Datalog, and the OTel Collector together. `datalog/backup.py` produces private `pg_dump` archives and restores into an empty database in a single transaction (`python -m datalog.backup backup file.dump` / `restore file.dump`). `examples.verify_recovery` tests restore, standby promotion, and multi-instance deduplication using `target_session_attrs=read-write`.
- **Ingestion Reconciliation**: `/internal/metrics` reports received, accepted, rejected, duplicate, and lag counters per OTLP signal and project. Use `python -m collector.reconcile --project PROJECT` to reconcile Collector `/metrics` (port 8888) against Datalog.
- **Per-Signal Retention & Audit Trail**: **Projects and access** configures independent retention windows for logs, metrics, traces, and evaluations, and exposes an admin-only audit trail including `denied:403` and `failed:<status>` entries.
- **OIDC SSO**: Authorization Code + PKCE (`S256`) + RS256 validation bound by issuer and subject. Configure `DATALOG_PUBLIC_ORIGIN`, `DATALOG_OIDC_ISSUER`, `DATALOG_OIDC_CLIENT_ID`, and `DATALOG_OIDC_CLIENT_SECRET`, register `/api/auth/oidc/callback`, and link accounts with `python -m datalog.manage link-oidc ISSUER SUBJECT USERNAME`.

Start the full Compose stack:

```sh
docker compose --profile stack up -d --wait postgres datalog
docker compose --profile stack exec datalog python -m datalog.manage create-user admin --adopt-existing
# Create an ingestion key in Projects and access and export DATALOG_INGESTION_KEY.
docker compose --profile stack --profile collector up -d collector
```

## 2. Collection, Search & APM

- **Multi-Project Collector & Auto-Instrumentation**: `collector.configure` generates and validates multi-project Collector configs with isolated listeners, keys, disk queues, and OTTL transforms (`python -m collector.configure --projects projects.json --queue /var/lib/datalog/queue --output collector-local.json`).
- **Tail Sampling**: [collector/tail-sampling.yaml](../collector/tail-sampling.yaml) retains errors and slow traces (`>= 1s`) while sampling normal traffic; [collector/trace-router.yaml](../collector/trace-router.yaml) routes spans by Trace ID.
- **Cross-Signal Investigation & APM**: Log Explorer supports literal text, typed JSON attribute filters, facets, patterns, and cursor-based Live Tail. Trace Explorer supports compound attribute filters, min duration, and status. Services provides operation-level RED metrics (`p50`/`p95`/`p99`) and dependency maps; Issues provides error grouping, triage, and window-over-window anomaly detection.

## 3. Metrics, SLOs & Alert Delivery

- **OTLP Series & Exemplars**: Metrics Explorer renders cumulative-to-delta converted series, `p50`/`p95`/`p99` histograms, and clickable exemplar markers linking directly to traces. Dashboards combine metrics, OTLP series, logs, operations, and authorized table frames with shareable filter links.
- **SLOs & Alert Routing**: Monitors support legacy metrics, `@otlp.span.errors`, OTLP metrics, multi-window SLO burn rates, online AI evaluators, data quality, and synthetics. Operator-defined webhook and Slack destinations (`DATALOG_ALERT_DESTINATIONS`) support service/severity routes, silences, and idempotent retries.

## 4. AI Workflows, Experiments & Gateway

- **Provider Instrumentation & Pricing**: [datalog/instrumentation.py](../datalog/instrumentation.py) instruments streaming TTFT and Anthropic/Groq OpenInference SDKs (`ai` extra) with privacy-by-default content redaction. `agents/prices` stores effective-dated USD-per-million token pricing.
- **Datasets, CI Gate & Online Evaluators**: capture production spans into versioned datasets/splits, run reproducible evaluations with `python -m datalog.evaluate --project local --config run.json --baseline A --max-drop 0.05`, configure rule-based or HTTP judge online evaluators, and triage items in the Human Review Queue.
- **Prompt Promotion, Playground & Gateway**: `agents/promote` assigns audited aliases (`production`, `staging`) to immutable prompt versions; **AI Operations** runs side-by-side Playground comparisons and Span Replay; `agents/gateway` executes ordered adapter fallback chains (`DATALOG_MODEL_ADAPTERS`) with full trace, log, and metric telemetry.

## 5. Platform Coverage

- **MCP Server**: `POST /api/v1/mcp?project=...` exposes read-only JSON-RPC tools (`logs`, `metrics`, `traces`, `evaluations`) authenticated by a revocable `read` project token.
- **Browser RUM & Masked Replay**: [datalog/web/browser.js](../datalog/web/browser.js) + `BrowserRelay` capture Core Web Vitals, errors, trace-correlated fetches, and privacy-masked SVG session replays (`data-datalog-private`, `data-datalog-ignore`).
- **Synthetics**: HTTP and Playwright multi-step browser probes (`DATALOG_SYNTHETIC_TARGETS`, `DATALOG_CHROMIUM`) with uptime strips, step timelines, and monitor alerts.
- **Continuous Profiling**: [datalog/profiling.py](../datalog/profiling.py) (Python CPU/tracemalloc) and [datalog/profiling.cjs](../datalog/profiling.cjs) (Node.js Inspector) produce flamegraphs correlated by trace or time window.
- **Infrastructure**: discovers hosts, containers, and Kubernetes pods from OTLP resource attributes and supports one-click resource saturation monitors.

## Reproducible Verification

```sh
python -m pip install -e ".[dev,ai,collector]"
npm ci --prefix examples/node
TEST_DATABASE_URL="$DATABASE_URL" python -m examples.verify_collector --collector /path/to/otelcol-contrib --auto --sampling --ai-providers
TEST_DATABASE_URL="$DATABASE_URL" python -m examples.verify_pipelines --collector /path/to/otelcol-contrib
python -m examples.verify_recovery
```
