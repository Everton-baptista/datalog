# Third-Party Dependencies & Licenses

Third-party libraries are installed separately. Datalog's Apache-2.0 license applies to the code in this repository; third-party packages retain their own licenses and attribution notices.

## Verified Python Runtime

- Google APIs Common Protos `1.76.0` and OpenTelemetry Proto `1.39.1` — Apache-2.0 ([Google APIs Common Protos](https://github.com/googleapis/google-cloud-python/tree/main/packages/googleapis-common-protos) · [OpenTelemetry Python](https://github.com/open-telemetry/opentelemetry-python)).
- Protobuf `6.33.6` — BSD-3-Clause ([Protobuf](https://github.com/protocolbuffers/protobuf/blob/main/LICENSE)).
- Psycopg & Psycopg Binary `3.3.6` and Psycopg Pool `3.3.3` — LGPL-3.0-only ([Psycopg](https://github.com/psycopg/psycopg)).
- Typing Extensions `4.16.0` — PSF-2.0 ([Typing Extensions](https://github.com/python/typing_extensions/blob/main/LICENSE)).
- PostgreSQL `18` — PostgreSQL License ([PostgreSQL](https://www.postgresql.org/about/licence/)).
- Optional OIDC SSO uses PyJWT `2.15.1` (MIT) and `cryptography` (Apache-2.0 / BSD-3-Clause) ([PyJWT](https://pypi.org/project/PyJWT/2.15.1/) · [cryptography](https://github.com/pyca/cryptography)).

## Optional Integrations

- Anthropic `0.84.0` (MIT), Groq `1.0.0` (Apache-2.0), OpenInference instrumentation for Anthropic `1.0.1` and Groq `0.1.15`, core `0.1.75` and semantic conventions `0.1.43` (Apache-2.0) ([Anthropic](https://pypi.org/project/anthropic/0.84.0/) · [Groq](https://pypi.org/project/groq/1.0.0/) · [OpenInference](https://github.com/Arize-ai/openinference)).
- PyYAML `6.0.3` (MIT) and Prometheus Client `0.24.1` (Apache-2.0 / BSD-2-Clause), used by optional Collector utilities ([PyYAML](https://pypi.org/project/PyYAML/6.0.3/) · [Prometheus Client](https://pypi.org/project/prometheus-client/0.24.1/)).
- Playwright `1.61.0` (Apache-2.0) and SQLGlot `30.21.0` (MIT) ([Playwright](https://pypi.org/project/playwright/1.61.0/) · [SQLGlot](https://pypi.org/project/sqlglot/30.21.0/)).
- Snowflake Connector `5.2.0`, Google Cloud BigQuery `3.47.0` and Databricks SQL Connector `4.9.0` — Apache-2.0 ([Snowflake](https://pypi.org/project/snowflake-connector-python/5.2.0/) · [BigQuery](https://pypi.org/project/google-cloud-bigquery/3.47.0/) · [Databricks](https://pypi.org/project/databricks-sql-connector/4.9.0/)).

## Examples, Fixtures & Benchmarks

- Official OpenTelemetry OTLP HTTP exporter for Python `1.39.1` and Collector Contrib `0.140.1` — Apache-2.0 ([OpenTelemetry Python](https://github.com/open-telemetry/opentelemetry-python) · [Collector Contrib](https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/main/LICENSE)).
- `opentelemetry-instrumentation-urllib` `0.60b1` and `opentelemetry-instrumentation-sqlite3` `0.60b1` — Apache-2.0 ([OpenTelemetry Python Contrib](https://github.com/open-telemetry/opentelemetry-python-contrib)).
- `openlineage-python` `1.55.0` (Apache-2.0, used in `examples/data_browser_fixture.py`) and helper container image `busybox:1.37` (GPL-2.0, isolated container initializing Collector queue permissions in `compose.yaml`) ([OpenLineage](https://github.com/OpenLineage/OpenLineage) · [BusyBox](https://busybox.net/license.html)).
- Node.js example dependencies (OpenTelemetry JS SDK & exporter) are pinned with declared licenses in [examples/node/package-lock.json](examples/node/package-lock.json).
- `chDB` `4.4.0` (Apache-2.0) and `DuckDB` `2.0.0` (MIT), used in the local storage benchmark ([chDB](https://github.com/chdb-io/chdb/blob/main/LICENSE) · [DuckDB](https://github.com/duckdb/duckdb-python/blob/main/LICENSE)).

Update this inventory whenever dependency versions change or third-party packages are bundled.

## UI Fonts & Brand Assets

- Geist and Geist Mono (variable fonts, Latin subset) — SIL Open Font License 1.1, located in `datalog/web/assets/` alongside `geist-OFL.txt` and `geist-mono-OFL.txt` ([Geist](https://github.com/vercel/geist-font)).
- The SynapseAI symbol and SynapseDev visual identity are not distributed under Apache-2.0. See [TRADEMARKS.md](TRADEMARKS.md).
