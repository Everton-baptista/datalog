"""Read-only query generation for configured cloud warehouse sources."""

from contextlib import contextmanager
from datetime import date, datetime, time, timezone
import json
import os
import re

from datalog.auth.access import AccessError, integer
from datalog.core.validation import Invalid

ENGINES = {"postgres", "snowflake", "bigquery", "databricks"}
BIGQUERY_IDENTIFIER = re.compile(r"^[A-Za-z0-9_-]+$")


class CloudWarehouse:
    def __init__(self, config):
        self.config = config
        self.engine = config["engine"]
        self.catalog = config["catalog"]

    def ident(self, name):
        if self.engine == "bigquery":
            if not BIGQUERY_IDENTIFIER.fullmatch(name):
                raise Invalid("BigQuery identifier outside the supported set")
            return "`" + name + "`"
        quote = "`" if self.engine == "databricks" else '"'
        return quote + name.replace(quote, quote * 2) + quote

    def table(self, table):
        parts = [self.catalog, table["schema"], table["table"]]
        if self.engine == "bigquery":
            return "`" + ".".join(self.ident(p)[1:-1] for p in parts) + "`"
        return ".".join(map(self.ident, parts))

    @contextmanager
    def connection(self):
        raw = os.environ.get(self.config.get("dsn_env", ""))
        try:
            credentials = json.loads(raw) if raw else {}
            if not isinstance(credentials, dict):
                raise ValueError("Invalid warehouse credential")
            if self.engine != "bigquery" and not credentials:
                raise ValueError("Missing credentials")
            if self.engine == "snowflake":
                from snowflake.connector import connect

                allowed = ("account", "user", "role", "warehouse")
                options = {key: credentials[key] for key in allowed if key in credentials}
                if credentials.get("private_key_file"):
                    if credentials.get("password"):
                        raise Invalid("Configure a private key or password for Snowflake")
                    options.update(authenticator="SNOWFLAKE_JWT", private_key_file=credentials["private_key_file"])
                    if "private_key_password" in credentials:
                        options["private_key_password"] = credentials["private_key_password"]
                else:
                    options["password"] = credentials["password"]
                client = connect(
                    **options,
                    login_timeout=6,
                    network_timeout=12,
                    socket_timeout=8,
                    session_parameters={"STATEMENT_TIMEOUT_IN_SECONDS": 8, "TIMEZONE": "UTC"},
                )
            elif self.engine == "databricks":
                from databricks.sql import connect

                client = connect(
                    **{key: credentials[key] for key in ("server_hostname", "http_path", "access_token")},
                    _socket_timeout=8,
                    _retry_stop_after_attempts_duration=15,
                    session_configuration={"spark.sql.session.timeZone": "UTC"},
                )
            else:
                from google.cloud import bigquery
                from google.oauth2 import service_account

                account = service_account.Credentials.from_service_account_info(credentials) if credentials else None
                client = bigquery.Client(
                    project=self.catalog, credentials=account, location=self.config.get("location")
                )
            try:
                if self.engine == "databricks":
                    with client.cursor() as cursor:
                        cursor.execute("SET STATEMENT_TIMEOUT = 10")
                yield client
            finally:
                client.close()
        except (Invalid, AccessError):
            raise
        except Exception:
            raise AccessError(503, "Warehouse unavailable or invalid credential") from None

    def query(self, client, statement, params=(), limit=10000):
        if self.engine == "bigquery":
            from google.cloud import bigquery

            bound = [bigquery.ScalarQueryParameter(None, "STRING", value) for value in params]
            config = bigquery.QueryJobConfig(
                query_parameters=bound,
                job_timeout_ms=10000,
                maximum_bytes_billed=integer(
                    self.config.get("maximum_bytes_billed", 1_073_741_824), "maximum bytes", 1, 2**53 - 1
                ),
            )
            job = client.query(statement, job_config=config, timeout=8)
            try:
                return [tuple(row) for row in job.result(timeout=12, max_results=limit)]
            except TimeoutError:
                job.cancel(timeout=3)
                raise
        with client.cursor() as cursor:
            if self.engine == "snowflake":
                cursor.execute(statement, params, timeout=10)
            else:
                cursor.execute(statement, params)
            return cursor.fetchmany(limit)

    def schema(self, client, table):
        if self.engine == "bigquery":
            source = f"{self.ident(self.catalog)}.{self.ident(table['schema'])}.INFORMATION_SCHEMA.COLUMNS"
            condition, params = "table_name=?", (table["table"],)
        else:
            info = "INFORMATION_SCHEMA.COLUMNS" if self.engine == "snowflake" else "information_schema.columns"
            source = f"{self.ident(self.catalog)}.{info}"
            mark = "%s" if self.engine == "snowflake" else "?"
            condition, params = f"table_schema={mark} AND table_name={mark}", (table["schema"], table["table"])
        rows = self.query(
            client,
            f"SELECT column_name,data_type,is_nullable FROM {source} WHERE {condition} ORDER BY ordinal_position LIMIT 501",
            params,
            501,
        )
        if not 1 <= len(rows) <= 500:
            raise Invalid("Table unavailable or has more than 500 columns")
        return {
            name: {"type": kind.lower(), "nullable": str(nullable).upper() == "YES"} for name, kind, nullable in rows
        }

    def measurements(self, table, policy):
        with self.connection() as client:
            schema = self.schema(client, table)
            columns = ["count(*)"]
            keys, issues = ["rows"], []
            rule = policy.get("freshness")
            if rule:
                field = rule["column"]
                if schema.get(field, {}).get("type", "").startswith(("timestamp", "datetime", "date")):
                    columns.append(f"max({self.ident(field)})")
                    keys.append("latest")
                else:
                    issues.append({"kind": "freshness_column", "column": field})
            for index, check in enumerate(policy.get("checks", [])):
                if check["column"] not in schema:
                    issues.append({"kind": "check_column", "column": check["column"]})
                    continue
                col = self.ident(check["column"])
                columns.append(
                    f"count(*)-count({col})" if check["kind"] == "not_null" else f"count({col})-count(DISTINCT {col})"
                )
                keys.append(f"check_{index}")
            (result,) = self.query(client, f"SELECT {','.join(columns)} FROM {self.table(table)}", limit=1)
        values = dict(zip(keys, result))
        latest = values.get("latest")
        if isinstance(latest, date):
            if not isinstance(latest, datetime):
                latest = datetime.combine(latest, time.min)
            values["latest"] = (latest if latest.tzinfo else latest.replace(tzinfo=timezone.utc)).timestamp()
        elif latest is not None:
            raise Invalid("Unexpected warehouse timestamp")
        return schema, values, issues

    def rows(self, table, fields, maximum):
        selection = ",".join(map(self.ident, fields))
        with self.connection() as client:
            return self.query(client, f"SELECT {selection} FROM {self.table(table)} LIMIT {maximum}", limit=maximum)
