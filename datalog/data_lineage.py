"""Project-scoped OpenLineage run metadata and observed dataset provenance."""

from collections import deque
import hashlib
import json
import re
from urllib.parse import urlsplit

from psycopg.types.json import Jsonb

from datalog.access import AccessError
from datalog.validation import Invalid, text, timestamp

STATES = ("START", "RUNNING", "COMPLETE", "FAIL", "ABORT", "OTHER")
TERMINAL = frozenset(("COMPLETE", "FAIL", "ABORT"))
SAFE_NAMESPACE = re.compile(r"^[A-Za-z0-9._:/-]+$")


def label(value, field, limit=300):
    result = text(value, field, limit=limit)
    if not result.isprintable():
        raise Invalid(f"{field} contains invalid characters")
    return result


def identity(value):
    if not isinstance(value, dict):
        raise Invalid("Invalid dataset or job")
    namespace = label(value.get("namespace"), "namespace")
    address = urlsplit(namespace)
    if not SAFE_NAMESPACE.fullmatch(namespace) or address.username or address.password:
        raise Invalid("Use a namespace without credentials or parameters")
    return namespace, label(value.get("name"), "name")


def digest(*parts):
    return hashlib.sha256(
        json.dumps(parts, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def dataset(db, project, namespace, name):
    ident = digest(namespace, name)[:32]
    db.execute(
        """INSERT INTO data_assets(project,id,namespace,name) VALUES(%s,%s,%s,%s)
        ON CONFLICT(project,namespace,name) DO NOTHING""",
        (project, ident, namespace, name),
    )
    return ident


def asset(db, project, ident):
    row = db.execute(
        "SELECT * FROM data_assets WHERE project=%s AND id=%s", (project, label(ident, "asset", 32))
    ).fetchone()
    if not row:
        raise AccessError(404, "Data asset not found in this project")
    return row


def describe(row):
    return {key: row[key] for key in ("id", "namespace", "name")}


def object_field(value, field):
    if not isinstance(value, dict):
        raise Invalid(f"{field}: provide an object")
    return value


def array_field(value, field, maximum):
    if not isinstance(value, list) or len(value) > maximum:
        raise Invalid(f"{field}: provide a list of up to {maximum} items")
    return value


def event_details(body):
    inputs = [identity(item) for item in array_field(body.get("inputs", []), "inputs", 40)]
    outputs = array_field(body.get("outputs", []), "outputs", 40)
    columns = set()
    for output in outputs:
        destination = identity(output)
        facets = object_field(output.get("facets", {}), "facets")
        lineage = object_field(facets.get("columnLineage", {}), "columnLineage")
        fields = object_field(lineage.get("fields", {}), "fields")
        if len(fields) > 200:
            raise Invalid("Lineage exceeds 200 columns per asset")
        for out, spec in fields.items():
            output_field = label(out, "output column", 200)
            spec = object_field(spec, "column lineage")
            for source in array_field(spec.get("inputFields", []), "inputFields", 40):
                columns.add(
                    (*identity(source), label(source.get("field"), "source column", 200), *destination, output_field)
                )
                if len(columns) > 1000:
                    raise Invalid("Lineage exceeds 1,000 column relations per event")
    details = {
        "inputs": sorted(set(inputs)),
        "outputs": sorted({identity(item) for item in outputs}),
        "columns": sorted(columns),
    }
    run = object_field(body.get("run"), "run")
    facets = object_field(run.get("facets", {}), "run.facets")
    parent = object_field(facets.get("parent", {}), "parent")
    if parent.get("run"):
        details["parent_run_id"] = label(object_field(parent["run"], "parent.run").get("runId"), "runId pai", 100)
    nominal = object_field(facets.get("nominalTime", {}), "nominalTime")
    if nominal.get("nominalStartTime"):
        details["nominal_at"] = timestamp(nominal["nominalStartTime"])
    airflow = facets.get("airflow")
    if isinstance(airflow, dict):
        dag, task = airflow.get("dagRun"), airflow.get("taskInstance")
        if isinstance(dag, dict) and isinstance(task, dict):
            run_id, dag_id = dag.get("run_id"), dag.get("dag_id")
            attempt = task.get("try_number")
            partition = task.get("map_index")
            clear = dag.get("clear_number")
            if (
                isinstance(run_id, str)
                and 0 < len(run_id) <= 250
                and isinstance(dag_id, str)
                and 0 < len(dag_id) <= 250
                and type(attempt) is int
                and 1 <= attempt <= 10000
                and (partition is None or type(partition) is int)
                and (clear is None or type(clear) is int)
            ):
                details.update(
                    attempt_group=digest(
                        *identity(body["job"]), dag_id, run_id, clear or 0, -1 if partition is None else partition
                    ),
                    attempt=attempt,
                )
    return details


class DataLineage:
    def __init__(self, connect):
        self.connect = connect

    def receive(self, project, body, *, single=False):
        state = body.get("eventType")
        if state not in STATES or body.get("eventTime") is None:
            raise Invalid("Provide the RunEvent eventType and eventTime")
        at = timestamp(body["eventTime"])
        details = event_details(body)
        ident = label(body["run"].get("runId"), "runId", 100)
        namespace, name = identity(body.get("job"))
        key = digest(state, at, details)
        with self.connect() as db:
            db.execute(
                """INSERT INTO data_job_runs(project,id,namespace,name,state,updated_at)
                VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                (project, ident, namespace, name, state, at),
            )
            run = db.execute(
                "SELECT * FROM data_job_runs WHERE project=%s AND id=%s FOR UPDATE", (project, ident)
            ).fetchone()
            if (run["namespace"], run["name"]) != (namespace, name):
                raise AccessError(409, "The runId already belongs to another job")
            if db.execute(
                "SELECT 1 FROM data_job_events WHERE project=%s AND run_id=%s AND digest=%s", (project, ident, key)
            ).fetchone():
                return {"run_id": ident, "accepted": 0, "state": run["state"]}
            events = db.execute(
                "SELECT * FROM data_job_events WHERE project=%s AND run_id=%s", (project, ident)
            ).fetchall()
            if single and events:
                raise AccessError(409, "The query ID already has published lineage")
            if len(events) >= 100:
                raise Invalid("The run exceeds 100 events")
            db.execute(
                "INSERT INTO data_job_events VALUES(%s,%s,%s,%s,%s,%s)",
                (project, ident, key, at, state, Jsonb(details)),
            )
            events.append({"event_at": at, "state": state, "digest": key, "details": details})
            state = self.update_run(db, project, ident, events)
        return {"run_id": ident, "accepted": 1, "state": state}

    @staticmethod
    def update_run(db, project, ident, events):
        events.sort(key=lambda event: (event["event_at"], STATES.index(event["state"]), event["digest"]))
        terminal = next((event for event in reversed(events) if event["state"] in TERMINAL), None)
        final = terminal or events[-1]
        inputs, outputs, fields = set(), set(), set()
        parent, nominal = None, None
        retry = {}
        for event in events:
            if terminal and event["event_at"] > final["event_at"]:
                continue
            observed = event["details"]
            inputs.update(tuple(item) for item in observed["inputs"])
            outputs.update(tuple(item) for item in observed["outputs"])
            fields.update(tuple(item) for item in observed["columns"])
            parent = observed.get("parent_run_id", parent)
            nominal = observed.get("nominal_at", nominal)
            if "attempt_group" in observed:
                retry = {key: observed[key] for key in ("attempt_group", "attempt")}
        inputs.update(edge[:2] for edge in fields)
        if len(inputs | outputs) > 120 or len(fields) > 1500:
            raise Invalid("Accumulated lineage exceeds the per-run limit")
        ids = {key: dataset(db, project, *key) for key in sorted(inputs | outputs)}
        starts = [
            event["event_at"]
            for event in events
            if event["state"] == "START" and event["event_at"] <= final["event_at"]
        ]
        details = {
            "inputs": [{"id": ids[(ns, name)], "namespace": ns, "name": name} for ns, name in sorted(inputs)],
            "outputs": [{"id": ids[(ns, name)], "namespace": ns, "name": name} for ns, name in sorted(outputs)],
            "columns": [
                {
                    "input_id": ids[edge[:2]],
                    "input_field": edge[2],
                    "output_id": ids[edge[3:5]],
                    "output_field": edge[5],
                }
                for edge in sorted(fields)
            ],
            "started_at": min(starts) if starts else None,
            "ended_at": final["event_at"] if terminal else None,
            "parent_run_id": parent,
            "nominal_at": nominal,
            **retry,
        }
        db.execute(
            "UPDATE data_job_runs SET state=%s,updated_at=%s,details=%s WHERE project=%s AND id=%s",
            (final["state"], final["event_at"], Jsonb(details), project, ident),
        )
        db.execute("DELETE FROM data_job_outputs WHERE project=%s AND run_id=%s", (project, ident))
        if final["state"] == "COMPLETE" and outputs:
            db.cursor().executemany(
                "INSERT INTO data_job_outputs VALUES(%s,%s,%s,%s)",
                [(project, ident, ids[key], final["event_at"]) for key in sorted(outputs)],
            )
        return final["state"]

    def runs(self, project, params):
        with self.connect() as db:
            if params.get("id"):
                item = db.execute(
                    "SELECT * FROM data_job_runs WHERE project=%s AND id=%s",
                    (project, label(params["id"], "runId", 100)),
                ).fetchone()
                if not item:
                    raise AccessError(404, "Data run not found in this project")
                group = item["details"].get("attempt_group")
                attempts = self.attempts(db, project, group) if group else []
                return {
                    "item": item,
                    "attempts": [{k: v for k, v in attempt.items() if k != "details"} for attempt in attempts],
                }
            items = db.execute(
                """SELECT id,namespace,name,state,updated_at,
                    jsonb_build_object('started_at',details->'started_at','ended_at',details->'ended_at',
                                       'parent_run_id',details->'parent_run_id','nominal_at',details->'nominal_at', 'attempt',details->'attempt') details
                    FROM data_job_runs WHERE project=%s ORDER BY updated_at DESC,id LIMIT 100""",
                (project,),
            ).fetchall()
        return {"items": items}

    @staticmethod
    def attempts(db, project, group, as_of=None):
        return db.execute(
            """SELECT id,state,updated_at,details,(details->>'attempt')::int attempt
            FROM data_job_runs WHERE project=%s AND details ? 'attempt_group' AND details->>'attempt_group'=%s
            AND (%s::float8 IS NULL OR updated_at<=%s)
            ORDER BY attempt DESC,updated_at DESC,id LIMIT 30""",
            (project, group, as_of, as_of),
        ).fetchall()

    @staticmethod
    def upstream(db, project, ident, as_of, columns=None):
        queue = deque([(ident, None if columns is None else tuple(sorted(set(columns))), as_of, 0)])
        seen, edges = set(), {}
        truncated, column_count = False, 0
        while queue:
            target, selected, cutoff, depth = queue.popleft()
            if (target, selected, cutoff) in seen:
                continue
            if len(seen) >= 30 or depth >= 12:
                truncated = True
                continue
            seen.add((target, selected, cutoff))
            run = db.execute(
                """SELECT r.id,r.namespace,r.name,r.details,o.completed_at
                FROM data_job_outputs o JOIN data_job_runs r ON (r.project,r.id)=(o.project,o.run_id)
                WHERE o.project=%s AND o.asset_id=%s AND o.completed_at<=%s
                ORDER BY o.completed_at DESC,o.run_id LIMIT 1""",
                (project, target, cutoff),
            ).fetchone()
            if not run:
                continue
            for item in run["details"]["inputs"]:
                source = item["id"]
                if source == target:
                    continue
                mapping = [
                    edge
                    for edge in run["details"]["columns"]
                    if edge["input_id"] == source
                    and edge["output_id"] == target
                    and (selected is None or edge["output_field"] in selected)
                ]
                if len(edges) >= 80 and (target, source, run["id"]) not in edges:
                    truncated = True
                    continue
                edge = edges.setdefault(
                    (target, source, run["id"]),
                    {
                        "target_id": target,
                        "source": item,
                        "run_id": run["id"],
                        "job": run["name"],
                        "completed_at": run["completed_at"],
                        "columns": [],
                    },
                )
                new_columns = [item for item in mapping if item not in edge["columns"]]
                room = 240 - column_count
                edge["columns"].extend(new_columns[:room])
                column_count += min(room, len(new_columns))
                truncated |= len(new_columns) > room
                next_columns = (
                    tuple(sorted({item["input_field"] for item in mapping})) if selected is not None else None
                )
                before = run["details"]["started_at"] or run["completed_at"]
                queue.append((source, next_columns, min(before, cutoff), depth + 1))
        return {"edges": list(edges.values()), "truncated": truncated}
