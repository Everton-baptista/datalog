"""Reproduce the 100k-span Datalog pilot and same-data local engine comparison."""

import argparse
import csv
from concurrent.futures import ThreadPoolExecutor
import gzip
from http.server import ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import platform
import tempfile
import threading
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import uuid

import chdb
from chdb.session import Session
import duckdb
import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
    ExportTraceServiceResponse,
)

from datalog.app import Store, make_handler
from datalog.core.retention import sweep

DAY = 86400
COLS = (
    "project,trace_id,span_id,parent_span_id,name,service,env,kind,status_code,start_ns,end_ns,resource,scope,payload"
)
TYPES = {
    "project": "VARCHAR",
    "trace_id": "VARCHAR",
    "span_id": "VARCHAR",
    "parent_span_id": "VARCHAR",
    "name": "VARCHAR",
    "service": "VARCHAR",
    "env": "VARCHAR",
    "kind": "INTEGER",
    "status_code": "INTEGER",
    "start_ns": "BIGINT",
    "end_ns": "BIGINT",
    "resource": "VARCHAR",
    "scope": "VARCHAR",
    "payload": "VARCHAR",
}
CH_TYPES = ",".join(
    f"{name} {({'VARCHAR': 'String', 'INTEGER': 'Int32', 'BIGINT': 'Int64'})[kind]}" for name, kind in TYPES.items()
)


def p95(values):
    return round(sorted(values)[max(0, math.ceil(0.95 * len(values)) - 1)], 3) if values else None


def trace_id(index):
    return f"{(index // 5) + 1:032x}"


def start_ns(index, total, now_ns):
    # Spread over 8 days so the same fixture exercises both retention windows.
    return now_ns - 8 * DAY * 10**9 + index * (8 * DAY * 10**9) // total


def request_body(first, end, total, now_ns):
    message = ExportTraceServiceRequest()
    services = {}
    for i in range(first, end):
        service = i % 32
        if service not in services:
            group = message.resource_spans.add()
            group.resource.attributes.add(key="service.name").value.string_value = f"service-{service:02d}"
            group.resource.attributes.add(key="deployment.environment.name").value.string_value = "benchmark"
            services[service] = group.scope_spans.add()
        start = start_ns(i, total, now_ns)
        parent = (i - i % 5 + 1).to_bytes(8, "big") if i % 5 else b""
        span = services[service].spans.add(
            trace_id=bytes.fromhex(trace_id(i)),
            span_id=(i + 1).to_bytes(8, "big"),
            parent_span_id=parent,
            name="request",
            kind=2,
            start_time_unix_nano=start,
            end_time_unix_nano=start + (i % 100 + 1) * 10**6,
        )
        span.status.code = 2 if i % 31 == 0 else 1
        span.attributes.add(key="datalog.bench_id").value.string_value = f"request-{i:09d}"
    return gzip.compress(message.SerializeToString(), compresslevel=1)


def sql_queries(table, lower, upper, total):
    target = trace_id(total - 5)
    attr = f"request-{total - 1:09d}"
    return (
        f"SELECT count(*) FROM {table} WHERE project='pilot' AND trace_id='{target}'",
        f"""SELECT service,count(*),sum(CASE WHEN status_code=2 THEN 1 ELSE 0 END)
            FROM {table} WHERE project='pilot' AND start_ns BETWEEN {lower} AND {upper}
            GROUP BY service ORDER BY service""",
        f"""SELECT count(*) FROM {table} WHERE project='pilot' AND start_ns BETWEEN {lower} AND {upper}
            AND CAST(payload AS VARCHAR) LIKE '%{attr}%'""",
    )


def benchmark_queries(factory, queries, clients, repeats=12):
    barrier = threading.Barrier(clients)

    def worker():
        with factory() as run:
            baseline = [run(query) for query in queries]
            barrier.wait(timeout=30)
            elapsed = []
            for i in range(repeats):
                before = time.perf_counter()
                result = run(queries[i % len(queries)])
                elapsed.append((time.perf_counter() - before) * 1000)
                if result != baseline[i % len(queries)]:
                    raise RuntimeError("Query changed during a read-only run")
            return baseline, elapsed

    with ThreadPoolExecutor(max_workers=clients) as pool:
        observed = list(pool.map(lambda _: worker(), range(clients)))
    return observed[0][0], {"queries": clients * repeats, "p95_ms": p95([x for _, times in observed for x in times])}


