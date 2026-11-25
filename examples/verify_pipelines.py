"""Exercise two projects and reconcile all three signals across a Collector outage."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import uuid
from http.server import ThreadingHTTPServer
from urllib.request import Request, urlopen

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceRequest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

from datalog.app import Store, make_handler
from collector.configure import write_validated
from collector.reconcile import reconcile, scrape, snapshot
from examples.verify_collector import listening, port, wait_until


def fixtures():
    now = time.time_ns()
    ident = uuid.uuid4().bytes
    traces, logs, metrics = ExportTraceServiceRequest(), ExportLogsServiceRequest(), ExportMetricsServiceRequest()
    span = (
        traces.resource_spans.add()
        .scope_spans.add()
        .spans.add(
            trace_id=ident,
            span_id=ident[:8],
            name="queue-fixture",
            start_time_unix_nano=now - 1,
            end_time_unix_nano=now,
        )
    )
    span.attributes.add(key="gen_ai.input.messages").value.string_value = "never-in-queue"
    records = logs.resource_logs.add().scope_logs.add().log_records
    log = records.add(time_unix_nano=now, trace_id=ident, span_id=ident[:8], event_name="queue-fixture")
    log.body.string_value = '{"error.type":"from-json","password":"never-in-queue"}'
    log.attributes.add(key="log.record.uid").value.string_value = ident.hex()
    metric = metrics.resource_metrics.add().scope_metrics.add().metrics.add(name="queue-fixture")
    point = metric.gauge.data_points.add(time_unix_nano=now, as_int=1)
    point.attributes.add(key="password").value.string_value = "never-in-queue"
    return {"traces": traces, "logs": logs, "metrics": metrics}


def send(url, payloads):
    for signal, message in payloads.items():
        request = Request(f"{url}/v1/{signal}", message.SerializeToString(), {"Content-Type": "application/x-protobuf"})
        with urlopen(request, timeout=5) as reply:
            assert reply.status == 200


def verify(store, work, collector):
    backend_port, metrics_port, first_port, second_port = [port() for _ in range(4)]
    backend = f"http://127.0.0.1:{backend_port}"
    collector_metrics = f"http://127.0.0.1:{metrics_port}/metrics"
    metrics_token = uuid.uuid4().hex
    admin = "pipeline-admin"
    store.access.create_user(admin, uuid.uuid4().hex)
    env = {"DATALOG_ENDPOINT": backend, "DATALOG_COLLECTOR_METRICS_PORT": str(metrics_port)}
    projects = {}
    for name, listener in (("pipeline-a", first_port), ("pipeline-b", second_port)):
        key_env = "PIPELINE_" + name[-1].upper()
        store.access.create_project(admin, name)
        env[key_env] = store.access.create_key(name, admin, "collector")["token"]
        projects[name] = {"http": listener, "key_env": key_env}
    projects["pipeline-a"]["transform"] = {
        "logs": ['set(log.attributes["error.type"], "custom-a") where log.attributes["error.type"] == "from-json"'],
    }
    config = work / "pipeline.json"
    queue = work / "queue"
    os.environ.update(env)
    write_validated(projects, str(queue), collector, config)

    def start_backend():
        server = ThreadingHTTPServer(("127.0.0.1", backend_port), make_handler(store, metrics_token=metrics_token))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server

    def start_collector(name):
        with (work / name).open("wb") as output:
            proc = subprocess.Popen([collector, f"--config={config}"], stdout=output, stderr=output)
        try:
            wait_until(lambda: listening(first_port) and listening(second_port), "multiproject listeners")
        except RuntimeError:
            proc.kill()
            proc.wait()
            raise RuntimeError((work / name).read_text()[-4000:]) from None
        return proc

    def counts(name):
        return snapshot(scrape(collector_metrics), store.operational.render(store.pool).decode(), name)

    server = start_backend()
    process = None
    try:
        process = start_collector("before.log")
        a, b = fixtures(), fixtures()
        send(f"http://127.0.0.1:{second_port}", b)
        wait_until(lambda: all(x["accepted"] == 1 for x in counts("pipeline-b")["signals"].values()), "project B")
        assert all(x["received"] == 0 for x in counts("pipeline-a")["signals"].values())
        server.shutdown()
        server.server_close()
        server = None
        before = counts("pipeline-a")
        send(f"http://127.0.0.1:{first_port}", a)

        def persisted():
            if not all(x["collector_received"] == 1 for x in counts("pipeline-a")["signals"].values()):
                return False
            return all(
                any(
                    b"queue-fixture" in file.read_bytes()
                    for file in (queue / "pipeline-a" / signal).glob("*")
                    if file.is_file()
                )
                for signal in a
            )

        wait_until(persisted, "three disk queues")
        disk = b"".join(file.read_bytes() for file in (queue / "pipeline-a").rglob("*") if file.is_file())
        assert b"never-in-queue" not in disk
        offline = reconcile(before, counts("pipeline-a"))["signals"]
        assert all(x["collector_received"] == 1 and x["received"] == 0 for x in offline.values())
        process.kill()
        process.wait(timeout=5)

        server = start_backend()
        process = start_collector("after.log")
        wait_until(
            lambda: all(
                x["accepted"] == 1 and x["collector_sent"] == 1 for x in counts("pipeline-a")["signals"].values()
            ),
            "replay for every signal",
        )
        now = time.time_ns()
        a["traces"].resource_spans[0].scope_spans[0].spans.add(
            name="invalid", start_time_unix_nano=now, end_time_unix_nano=now
        )
        a["logs"].resource_logs[0].scope_logs[0].log_records.add(span_id=b"orphan!!", time_unix_nano=now)
        a["metrics"].resource_metrics[0].scope_metrics[0].metrics[0].gauge.data_points.add(as_int=0)
        send(f"http://127.0.0.1:{first_port}", a)
        wait_until(
            lambda: all(x["received"] == 3 for x in counts("pipeline-a")["signals"].values()),
            "resend with invalid data for every signal",
        )
        restored = counts("pipeline-a")["signals"]
        assert all(x["accepted"] == x["duplicate"] == x["rejected"] == 1 for x in restored.values()), restored
        assert all(
            x["accepted"] == 1 and x["lag_seconds"] is not None and x["queue_requests"] == 0 for x in restored.values()
        )
        assert store.logs("pipeline-a", {"attrs": '{"error.type":"custom-a"}'})["total"] == 1
        assert store.logs("pipeline-b", {"attrs": '{"error.type":"from-json"}'})["total"] == 1
        assert store.logs("pipeline-b", {"attrs": '{"error.type":"custom-a"}'})["total"] == 0
        return {"offline": offline, "recovered": restored, "separate_project_policy": True}
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            process.wait(timeout=10)
        if server:
            server.shutdown()
            server.server_close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--collector", required=True)
    args = parser.parse_args()
    base = os.environ["TEST_DATABASE_URL"]
    schema = "datalog_pipelines_" + uuid.uuid4().hex
    with psycopg.connect(base) as db:
        db.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    store = Store(make_conninfo(base, options=f"-c search_path={schema}"))
    try:
        with tempfile.TemporaryDirectory(prefix="datalog-shared-collector-") as work:
            print(json.dumps(verify(store, Path(work), args.collector), indent=2))
    finally:
        store.close()
        with psycopg.connect(base) as db:
            db.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


if __name__ == "__main__":
    main()
