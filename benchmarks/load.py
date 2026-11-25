"""Sustained load test against a running Datalog server.

Sends log batches and OTLP/JSON traces from concurrent workers for a fixed
duration and reports throughput, latency percentiles and status codes, so
operators can size their own deployment before rollout.

    python -m benchmarks.load --url http://127.0.0.1:4310 --project local \
        --key "$DATALOG_INGESTION_KEY" --workers 32 --duration 60

Optional read load: pass a browser session cookie value with --session to also
query /api/logs and /api/traces while ingestion runs.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
import random
import secrets
import statistics
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def log_batch(size):
    now = time.time()
    return [
        {
            "timestamp": now,
            "service": random.choice(("api", "worker", "checkout")),
            "env": "load",
            "level": random.choice(("info", "info", "info", "warn", "error")),
            "message": f"load event {secrets.token_hex(4)}",
            "tags": {"region": "load"},
        }
        for _ in range(size)
    ]


def otlp_trace(spans):
    now = time.time_ns()
    trace_id = secrets.token_hex(16)
    items = [
        {
            "traceId": trace_id,
            "spanId": secrets.token_hex(8),
            "name": f"GET /load/{i}",
            "kind": 2,
            "startTimeUnixNano": str(now - 5_000_000),
            "endTimeUnixNano": str(now),
            "status": {"code": 2 if random.random() < 0.02 else 1},
        }
        for i in range(spans)
    ]
    return {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": "load-generator"}},
                        {"key": "deployment.environment.name", "value": {"stringValue": "load"}},
                    ]
                },
                "scopeSpans": [{"spans": items}],
            }
        ]
    }


def percentile(ordered, q):
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))]


class Recorder:
    def __init__(self):
        self.lock = threading.Lock()
        self.samples = {}

    def add(self, kind, status, seconds):
        with self.lock:
            self.samples.setdefault(kind, []).append((status, seconds))

    def report(self, elapsed):
        result = {}
        for kind, rows in sorted(self.samples.items()):
            latencies = sorted(seconds * 1000 for _, seconds in rows)
            statuses = {}
            for status, _ in rows:
                statuses[str(status)] = statuses.get(str(status), 0) + 1
            result[kind] = {
                "requests": len(rows),
                "per_second": round(len(rows) / elapsed, 1),
                "p50_ms": round(statistics.median(latencies), 1),
                "p95_ms": round(percentile(latencies, 0.95), 1),
                "p99_ms": round(percentile(latencies, 0.99), 1),
                "statuses": statuses,
            }
        return result


def send(url, body, headers, timeout):
    request = Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json", **headers})
    started = time.perf_counter()
    try:
        with urlopen(request, timeout=timeout) as response:
            response.read()
            status = response.status
    except HTTPError as exc:
        status = exc.code
    except (URLError, TimeoutError, ConnectionError):
        status = "network"
    return status, time.perf_counter() - started


def get(url, headers, timeout):
    started = time.perf_counter()
    try:
        with urlopen(Request(url, headers=headers), timeout=timeout) as response:
            response.read()
            status = response.status
    except HTTPError as exc:
        status = exc.code
    except (URLError, TimeoutError, ConnectionError):
        status = "network"
    return status, time.perf_counter() - started


def main():
    parser = argparse.ArgumentParser(description="Sustained Datalog load test")
    parser.add_argument("--url", default="http://127.0.0.1:4310")
    parser.add_argument("--project", default="local")
    parser.add_argument(
        "--key", default=os.environ.get("DATALOG_INGESTION_KEY"), help="Ingestion key (or DATALOG_INGESTION_KEY)"
    )
    parser.add_argument(
        "--session", default=os.environ.get("DATALOG_SESSION"), help="Optional session cookie for read load"
    )
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--duration", type=float, default=30)
    parser.add_argument("--batch", type=int, default=50, help="Logs per request")
    parser.add_argument("--spans", type=int, default=20, help="Spans per OTLP request")
    parser.add_argument("--timeout", type=float, default=10)
    args = parser.parse_args()
    if not args.key:
        parser.error("--key or DATALOG_INGESTION_KEY is required")
    base = args.url.rstrip("/")
    query = urlencode({"project": args.project})
    auth = {"Authorization": "Bearer " + args.key}
    reads = {"Cookie": "datalog_session=" + args.session} if args.session else None
    recorder = Recorder()
    stop = time.monotonic() + args.duration

    def worker(index):
        while time.monotonic() < stop:
            choice = index % 4 if reads else index % 2
            if choice == 0:
                status, seconds = send(f"{base}/api/logs?{query}", log_batch(args.batch), auth, args.timeout)
                recorder.add("logs", status, seconds)
            elif choice == 1:
                status, seconds = send(f"{base}/v1/traces?{query}", otlp_trace(args.spans), auth, args.timeout)
                recorder.add("otlp_traces", status, seconds)
            else:
                path = "logs" if choice == 2 else "traces"
                status, seconds = get(f"{base}/api/{path}?{query}", reads, args.timeout)
                recorder.add("read_" + path, status, seconds)
            if status in (429, 503):
                time.sleep(1)

    started = time.monotonic()
    with ThreadPoolExecutor(args.workers) as pool:
        list(pool.map(worker, range(args.workers)))
    elapsed = time.monotonic() - started
    print(
        json.dumps(
            {"workers": args.workers, "seconds": round(elapsed, 1), "results": recorder.report(elapsed)}, indent=2
        )
    )


if __name__ == "__main__":
    main()
