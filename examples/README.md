# Examples & Verification Fixtures (`examples/`)

Reproducible telemetry demos, browser acceptance fixtures and end-to-end verification scripts.

## Quick Demos

- [otel_checkout.py](otel_checkout.py) — Sends a multi-service OTLP trace + correlated JSON log (`python examples/otel_checkout.py`).
- [agent_comparison.py](agent_comparison.py) — Seeds a versioned AI dataset (`demo/pagamentos`) for A/B comparison in **Distributed AI** (`python -m examples.agent_comparison`).
- [provider_sdks.py](provider_sdks.py) — Exercises Anthropic & Groq OpenInference instrumentation against a local mock endpoint.
- [airflow_retries.py](airflow_retries.py) — Emits OpenLineage retry attempts for job monitoring.
- [node/](node/package.json) — Node.js OpenTelemetry HTTP & payments trace fixtures (`npm ci --prefix examples/node`).

## Verification & Browser Acceptance

- [verify_collector.py](verify_collector.py) — End-to-end Collector Contrib test (auto-instrumentation, tail sampling, AI providers, persistent queue recovery).
- [verify_pipelines.py](verify_pipelines.py) — Multi-project Collector pipelines and queue reconciliation check.
- [verify_recovery.py](verify_recovery.py) — PostgreSQL backup/restore, standby promotion and multi-instance deduplication check.
- [browser_fixture.py](browser_fixture.py) + [browser_acceptance.js](browser_acceptance.js) — RUM, masked session replay and metric exemplar Playwright acceptance.
