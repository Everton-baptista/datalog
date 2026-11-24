"""Measure restore and primary promotion on PostgreSQL clusters created in a temporary directory."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor

import psycopg
from psycopg.conninfo import make_conninfo
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

from datalog.app import Store
from datalog.backup import backup, command, restore
from datalog.privacy import Privacy


def execute(tool, *args, **kwargs):
    return subprocess.run([command(tool), *map(str, args)], check=True, capture_output=True, text=True, **kwargs)


def fixture():
    payload = ExportTraceServiceRequest()
    start = time.time_ns()
    trace = bytes.fromhex("afbf1f3577b34da6a3ce929d0e0e4736")
    span = bytes.fromhex("10f067aa0ba902b7")
    payload.resource_spans.add().scope_spans.add().spans.add(
        trace_id=trace,
        span_id=span,
        name="recovery-fixture",
        start_time_unix_nano=start,
        end_time_unix_nano=start + 1000,
        kind=2,
    )
    return payload, trace.hex()


def verify(work):
    primary, standby, restored = (work / name for name in ("primary", "standby", "restored"))
    stores, started = [], []

    def start(folder):
        socket = folder / "socket"
        socket.mkdir()
        execute(
            "pg_ctl",
            "-D",
            folder / "data",
            "-l",
            folder / "postgres.log",
            "-o",
            f"-c listen_addresses='' -k {socket}",
            "-w",
            "start",
        )
        started.append(folder)
        return make_conninfo(dbname="postgres", host=str(socket), connect_timeout=2)

    def fresh(folder):
        folder.mkdir()
        execute("initdb", "-D", folder / "data", "-A", "trust", "--no-locale", "--encoding=UTF8")
        return start(folder)

    def open_store(dsn):
        item = Store(dsn)
        stores.append(item)
        return item

    def privacy(item):
        return Privacy(item.access.authorize("recovery", "operator", "write"))

    def verify_access(item, trace):
        assert item.tracing.detail("recovery", trace)["span_count"] == 1
        assert item.tracing.detail("second", trace) is None
        assert item.access.ingestion_policy("recovery", key)["signal_retention"] == {"logs": 7}

    try:
        source_dsn = fresh(primary)
        source = open_store(source_dsn)
        source.access.create_user("operator", "synthetic example credential")
        source.access.create_project("operator", "recovery")
        source.access.create_project("operator", "second")
        key = source.access.create_key("recovery", "operator", "Fixture")["token"]
        source.access.update_settings(
            "recovery",
            "operator",
            {
                "retention_days": 30,
                "request_quota": 600,
                "byte_quota": 1048576,
                "capture_content": False,
                "signal_retention": {"logs": 7},
            },
        )
        payload, trace = fixture()
        source.tracing.ingest("recovery", payload, privacy(source))

        archive = work / "snapshot.dump"
        backup(source_dsn, archive)
        target_dsn = fresh(restored)
        before = time.monotonic()
        restore(target_dsn, archive)
        restored_store = open_store(target_dsn)
        verify_access(restored_store, trace)
        restored_store.tracing.ingest("recovery", payload, privacy(restored_store))
        verify_access(restored_store, trace)
        restore_seconds = time.monotonic() - before

        standby.mkdir()
        execute(
            "pg_basebackup",
            "-D",
            standby / "data",
            "-R",
            "-X",
            "stream",
            env={**os.environ, "PGHOST": str(primary / "socket")},
        )
        standby_dsn = start(standby)
        with psycopg.connect(standby_dsn) as db:
            assert db.execute("SELECT pg_is_in_recovery()").fetchone()[0]
        before = time.monotonic()
        execute("pg_ctl", "-D", primary / "data", "-w", "-m", "fast", "stop")
        started.remove(primary)
        execute("pg_ctl", "-D", standby / "data", "-w", "promote")
        failover_dsn = make_conninfo(
            dbname="postgres",
            host=f"{primary / 'socket'},{standby / 'socket'}",
            target_session_attrs="read-write",
            connect_timeout=2,
        )
        a, b = open_store(failover_dsn), open_store(failover_dsn)
        with ThreadPoolExecutor(max_workers=2) as threads:
            list(threads.map(lambda app: app.tracing.ingest("recovery", payload, privacy(app)), (a, b)))
        verify_access(a, trace)
        verify_access(b, trace)
        return {
            "restore_seconds": round(restore_seconds, 3),
            "failover_seconds": round(time.monotonic() - before, 3),
            "fixture_spans_lost": 0,
            "fixture_spans_after_retries": 1,
            "project_isolation": True,
        }
    finally:
        for store in stores:
            store.close()
        for folder in reversed(started):
            execute("pg_ctl", "-D", folder / "data", "-w", "-m", "immediate", "stop")


def main():
    argparse.ArgumentParser(description=__doc__).parse_args()
    with tempfile.TemporaryDirectory(prefix="datalog-recovery-") as work:
        print(json.dumps(verify(Path(work)), indent=2))


if __name__ == "__main__":
    main()
