# Datalog v1 Contract

The control API lives under `/api/v1/`. Legacy `/api/` paths remain as aliases and return `X-Datalog-API-Version: 1`. OpenTelemetry receivers use standard `/v1/traces`, `/v1/logs` and `/v1/metrics` paths. Breaking changes to the control API require a new API version.

## Identity, User Management & Access Control

- Datalog ships with **no hardcoded default user or password**. Operators provision accounts via CLI with `python -m datalog.manage create-user USERNAME` (interactive password of 12–256 bytes, stored as a `scrypt` hash). Pass `--adopt-existing` on the first admin account to grant admin access to all existing projects. To onboard additional users, run `python -m datalog.manage create-user NEW_USER` and grant access with `python -m datalog.manage grant PROJECT NEW_USER <admin|editor|viewer>` (or via **Projects and access** in the UI). Use `reset-password` to rotate a password and revoke active sessions, `link-oidc ISSUER SUBJECT USERNAME` for SSO, and `sweep` for manual retention cleanup.
- `POST /api/v1/auth/login` accepts JSON `{"username", "password"}`. Sessions last 12 hours using an opaque token stored as a SHA-256 hash in PostgreSQL and delivered in an `HttpOnly`, `SameSite=Strict` cookie (`__Host-` + `Secure` when `DATALOG_PUBLIC_ORIGIN` is HTTPS). `GET /api/v1/auth/session` returns the authenticated user and authorized projects. `POST /api/v1/auth/logout` revokes the session and clears the cookie.
- Session mutations require JSON, `X-Datalog-Request: 1`, and a matching `Origin` header when present. `GET /api/v1/health` is public. Missing/invalid sessions return `401`; unauthorized projects or actions return `403`.
- **Roles**: `viewer` (read-only), `editor` (read + write telemetry, dashboards, monitors, prompts, datasets, evaluations), `admin` (editor + project policies, members, keys, tokens, audit log and data sources). Any authenticated user can create new projects (`POST /api/v1/projects`).
- **Ingestion keys** (`Authorization: Bearer …`) authorize OTLP and ingestion endpoints for their bound project only. They cannot open UI sessions or read data. Secrets are shown once at creation and can be revoked by ID.

Data routes default to `?project=local` when omitted and validate project authorization on every request.

| Method | Path under `/api/v1/` | Payload or Query |
| --- | --- | --- |
| GET, POST | `project/settings` | POST: `retention_days` (int or null), `signal_retention` (`logs`/`metrics`/`traces`/`evaluations`), `request_quota`, `byte_quota`, `capture_content`, `allowed_attributes` |
| GET | `project/audit` | Administrative audit trail |
| POST | `project/members` | `username` and `role` (`admin`, `editor`, `viewer`, or `null` to remove) |
| POST | `project/keys`, `project/keys/revoke` | Create (`name`) or revoke (`id`) ingestion keys |
| POST | `project/tokens`, `project/tokens/revoke` | Create (`capability`: `read`, `evaluate`, `invoke`) or revoke (`id`) scoped project tokens |
| GET | `overview` | Multi-signal summary (logs, OTLP, AI, data), histograms, services and facets |
| GET, POST | `logs` | GET with `q`, `level`, `attrs`, `pattern`, `facets`, `trace_id`, `span_id`. POST accepts object or batch |
| GET | `logs/tail` | Cursor-based live tail (`after`) |
| GET | `logs/patterns` | Up to 50 normalized patterns from the latest 10,000 matching logs (`logs?pattern=…` drills down) |
| GET, POST | `metrics` | POST accepts gauge and delta count; GET returns catalog and aggregated series |
| GET | `traces`, `trace`, `trace/span` | List with `from`, `to`, `service`, `env`, `attribute`, `value`, `attrs`, `min_ms`, `status`, `operation`, `offset` (plus summary & histogram). Detail uses `id` and `span_id` |
| GET | `services` | Service error rates, `p50`/`p95`/`p99`, rates, server operations and parent–child edges |
| GET | `otlp/metrics`, `otlp/series` | Raw OTLP points and aggregated series (`p50`/`p95`/`p99`) with exemplars |
| GET, POST | `dashboards`, `dashboards/frames`, `monitors` | Multi-panel dashboards, authorized table frames, and threshold/SLO/OTLP/AI/data/synthetic monitors |
| GET, POST | `alerts/settings`, `alerts/routes`, `alerts/silences` | Destinations, service/severity routes, silences and delivery history |
| GET, POST | `issues`, `anomalies` | Error grouping, triage (`assignee`, `resolved`) and window-over-window regressions |
| GET, POST | `platform/browser`, `platform/profiles`, `platform/probes` | Web Vitals with bands, masked SVG replay, CPU/memory profiles and synthetic probes (`probes/run`) |
| GET | `platform/resources` | Hosts, containers and pods discovered from OTLP resource attributes |
| POST | `mcp` | Read-only JSON-RPC MCP endpoint authenticated by a `read` project token |
| GET, POST | `ai/traces`, `ai/scores`, `ai/prompts` | Datalog SDK snapshots, scores and immutable prompts (`ai/trace`, `ai/overview`, `ai/prompt-stats`, `ai/atlas`) |
| GET, POST | `agents/otlp`, `agents/datasets`, `agents/dataset`, `agents/evaluations`, `agents/compare`, `agents/evaluators`, `agents/replay` | Distributed GenAI/OpenInference spans, versioned datasets/splits, paired comparison, online evaluators and span replay |
| GET, POST | `trace/media` | PNG/WAV span attachments (up to 1 MiB, Base64 + SHA-256) |
| GET, POST | `data/products`, `data/reports`, `data/impact`, `lineage/sql` | Data products, BI report links, column impact and SQL audit lineage |

