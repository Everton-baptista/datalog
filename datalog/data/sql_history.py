"""Turn bounded successful SQL audit entries into observed OpenLineage edges."""

from datetime import datetime, timezone
import uuid

from datalog.auth.access import AccessError
from datalog.data.data_lineage import label
from datalog.core.validation import Invalid, text, timestamp

DIALECTS = {"postgres", "snowflake", "bigquery", "databricks"}


def event(body, config, source):
    if body.get("status") != "success":
        return None
    query_id = label(body.get("query_id"), "query ID", 180)
    statement = text(body.get("sql"), "SQL", limit=16384)
    if body.get("event_time") is None:
        raise Invalid("Provide the SQL record time")
    at = timestamp(body["event_time"])
    namespace = config["namespace"]
    dialect = config.get("engine", "postgres")
    if dialect not in DIALECTS:
        raise Invalid("Dialect unavailable")
    try:
        from sqlglot import exp, parse, parse_one
        from sqlglot.errors import SqlglotError
        from sqlglot.lineage import lineage
        from sqlglot.optimizer.normalize_identifiers import normalize_identifiers
        from sqlglot.optimizer.scope import traverse_scope
        from sqlglot.schema import MappingSchema
    except ImportError as error:
        raise AccessError(503, 'Install the lineage extra to process SQL: pip install "datalog[lineage]"') from error

    def path(table):
        return tuple(part.name for part in normalize_identifiers(table.copy(), dialect=dialect).parts)

    try:
        schema = MappingSchema(dialect=dialect)
        by_table = {}
        for dataset_name, table in config["tables"].items():
            columns = table.get("lineage_columns")
            if not columns:
                continue
            configured = parse_one(
                table.get("sql_name") or table["schema"] + "." + table["table"], read=dialect, into=exp.Table
            )
            key = path(configured)
            if len(key) < 2 or key in by_table:
                raise Invalid("Ambiguous lineage table")
            schema.add_table(configured, columns)
            by_table[key] = dataset_name
        statements = [item for item in parse(statement, read=dialect) if item is not None]
        if len(statements) != 1:
            raise Invalid("Send only a completed statement")
        command = statements[0]
        if not isinstance(command, (exp.Insert, exp.Create)) or not isinstance(command.expression, exp.Select):
            raise Invalid("Automatic lineage supports INSERT SELECT and CREATE TABLE AS SELECT")
        if isinstance(command, exp.Create) and command.kind != "TABLE":
            raise Invalid("Only CREATE TABLE AS is supported")
        target = command.this
        dest_names = (
            [normalize_identifiers(item.copy(), dialect=dialect).name for item in target.expressions]
            if isinstance(target, exp.Schema)
            else None
        )
        if isinstance(command, exp.Insert) and not dest_names:
            raise Invalid("INSERT must declare the destination columns")
        destination = target.this if isinstance(target, exp.Schema) else target
        if not isinstance(destination, exp.Table) or path(destination) not in by_table:
            raise Invalid("SQL target not registered for lineage")
        scopes = traverse_scope(command.expression)
        known = set()
        for scope in scopes:
            for _, selected in scope.selected_sources.values():
                if isinstance(selected, exp.Table):
                    key = path(selected)
                    if key not in by_table:
                        raise Invalid("SQL source not registered or not qualified")
                    known.add(by_table[key])
        if len(known) > 40:
            raise Invalid("SQL references more than 40 sources")
        fields = lineage(None, command.expression, schema=schema, dialect=dialect, validate_qualify_columns=True)
        aliases = command.expression.named_selects
        if len(fields) > 200 or (dest_names and len(dest_names) != len(fields)):
            raise Invalid("Column count does not match the destination")
        if not dest_names and (len(aliases) != len(fields) or any(not name or name == "*" for name in aliases)):
            raise Invalid("Provide explicit, unique names in the projection")
        mapped = {}
        for dest, tree in zip(dest_names or fields, fields.values()):
            if not schema.has_column(destination, dest):
                raise Invalid("Target column not registered")
            mapped_inputs = set()
            for child in tree.walk():
                if child.downstream or not isinstance(child.expression, exp.Table):
                    continue
                column = parse_one(child.name, read=dialect, into=exp.Column)
                key = path(child.expression)
                if key not in by_table or not schema.has_column(child.expression, column):
                    raise Invalid("Source column not resolved")
                mapped_inputs.add((by_table[key], column.name))
            if len(mapped_inputs) > 40:
                raise Invalid("Column references more than 40 inputs")
            mapped[dest] = {
                "inputFields": [
                    {"namespace": namespace, "name": name, "field": field} for name, field in sorted(mapped_inputs)
                ]
            }
        if len(mapped) != len(fields):
            raise Invalid("Duplicate columns in the destination")
    except (SqlglotError, RecursionError, TypeError):
        raise Invalid("SQL is ambiguous or outside the supported statements") from None

    run = str(uuid.uuid5(uuid.NAMESPACE_URL, f"datalog-sql-history:{namespace}:{source}:{query_id}"))
    return {
        "eventType": "COMPLETE",
        "eventTime": datetime.fromtimestamp(at, timezone.utc).isoformat(),
        "job": {"namespace": namespace, "name": "sql_history." + source},
        "run": {"runId": run},
        "inputs": [{"namespace": namespace, "name": name} for name in sorted(known)],
        "outputs": [
            {
                "namespace": namespace,
                "name": by_table[path(destination)],
                "facets": {"columnLineage": {"fields": mapped}},
            }
        ],
    }
