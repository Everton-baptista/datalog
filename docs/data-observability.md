# Data Quality & Lineage in Datalog

The **Data** screen brings together data products, monitored assets, quality check history, and OpenLineage pipeline runs. Alerts link directly to the triggering observation, and AI agent spans expose the exact tables, columns, prior quality state, and producing jobs behind retrieved context.

## Configuring a Read-Only Source

Configure `DATALOG_DATA_SOURCES` with the projects and authorized relations. Use a read-only database role limited to approved tables. PostgreSQL scans execute in `REPEATABLE READ READ ONLY` transactions with strict statement and lock timeouts. Name each dataset to match its OpenLineage producer.

```json
{
  "warehouse": {
    "dsn_env": "WAREHOUSE_READONLY_DSN",
    "namespace": "postgres://warehouse:5432",
    "projects": ["local"],
    "tables": {
      "analytics.reporting.orders": {
        "schema": "reporting",
        "table": "orders",
        "lookup_column": "order_id",
        "agent_columns": ["region", "status"],
        "agent_adapters": ["approved"],
        "dashboard_columns": ["region", "amount"]
      },
      "analytics.reporting.expected_orders": {
        "schema": "reporting",
        "table": "expected_orders"
      }
    }
  }
}
```

Cloud warehouses (Snowflake, BigQuery, Databricks) are also supported via the `warehouses` extra (`pip install -e ".[warehouses]"`) (`datalog/warehouse_sources.py`).

In **Data → Register asset**, configure quality policies covering freshness, row volume bounds and median-based drift, `not_null`/`unique` column checks, schema drift detection, and aggregate or key-based row reconciliation (`datalog/reconcile_rows.py`):

```json
{
  "freshness": {"column": "updated_at", "max_age_seconds": 900},
  "volume": {"min_rows": 1, "max_change_percent": 30},
  "checks": [{"kind": "unique", "column": "order_id", "max_violations": 0}],
  "reconcile": {"target": "<registered-asset-id>", "metric": "count", "tolerance": 0}
}
```

Use **Accept current schema** to update the schema baseline after reviewing a drift alert.

## Pipeline Lineage, SQL History & Agent Provenance

- **OpenLineage**: emit RunEvents to `POST /api/v1/lineage?project=<project>` with an ingestion key (supports state transitions, inputs/outputs, and `columnLineage.fields`). Use **Monitor job** to alert on failures or max duration breaches.
- **SQL Audit Lineage**: send `INSERT SELECT` / `CREATE TABLE AS SELECT` audit entries to `POST /api/v1/lineage/sql?project=<project>` (parsed via `sqlglot` in `datalog/sql_history.py`).
- **Agent Retrieval Provenance**: authorize model adapters in `DATALOG_MODEL_ADAPTERS` and `agent_adapters`, then invoke `POST /api/v1/agents/gateway` with `retrieval: [{"asset_id": "<id>", "key": 7}]`. Datalog records table/column provenance and prior quality state on the resulting span without persisting raw lookup values in lineage metadata.
- **Data Products & BI Impact**: group eligible tables and consumers in **Data → Create product**, link HTTPS BI report URLs to consumer assets, and run **Column impact** to trace downstream mappings up to 8 hops.

## Try it locally

Point any OpenLineage emitter (Airflow, dbt, Spark) at `POST /api/v1/lineage?project=local` with an ingestion key. [examples/airflow_retries.py](../examples/airflow_retries.py) is an Airflow DAG whose first attempt fails on purpose, so retries and recovery show up under **Data → Jobs**. SQL lineage needs the `lineage` extra (`pip install -e ".[lineage]"`).

Open the printed URL and run `playwright-cli run-code --filename=examples/data_acceptance.js` to exercise agent provenance, schema drift alerting, and column-level lineage end-to-end.

References: [OpenLineage Object Model](https://openlineage.io/docs/spec/object-model/) · [Column Lineage Facet](https://openlineage.io/docs/spec/facets/dataset-facets/column_lineage_facet/) · [PostgreSQL Transactions](https://www.postgresql.org/docs/current/sql-set-transaction.html).