def run_comparison(store, work, now_ns, total, clients):
    from contextlib import contextmanager

    csv_path, parquet = work / "spans.csv", work / "spans.parquet"
    with csv_path.open("w", newline="") as stream, store.connect() as db:
        writer = csv.writer(stream)
        writer.writerow(COLS.split(","))
        selected = COLS.replace("resource,scope,payload", "resource::text,scope::text,payload::text")
        with db.cursor(name="export_for_pilot") as rows:
            rows.execute(f"SELECT {selected} FROM otlp_spans WHERE project='pilot' ORDER BY start_ns")
            for record in rows:
                writer.writerow(record.values())
        pg_size = db.execute("SELECT pg_total_relation_size('otlp_spans') n").fetchone()["n"]
        pg_version = db.execute("SHOW server_version").fetchone()["server_version"]

    with duckdb.connect(config={"threads": 1}) as db:
        db.execute(
            f"COPY (SELECT * FROM read_csv('{csv_path}',columns={TYPES},header=true)) TO '{parquet}' (FORMAT PARQUET, COMPRESSION ZSTD)"
        )

    with Session(str(work / "clickhouse")) as engine:
        engine.query(f"CREATE TABLE pilot ({CH_TYPES}) ENGINE=MergeTree ORDER BY (project,start_ns,trace_id,span_id)")
        engine.query(
            f"INSERT INTO pilot SELECT * FROM file('{csv_path}','CSVWithNames','{CH_TYPES}') SETTINGS max_threads=1"
        )
        ch_size = json.loads(
            str(
                engine.query(
                    "SELECT sum(bytes_on_disk) n FROM system.parts WHERE table='pilot' AND active FORMAT JSONCompact"
                )
            )
        )["data"][0][0]
        version = str(engine.query("SELECT version()", "CSV")).strip().strip('"')

        @contextmanager
        def pg():
            with store.connect() as db:
                yield lambda query: [tuple(row.values()) for row in db.execute(query).fetchall()]

        @contextmanager
        def ch():
            # chDB can serialize its shared engine internally. Four callers still issue work concurrently.
            def run(query):
                result = engine.query(query + " SETTINGS max_threads=1", "JSONCompact")
                return [tuple(row) for row in json.loads(str(result))["data"]]

            yield run

        @contextmanager
        def pq():
            with duckdb.connect(config={"threads": 1}) as db:
                yield lambda query: db.execute(query).fetchall()

        results = {}
        for days in (1, 7):
            metrics = {}
            reference = None
            for name, table, factory in [
                ("postgresql", "otlp_spans", pg),
                ("clickhouse_embedded", "pilot", ch),
                ("parquet_duckdb", f"read_parquet('{parquet}')", pq),
            ]:
                queries = sql_queries(table, now_ns - days * DAY * 10**9, now_ns, total)
                result, measure = benchmark_queries(factory, queries, clients)
                if reference is None:
                    reference = result
                elif result != reference:
                    raise RuntimeError(f"Query results disagree for {name}: {result!r} != {reference!r}")
                metrics[name] = measure
            results[f"{days}d"] = metrics
    return {
        "queries_by_window": results,
        "bytes_per_span": {
            "postgresql": round(pg_size / total, 2),
            "clickhouse_embedded": round(int(ch_size) / total, 2),
            "parquet_duckdb": round(parquet.stat().st_size / total, 2),
        },
        "versions": {
            "postgresql": pg_version,
            "clickhouse": version,
            "chdb": chdb.__version__,
            "duckdb": duckdb.__version__,
        },
    }