HTTP body limit is 4 MiB (wire and decompressed). Default per-project quotas are 600 requests/min and 128 MiB/min across all signals and keys (`429` + `Retry-After` when exceeded; `413` if a single batch exceeds the byte quota).

## Errors

API errors are JSON objects with an English `error` message and a stable `code`:

```json
{"error": "Monitor not found in this project", "code": "not_found"}
```

| Status | `code` | Status | `code` |
| --- | --- | --- | --- |
| 400 | `invalid_request` | 409 | `conflict` |
| 401 | `unauthenticated` | 413 | `payload_too_large` |
| 403 | `forbidden` | 415 | `unsupported_media_type` |
| 404 | `not_found` | 429 | `rate_limited` (with `Retry-After`) |
| 405 | `method_not_allowed` | 500 | `internal_error` |
| 408 | `timeout` | 503 | `unavailable` (with `Retry-After` when the server is saturated) |

Clients should branch on `code` and treat `error` as human-readable text. OTLP endpoints answer with an OTLP `Status` in the request's encoding instead.

## Telemetry, Privacy & Retention

- **OTLP/HTTP**: supports Protobuf (`application/x-protobuf`) and JSON (`application/json`), uncompressed or gzip, up to 2,000 records/request (1 MiB/record, 16 MiB normalized budget). Responses match the request encoding. Supports rootless spans and deterministic deduplication. [OTLP Specification](https://opentelemetry.io/docs/specs/otlp/).
- **Privacy**: OTLP attributes are restricted to an allowlist by default; OTLP log bodies, status messages, and AI inputs/outputs are omitted unless enabled per project (`capture_content`). Known secret keys are scrubbed from JSON objects.
- **Retention**: newly created projects default to 30 days (empty = keep forever). Per-signal overrides (`logs`, `metrics`, `traces`, `evaluations`) are supported. Cleanup runs on startup and every 60 seconds in 5,000-row batches. Configuration (dashboards, monitors, members, keys, prompts, datasets) is never expired.
- **Operational metrics**: `GET /internal/metrics` exposes Prometheus metrics (uptime, HTTP counts, retention deletions, DB pool stats, OTLP per-signal/project counts and lag) protected by `DATALOG_INTERNAL_METRICS_TOKEN`.

## Reproduction

With a disposable PostgreSQL DSN in `TEST_DATABASE_URL`:

```sh
python -m pip install -e ".[dev]"
npm ci --prefix examples/node --ignore-scripts
python -m examples.verify_collector --collector /path/to/otelcol-contrib
```

The scenario uses Collector Contrib `0.140.1`, Python SDK `1.39.1`, and Node SDK `2.12.1` to verify parallel services, late roots, attribute filtering, all three OTLP signals, alerting, span correlation, and persistent queue recovery after abrupt Collector restart.

For continuous local collection using [collector/datalog.yaml](../collector/datalog.yaml):

```sh
export DATALOG_ENDPOINT=http://127.0.0.1:4310
export DATALOG_PROJECT=local
export DATALOG_COLLECTOR_QUEUE="$PWD/.data/collector-queue"
# Load DATALOG_INGESTION_KEY from your secret store.
otelcol-contrib --config collector/datalog.yaml
export OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318
export OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
```
