"""Small Prometheus endpoint for the Datalog process itself."""

import threading
import time
from collections import Counter


class OperationalMetrics:
    def __init__(self):
        self.started = time.monotonic()
        self.lock = threading.Lock()
        self.requests = Counter()
        self.deleted = Counter()
        self.sweeps = Counter()
        self.records = Counter()
        self.wire_records = Counter()
        self.wire_lag = {}
        self.lag = {}

    def observe(self, path, status):
        signal = {
            "/v1/traces": "traces",
            "/v1/logs": "logs",
            "/v1/metrics": "metrics",
            "/api/logs": "json_logs",
            "/api/metrics": "json_metrics",
            "/api/ai/traces": "json_ai",
            "/api/auth/login": "login",
        }.get(path, "other")
        with self.lock:
            self.requests[signal, status] += 1

    def sweep(self, removed=None):
        with self.lock:
            self.sweeps["error" if removed is None else "ok"] += 1
            self.deleted.update(removed or {})

    def ingest(self, signal, valid, accepted, rejected, latest=None, *, wire_project=None):
        with self.lock:
            for result, count in (
                ("received", valid + rejected),
                ("accepted", accepted),
                ("rejected", rejected),
                ("duplicate", valid - accepted),
            ):
                self.records[signal, result] += count
                if wire_project:
                    self.wire_records[wire_project, signal, result] += count
            if latest is not None and accepted:
                lag = max(0, time.time() - latest)
                self.lag[signal] = lag
                if wire_project:
                    self.wire_lag[wire_project, signal] = lag

    def render(self, pool):
        rows = ["# TYPE datalog_uptime_seconds gauge", f"datalog_uptime_seconds {time.monotonic() - self.started:.3f}"]
        with self.lock:
            rows.append("# TYPE datalog_http_requests_total counter")
            rows.extend(
                f'datalog_http_requests_total{{route="{route}",status="{status}"}} {count}'
                for (route, status), count in sorted(self.requests.items())
            )
            rows.append("# TYPE datalog_retention_deleted_total counter")
            rows.extend(
                f'datalog_retention_deleted_total{{table="{table}"}} {count}'
                for table, count in sorted(self.deleted.items())
            )
            rows.append("# TYPE datalog_retention_sweeps_total counter")
            rows.extend(
                f'datalog_retention_sweeps_total{{result="{result}"}} {count}'
                for result, count in sorted(self.sweeps.items())
            )
            rows.append("# TYPE datalog_otlp_records_total counter")
            rows.extend(
                f'datalog_otlp_records_total{{signal="{signal}",result="{result}"}} {count}'
                for (signal, result), count in sorted(self.records.items())
            )
            rows.append("# TYPE datalog_otlp_ingestion_lag_seconds gauge")
            rows.extend(
                f'datalog_otlp_ingestion_lag_seconds{{signal="{signal}"}} {lag:.6f}'
                for signal, lag in sorted(self.lag.items())
            )
            rows.append("# TYPE datalog_otlp_wire_records_total counter")
            rows.extend(
                f'datalog_otlp_wire_records_total{{project="{p}",signal="{signal}",result="{result}"}} {count}'
                for (p, signal, result), count in sorted(self.wire_records.items())
            )
            rows.append("# TYPE datalog_otlp_wire_lag_seconds gauge")
            rows.extend(
                f'datalog_otlp_wire_lag_seconds{{project="{p}",signal="{signal}"}} {lag:.6f}'
                for (p, signal), lag in sorted(self.wire_lag.items())
            )
        for key, value in pool.get_stats().items():
            if key in {"pool_size", "pool_available", "requests_waiting"}:
                rows.extend([f"# TYPE datalog_db_{key} gauge", f"datalog_db_{key} {value}"])
        return ("\n".join(rows) + "\n").encode()
