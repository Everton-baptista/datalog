# Datalog Documentation

Central index for Datalog's technical architecture, API contracts, operational guides, benchmarks and feature references.

## Architecture & Contracts

| Document | Scope |
| --- | --- |
| [architecture.md](architecture.md) | System topology (Mermaid diagrams), backend domain stores, PostgreSQL migrations (`001`–`014`), background workers, zero-dependency UI/charts engine and security model. |
| [api-v1.md](api-v1.md) | HTTP `/api/v1/` and OTLP `/v1/*` contract, RBAC roles, user management, quotas, privacy allowlists, retention semantics and reproduction steps. |

## Operational & Feature Guides

| Document | Scope |
| --- | --- |
| [features.md](features.md) | Full stack with Collector, backup/restore & HA recovery, OIDC SSO, cross-signal investigation, SLOs, AI workflows (Playground, Gateway, Evaluators), RUM/Replay, Synthetics, Profiling and Infrastructure. |
| [data-observability.md](data-observability.md) | Read-only warehouse connectors, quality policies (freshness, volume, schema drift, rules, reconciliation), OpenLineage, SQL history lineage, Data Products and column-to-BI impact. |
| [benchmark-100k.md](benchmark-100k.md) | 100k-span local benchmark results, p95 latency and comparison against ClickHouse (`chDB`) and Parquet (`DuckDB`). |

## Project & Governance

- [README.md](../README.md) · [CONTRIBUTING.md](../CONTRIBUTING.md) · [CHANGELOG.md](../CHANGELOG.md)
- [SECURITY.md](../.github/SECURITY.md) · [CODE_OF_CONDUCT.md](../.github/CODE_OF_CONDUCT.md) · [THIRD_PARTY.md](../THIRD_PARTY.md) · [TRADEMARKS.md](../TRADEMARKS.md) · [LICENSE](../LICENSE) · [NOTICE](../NOTICE)