def pilot(store, work, total, clients):
    now = time.time()
    now_ns = int(now * 10**9)
    p = "pilot"
    secret = uuid.uuid4().hex
    store.access.create_user(p, secret)
    store.access.create_project(p, p)
    store.access.update_settings(
        p,
        p,
        {
            "retention_days": None,
            "request_quota": 600,
            "byte_quota": 134217728,
            "capture_content": False,
            "allowed_attributes": ["datalog.bench_id"],
        },
    )
    key = store.access.create_key(p, p, "Pilot")["token"]
    session = store.access.login(p, secret)

    class Quiet(make_handler(store)):
        def log_message(self, fmt, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Quiet)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}"
    finished = threading.Event()
    readers = ThreadPoolExecutor(max_workers=clients)

    def get(path, params):
        req = Request(
            f"{url}/api/{path}?" + urlencode({"project": p, **params}), headers={"Cookie": "datalog_session=" + session}
        )
        with urlopen(req, timeout=15) as response:
            return json.load(response)

    def read_while_ingesting():
        elapsed = []
        while not finished.is_set():
            before = time.perf_counter()
            get("traces", {"from": now - DAY, "to": now, "service": "service-01"})
            elapsed.append((time.perf_counter() - before) * 1000)
            finished.wait(0.05)
        return elapsed

    try:
        futures = [readers.submit(read_while_ingesting) for _ in range(clients)]
        elapsed, rejected, wire_bytes = [], 0, 0
        wall = time.perf_counter()
        for start in range(0, total, 1000):
            data = request_body(start, min(total, start + 1000), total, now_ns)
            req = Request(
                f"{url}/v1/traces?project=pilot",
                data=data,
                headers={
                    "Content-Type": "application/x-protobuf",
                    "Content-Encoding": "gzip",
                    "Authorization": "Bearer " + key,
                },
            )
            wire_bytes += len(data)
            before = time.perf_counter()
            with urlopen(req, timeout=20) as response:
                reply = ExportTraceServiceResponse.FromString(response.read())
                rejected += reply.partial_success.rejected_spans
            elapsed.append((time.perf_counter() - before) * 1000)
        seconds = time.perf_counter() - wall
        finished.set()
        query_times = [sample for future in futures for sample in future.result()]
        visibility_start = time.perf_counter()
        last = get("trace", {"id": trace_id(total - 1)})
        assert any(span["span_id"] == f"{total:016x}" for span in last["spans"])
        visibility_ms = (time.perf_counter() - visibility_start) * 1000
        with store.connect() as db:
            stored = db.execute("SELECT count(*) n FROM otlp_spans WHERE project='pilot'").fetchone()["n"]
        if stored != total or rejected:
            raise RuntimeError(f"Ingested {stored}/{total}; rejected {rejected}")
    finally:
        finished.set()
        readers.shutdown()
        server.shutdown()
        server.server_close()
    result = {
        "fixture": {
            "spans": total,
            "read_clients": clients,
            "age_distribution_days": 8,
            "high_cardinality_values": total,
        },
        "host": {"platform": platform.machine(), "python": platform.python_version(), "cpu_visible": os.cpu_count()},
        "datalog_http": {
            "accepted_per_second": round(stored / seconds, 2),
            "stored": stored,
            "rejected": rejected,
            "loss": total - stored,
            "ingest_p95_ms": p95(elapsed),
            "concurrent_query_p95_ms": p95(query_times),
            "concurrent_queries": len(query_times),
            "last_batch_ack_to_visible_probe_ms": round(visibility_ms, 3),
            "compressed_wire_bytes_per_span": round(wire_bytes / total, 2),
        },
        "storage": run_comparison(store, work, now_ns, total, clients),
    }
    checks = {}
    for days in (7, 1):
        with store.connect() as db:
            db.execute("UPDATE projects SET retention_days=%s WHERE id='pilot'", (days,))
        before = time.perf_counter()
        while sweep(store.connect, now).get("otlp_spans", 0):
            pass
        with store.connect() as db:
            actual = db.execute("SELECT count(*) n FROM otlp_spans WHERE project='pilot'").fetchone()["n"]
        boundary = int((now - days * DAY) * 1e9)
        expected = sum(start_ns(i, total, now_ns) + (i % 100 + 1) * 10**6 >= boundary for i in range(total))
        if actual != expected:
            raise RuntimeError(f"Retention {days} days: expected {expected}, observed {actual}")
        checks[f"{days}d"] = {"retained": actual, "sweep_ms": round((time.perf_counter() - before) * 1000, 2)}
    result["retention"] = checks
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spans", type=int, default=100000)
    parser.add_argument("--clients", type=int, default=4)
    args = parser.parse_args()
    if args.spans < 10 or not 1 <= args.clients <= 8:
        parser.error("Use ao menos 10 spans e de 1 a 8 leitores")
    dsn = os.environ["TEST_DATABASE_URL"]
    schema = "datalog_benchmark_" + uuid.uuid4().hex
    with psycopg.connect(dsn) as db:
        db.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    store = Store(make_conninfo(dsn, options=f"-c search_path={schema}"))
    try:
        with tempfile.TemporaryDirectory(prefix="datalog-pilot-") as directory:
            print(json.dumps(pilot(store, Path(directory), args.spans, args.clients), indent=2))
    finally:
        store.close()
        with psycopg.connect(dsn) as db:
            db.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


if __name__ == "__main__":
    main()
