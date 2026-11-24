#!/usr/bin/env python3
"""Populate a project with one coherent, explicitly synthetic story across every page.

The story: a fictional shop ("Loja Demo") with eight services. About 35 minutes ago the
payments provider started degrading: payments p95 latency triples, errors rise, a payments
pod restarts, the checkout synthetic test fails, checkout Web Vitals degrade and the
payments data mart goes stale. Everything is tagged with a fresh ``seed_id`` so re-running
adds a new set without touching existing data.

Usage: python -m datalog.seed_full [--project demo] [--hours 24]
"""

import argparse
import bisect
import hashlib
import json
import math
import os
import random
import time
import uuid

from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceRequest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from psycopg.types.json import Jsonb

from datalog.agent_workflows import project_privacy
from datalog.alerts import options as alert_options
from datalog.browser_journeys import steps as journey_steps
from datalog.data_quality import DataQuality
from datalog.data_lineage import digest

DATASET = "synthetic-full"
SERVER, CLIENT, INTERNAL, PRODUCER, CONSUMER = 2, 3, 1, 4, 5
ERROR = 2
NS = 10**9
BOUNDS = [5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000]
SERVICES = ["gateway", "checkout", "payments", "inventory", "auth", "search", "notifications", "worker"]
STAGING = ["gateway", "checkout", "payments", "search"]
PODS = {
    "gateway": 3,
    "checkout": 2,
    "payments": 2,
    "inventory": 2,
    "auth": 2,
    "search": 2,
    "notifications": 1,
    "worker": 2,
}
VERSIONS = {
    "gateway": "3.8.1-demo",
    "checkout": "2.14.3-demo",
    "payments": "5.2.0-demo",
    "inventory": "1.9.7-demo",
    "auth": "4.0.2-demo",
    "search": "2.3.5-demo",
    "notifications": "1.4.0-demo",
    "worker": "2.14.3-demo",
}
RATE = {
    "gateway": 900,
    "checkout": 120,
    "payments": 110,
    "inventory": 300,
    "auth": 600,
    "search": 350,
    "notifications": 90,
    "worker": 100,
}  # requests per minute at peak
RESTARTING_POD = "payments-6d8f9b7c4-x7k2p"
WAREHOUSE = "postgres://demo-warehouse"


class Ids:
    """Unique per run (mixed with seed_id) while the rest of the data stays RNG-deterministic."""

    def __init__(self, seed_id):
        self.seed, self.counter = seed_id, 0

    def make(self, size):
        self.counter += 1
        return hashlib.sha256(f"{self.seed}:{self.counter}".encode()).digest()[:size]


def set_attr(attrs, key, value):
    item = attrs.add(key=key)
    if isinstance(value, bool):
        item.value.bool_value = value
    elif isinstance(value, int):
        item.value.int_value = value
    elif isinstance(value, float):
        item.value.double_value = value
    else:
        item.value.string_value = str(value)


def diurnal(ts):
    # Peak in the Brazilian afternoon (UTC-3), quiet before dawn.
    hour = ((ts / 3600) - 3) % 24
    return 0.25 + 0.75 * max(0.0, math.sin(math.pi * (hour - 5) / 17)) if 5 <= hour <= 22 else 0.25


class Story:
    def __init__(self, store, project, hours, seed_id):
        self.store, self.p, self.hours, self.seed_id = store, project, hours, seed_id
        self.rng = random.Random(20261118)
        self.ids = Ids(seed_id)
        self.now = time.time() - 2
        self.begin = self.now - hours * 3600
        self.incident = self.now - 35 * 60
        self.counts = {}
        self.notes = []
        self.pods = {}
        nodes = ["demo-node-a", "demo-node-b", "demo-node-c"]
        index = 0
        for env, services in (("production", SERVICES), ("staging", STAGING)):
            for service in services:
                total = PODS[service] if env == "production" else 1
                items = []
                for n in range(total):
                    name = (
                        RESTARTING_POD
                        if (service, env, n) == ("payments", "production", 0)
                        else f"{service}{'-stg' if env == 'staging' else ''}-{hashlib.md5(f'{service}{env}{n}'.encode()).hexdigest()[:9]}-"
                        f"{hashlib.md5(f'{n}{service}'.encode()).hexdigest()[:5]}"
                    )
                    items.append(
                        {
                            "service": service,
                            "env": env,
                            "pod": name,
                            "node": nodes[index % 3] if env == "production" else "demo-node-stg",
                            "container": hashlib.sha256((seed_id + name).encode()).hexdigest()[:24],
                            "uid": str(uuid.UUID(hashlib.md5((seed_id + name).encode()).hexdigest())),
                        }
                    )
                    index += 1
                self.pods[(service, env)] = items
        self.server_spans = {}  # service -> sorted [(start_ns, trace_hex, span_hex, ms)]
        self.checkout_traces = []  # (start_s, trace_hex, error)
        self.slow_payments = []  # (start_s, trace_hex, span_hex, ms)
        self.error_spans = []  # (start_s, service, trace_hex, span_hex)
        self.logs = []

    # ---------------------------------------------------------------- helpers
    def tags(self, pod=None, **extra):
        tags = {"dataset": DATASET, "seed_id": self.seed_id, "region": "sa-east-1-demo"}
        if pod:
            tags.update(host=pod["node"], pod=pod["pod"], version=VERSIONS.get(pod["service"], "1.0.0-demo"))
        tags.update({k: str(v) for k, v in extra.items()})
        return tags

    def degraded(self, ts):
        return ts >= self.incident

    def latency(self, op, ts, service=None):
        """Milliseconds for one operation, with a long tail and the payments incident."""
        base = {
            "gw": 3,
            "auth": 8,
            "auth.db": 2,
            "login": 38,
            "checkout": 12,
            "inv": 14,
            "inv.db": 4,
            "pay": 18,
            "pay.db": 5,
            "provider": 170,
            "search": 42,
            "es": 24,
            "stock": 9,
            "orders": 16,
            "orders.db": 5,
            "notify": 22,
            "worker": 15,
            "worker.db": 4,
        }[op]
        value = base * math.exp(self.rng.gauss(0, 0.38))
        if self.rng.random() < 0.035:
            value *= self.rng.uniform(3, 9)
        if self.degraded(ts):
            if op == "provider":
                value *= self.rng.uniform(2.2, 3.2)
            elif op == "pay.db":
                value *= self.rng.uniform(2, 5)
        return value

    def payments_server_ms(self, ts):
        return self.latency("pay", ts) + self.latency("pay.db", ts) + self.latency("provider", ts)

    # ----------------------------------------------------------------- traces
    def build_traces(self):
        pending = []  # list of (pod, span proto kwargs)
        times = []
        target = max(40, int(16 * self.hours))
        while len(times) < target:
            ts = self.rng.uniform(self.begin, self.now - 3600)
            if self.rng.random() < diurnal(ts):
                times.append(ts)
        times += [self.rng.uniform(self.now - 3600, self.now - 4) for _ in range(420)]
        # The incident window gets extra checkout traffic so p95/error rates are visible.
        times.sort()
        kinds = ["checkout", "product", "search", "login", "orders"]
        for ts in times:
            env = "staging" if self.rng.random() < 0.09 else "production"
            kind = self.rng.choices(kinds, [30, 25, 25, 10, 10])[0]
            if self.degraded(ts) and self.rng.random() < 0.25:
                kind = "checkout"
            if env == "staging" and kind not in ("checkout", "search"):
                kind = "search"
            getattr(self, "trace_" + kind)(pending, ts, env)
        for job in range(max(6, self.hours * 2)):
            ts = self.begin + (job + 0.5) * (self.now - self.begin) / max(6, self.hours * 2)
            self.trace_job(pending, ts)
        self.export_spans(pending)
        for values in self.server_spans.values():
            values.sort()
        self.counts["traces"] = len(times) + max(6, self.hours * 2)

    def pod(self, service, env):
        return self.rng.choice(self.pods[(service, env)])

    def ctx_pod(self, ctx, service):
        if service not in ctx["pods"]:
            ctx["pods"][service] = self.pod(service, ctx["env"])
        return ctx["pods"][service]

    def span(self, pending, ctx, service, name, kind, start, end, parent, attrs=None, error=None, events=()):
        pod = self.ctx_pod(ctx, service)
        span_id = self.ids.make(8)
        record = {
            "trace_id": ctx["trace"],
            "span_id": span_id,
            "parent_span_id": parent or b"",
            "name": name,
            "kind": kind,
            "start_time_unix_nano": int(start * NS),
            "end_time_unix_nano": int(max(end, start + 1e-6) * NS),
            "attrs": attrs or {},
            "error": error,
            "events": events,
        }
        pending.append((pod, record))
        if kind == SERVER:
            self.server_spans.setdefault(service, []).append(
                (int(start * NS), ctx["trace"].hex(), span_id.hex(), (end - start) * 1000)
            )
        if error:
            self.error_spans.append((start, service, ctx["trace"].hex(), span_id.hex()))
        return span_id

    def rpc(self, pending, ctx, caller, callee, method, route, start, parent, inner=None, own_ms=5, status=200):
        """Client span in caller + server span in callee; ``inner`` builds the callee's children."""
        client_id, server_id = self.ids.make(8), self.ids.make(8)
        net = self.rng.uniform(0.0004, 0.0018)
        server_start = start + net
        cursor, error = server_start + 0.0003, None
        if inner:
            cursor, error = inner(server_id, cursor)
        server_end = cursor + own_ms / 1000
        code = status if not error else (504 if "Timeout" in error else 502 if callee != "payments" else 503)
        attrs = {"http.request.method": method, "http.route": route, "http.response.status_code": code}
        events = [("exception", server_end - 0.0005)] if error else ()
        self.record(
            pending,
            ctx,
            callee,
            f"{method} {route}",
            SERVER,
            server_start,
            server_end,
            client_id,
            server_id,
            attrs,
            error,
            events,
        )
        self.record(
            pending,
            ctx,
            caller,
            f"{method} {callee}{route}",
            CLIENT,
            start,
            server_end + net,
            parent,
            client_id,
            {"http.request.method": method, "http.response.status_code": code},
            ("Upstream" + error) if error and not error.startswith("Upstream") else error,
        )
        return server_end + net, error, server_id

    def record(self, pending, ctx, service, name, kind, start, end, parent, span_id, attrs, error, events=()):
        pod = self.ctx_pod(ctx, service)
        pending.append(
            (
                pod,
                {
                    "trace_id": ctx["trace"],
                    "span_id": span_id,
                    "parent_span_id": parent or b"",
                    "name": name,
                    "kind": kind,
                    "start_time_unix_nano": int(start * NS),
                    "end_time_unix_nano": int(max(end, start + 1e-6) * NS),
                    "attrs": attrs,
                    "error": error,
                    "events": events,
                },
            )
        )
        if kind == SERVER:
            self.server_spans.setdefault(service, []).append(
                (int(start * NS), ctx["trace"].hex(), span_id.hex(), (end - start) * 1000)
            )
        if error:
            self.error_spans.append((start, service, ctx["trace"].hex(), span_id.hex()))

    def db(self, pending, ctx, service, operation, table, start, parent, op, error=None):
        end = start + self.latency(op, start) / 1000
        self.span(
            pending,
            ctx,
            service,
            f"{operation} {table}",
            CLIENT,
            start,
            end,
            parent,
            {"db.system.name": "postgresql", "db.operation.name": operation},
            error,
            [("exception", end)] if error else (),
        )
        return end

    def context(self, ts, env):
        return {"trace": self.ids.make(16), "env": env, "pods": {}, "start": ts}

    def finish_root(self, pending, ctx, name, route, method, start, end, root_id, error, logs=True):
        code = 502 if error else 200
        self.record(
            pending,
            ctx,
            "gateway",
            name,
            SERVER,
            start,
            end,
            None,
            root_id,
            {"http.request.method": method, "http.route": route, "http.response.status_code": code},
            "UpstreamBadGateway" if error else None,
            [("exception", end)] if error else (),
        )
        if logs and (error or self.rng.random() < 0.55):
            pod = ctx["pods"]["gateway"]
            self.logs.append(
                {
                    "timestamp": end,
                    "service": "gateway",
                    "env": ctx["env"],
                    "level": "error" if error else "info",
                    "message": f"{method} {route} {code} em {round((end - start) * 1000)} ms",
                    "trace_id": ctx["trace"].hex(),
                    "span_id": root_id.hex(),
                    "tags": self.tags(pod, http_status=code),
                }
            )

    def auth_call(self, pending, ctx, start, parent):
        def inner(server_id, cursor):
            return self.db(pending, ctx, "auth", "SELECT", "sessions", cursor, server_id, "auth.db"), None

        end, _, _ = self.rpc(
            pending,
            ctx,
            "gateway",
            "auth",
            "POST",
            "/v1/sessions/verify",
            start,
            parent,
            inner,
            self.latency("auth", start),
        )
        return end

    def trace_checkout(self, pending, ts, env):
        ctx = self.context(ts, env)
        root = self.ids.make(8)
        degraded = self.degraded(ts) and env == "production"
        cursor = ts + 0.0005
        if env == "production":
            cursor = self.auth_call(pending, ctx, cursor, root)
        payment_error = None

        def payments_inner(server_id, start):
            nonlocal payment_error
            pool_error = degraded and self.rng.random() < 0.03
            cursor = self.db(
                pending,
                ctx,
                "payments",
                "INSERT",
                "payments",
                start,
                server_id,
                "pay.db",
                "ConnectionPoolExhausted" if pool_error else None,
            )
            if pool_error:
                payment_error = "ConnectionPoolExhausted"
                return cursor, payment_error
            provider_ms = self.latency("provider", start)
            timeout = (degraded and self.rng.random() < 0.045) or self.rng.random() < 0.003
            if timeout:
                provider_ms = 3000 + self.rng.uniform(0, 40)
            end = cursor + provider_ms / 1000
            self.span(
                pending,
                ctx,
                "payments",
                "POST provider-demo/authorize",
                CLIENT,
                cursor,
                end,
                server_id,
                {"http.request.method": "POST", "http.response.status_code": 504 if timeout else 200},
                "PaymentProviderTimeout" if timeout else None,
                [("exception", end)] if timeout else (),
            )
            if timeout:
                payment_error = "PaymentProviderTimeout"
            else:
                end = self.db(pending, ctx, "payments", "UPDATE", "payments", end, server_id, "pay.db")
            return end, payment_error

        def checkout_inner(server_id, start):
            cursor = start
            if env == "production":

                def inventory_inner(inv_id, begin):
                    return self.db(pending, ctx, "inventory", "UPDATE", "stock_items", begin, inv_id, "inv.db"), None

                cursor, _, _ = self.rpc(
                    pending,
                    ctx,
                    "checkout",
                    "inventory",
                    "POST",
                    "/v1/reservations",
                    cursor,
                    server_id,
                    inventory_inner,
                    self.latency("inv", cursor),
                )
            cursor, error, pay_id = self.rpc(
                pending,
                ctx,
                "checkout",
                "payments",
                "POST",
                "/v1/charges",
                cursor,
                server_id,
                payments_inner,
                self.latency("pay", cursor),
            )
            pay_ms = (cursor - start) * 1000
            if degraded and pay_ms > 900:
                self.slow_payments.append((start, ctx["trace"].hex(), pay_id.hex(), pay_ms))
            if not error and env == "production":
                publish = cursor + 0.002
                producer = self.span(
                    pending,
                    ctx,
                    "checkout",
                    "orders.created publish",
                    PRODUCER,
                    cursor,
                    publish,
                    server_id,
                    {"rpc.system": "kafka-demo"},
                )
                consumer_start = publish + self.rng.uniform(0.05, 0.6) * (4 if degraded else 1)
                consumer_id = self.ids.make(8)
                notify_end, _, _ = self.rpc(
                    pending,
                    ctx,
                    "worker",
                    "notifications",
                    "POST",
                    "/v1/notifications/email",
                    consumer_start + 0.004,
                    consumer_id,
                    None,
                    self.latency("notify", consumer_start),
                )
                self.record(
                    pending,
                    ctx,
                    "worker",
                    "orders.created process",
                    CONSUMER,
                    consumer_start,
                    notify_end + 0.001,
                    producer,
                    consumer_id,
                    {"rpc.system": "kafka-demo"},
                    None,
                )
                cursor = publish
            if error:
                pod = ctx["pods"]["payments"]
                messages = {
                    "PaymentProviderTimeout": "Timeout ao autorizar pagamento no provider-demo após 3000 ms",
                    "ConnectionPoolExhausted": "Pool de conexões esgotado em payments-db (50/50 em uso)",
                }
                self.logs.append(
                    {
                        "timestamp": cursor,
                        "service": "payments",
                        "env": env,
                        "level": "error",
                        "message": messages[payment_error],
                        "trace_id": ctx["trace"].hex(),
                        "span_id": pay_id.hex(),
                        "tags": self.tags(pod, error_type=payment_error),
                    }
                )
                self.logs.append(
                    {
                        "timestamp": cursor + 0.001,
                        "service": "checkout",
                        "env": env,
                        "level": "error",
                        "message": "Falha ao confirmar pedido: payments respondeu 503",
                        "trace_id": ctx["trace"].hex(),
                        "span_id": server_id.hex(),
                        "tags": self.tags(ctx["pods"]["checkout"], error_type="UpstreamPaymentError"),
                    }
                )
            elif degraded and pay_ms > 700:
                self.logs.append(
                    {
                        "timestamp": cursor,
                        "service": "payments",
                        "env": env,
                        "level": "warn",
                        "message": f"Autorização lenta no provider-demo: {round(pay_ms)} ms (limite 600 ms)",
                        "trace_id": ctx["trace"].hex(),
                        "span_id": pay_id.hex(),
                        "tags": self.tags(ctx["pods"]["payments"]),
                    }
                )
            return cursor, ("UpstreamPaymentError" if error else None)

        end, error, _ = self.rpc(
            pending,
            ctx,
            "gateway",
            "checkout",
            "POST",
            "/v1/checkout",
            cursor,
            root,
            checkout_inner,
            self.latency("checkout", cursor),
        )
        self.finish_root(pending, ctx, "POST /api/checkout", "/api/checkout", "POST", ts, end + 0.0008, root, error)
        self.checkout_traces.append((ts, ctx["trace"].hex(), bool(error)))

    def trace_product(self, pending, ts, env):
        ctx = self.context(ts, env)
        root = self.ids.make(8)

        def inner(server_id, start):
            return self.db(pending, ctx, "inventory", "SELECT", "stock_items", start, server_id, "inv.db"), None

        end, error, _ = self.rpc(
            pending,
            ctx,
            "gateway",
            "inventory",
            "GET",
            "/v1/stock/{sku}",
            ts + 0.0005,
            root,
            inner,
            self.latency("stock", ts),
        )
        self.finish_root(
            pending, ctx, "GET /api/products/{id}", "/api/products/{id}", "GET", ts, end + 0.0006, root, error
        )

    def trace_search(self, pending, ts, env):
        ctx = self.context(ts, env)
        root = self.ids.make(8)

        def inner(server_id, start):
            timeout = self.rng.random() < 0.006
            ms = 2000 if timeout else self.latency("es", start)
            self.span(
                pending,
                ctx,
                "search",
                "POST catalog-index/_search",
                CLIENT,
                start,
                start + ms / 1000,
                server_id,
                {"http.request.method": "POST", "http.response.status_code": 504 if timeout else 200},
                "SearchBackendTimeout" if timeout else None,
            )
            return start + ms / 1000, "SearchBackendTimeout" if timeout else None

        end, error, _ = self.rpc(
            pending, ctx, "gateway", "search", "GET", "/v1/search", ts + 0.0005, root, inner, self.latency("search", ts)
        )
        self.finish_root(pending, ctx, "GET /api/search", "/api/search", "GET", ts, end + 0.0006, root, error)

    def trace_login(self, pending, ts, env):
        ctx = self.context(ts, env)
        root = self.ids.make(8)

        def inner(server_id, start):
            return self.db(pending, ctx, "auth", "SELECT", "users", start, server_id, "auth.db"), None

        end, error, _ = self.rpc(
            pending, ctx, "gateway", "auth", "POST", "/v1/login", ts + 0.0005, root, inner, self.latency("login", ts)
        )
        self.finish_root(pending, ctx, "POST /api/login", "/api/login", "POST", ts, end + 0.0006, root, error)

    def trace_orders(self, pending, ts, env):
        ctx = self.context(ts, env)
        root = self.ids.make(8)
        cursor = self.auth_call(pending, ctx, ts + 0.0005, root)

        def inner(server_id, start):
            return self.db(pending, ctx, "checkout", "SELECT", "orders", start, server_id, "orders.db"), None

        end, error, _ = self.rpc(
            pending,
            ctx,
            "gateway",
            "checkout",
            "GET",
            "/v1/orders/{id}",
            cursor,
            root,
            inner,
            self.latency("orders", ts),
        )
        self.finish_root(pending, ctx, "GET /api/orders/{id}", "/api/orders/{id}", "GET", ts, end + 0.0006, root, error)

    def trace_job(self, pending, ts):
        ctx = self.context(ts, "production")
        root = self.ids.make(8)
        cursor = ts + 0.001
        for _ in range(self.rng.randint(3, 6)):
            cursor = self.db(pending, ctx, "worker", "SELECT", "payments_pending", cursor, root, "worker.db") + 0.002
        failed = self.degraded(ts)
        self.record(
            pending,
            ctx,
            "worker",
            "job reconcile-payments",
            INTERNAL,
            ts,
            cursor + 0.03,
            None,
            root,
            {},
            "ReconciliationLagExceeded" if failed else None,
        )
        self.logs.append(
            {
                "timestamp": cursor,
                "service": "worker",
                "env": "production",
                "level": "error" if failed else "info",
                "message": "Reconciliação de pagamentos atrasada: 412 transações pendentes"
                if failed
                else f"Reconciliação de pagamentos concluída: {self.rng.randint(800, 4000)} transações",
                "trace_id": ctx["trace"].hex(),
                "span_id": root.hex(),
                "tags": self.tags(ctx["pods"]["worker"]),
            }
        )

    def resource_attrs(self, attrs, pod):
        for key, value in (
            ("service.name", pod["service"]),
            ("service.namespace", "loja-demo"),
            ("service.version", VERSIONS.get(pod["service"], "1.0.0-demo")),
            ("deployment.environment.name", pod["env"]),
            ("host.name", pod["node"]),
            ("host.id", "host-" + pod["node"]),
            ("k8s.namespace.name", "loja-demo" if pod["env"] == "production" else "loja-demo-stg"),
            ("k8s.pod.name", pod["pod"]),
            ("k8s.pod.uid", pod["uid"]),
            ("k8s.container.name", pod["service"]),
            ("container.id", pod["container"]),
            ("telemetry.sdk.name", "opentelemetry"),
            ("telemetry.sdk.language", "python"),
        ):
            set_attr(attrs, key, value)

    def export_spans(self, pending, service_override=None):
        """Group spans by resource (pod) and send them through TraceStore.ingest in ≤2000-span requests."""
        with self.store.connect() as db:
            privacy = project_privacy(db, self.p)
        total = 0
        for offset in range(0, len(pending), 1800):
            chunk = pending[offset : offset + 1800]
            request = ExportTraceServiceRequest()
            groups = {}
            for pod, record in chunk:
                key = pod["pod"]
                if key not in groups:
                    group = request.resource_spans.add(schema_url="https://opentelemetry.io/schemas/1.38.0")
                    self.resource_attrs(group.resource.attributes, pod)
                    scope = group.scope_spans.add()
                    scope.scope.name = "loja-demo.instrumentation"
                    scope.scope.version = "1.0.0-demo"
                    groups[key] = scope
                span = groups[key].spans.add(
                    trace_id=record["trace_id"],
                    span_id=record["span_id"],
                    parent_span_id=record["parent_span_id"],
                    name=record["name"],
                    kind=record["kind"],
                    start_time_unix_nano=record["start_time_unix_nano"],
                    end_time_unix_nano=record["end_time_unix_nano"],
                )
                for key_, value in record["attrs"].items():
                    set_attr(span.attributes, key_, value)
                if record["error"]:
                    span.status.code = ERROR
                    span.status.message = record["error"]
                    set_attr(span.attributes, "error.type", record["error"])
                for name, at in record["events"]:
                    event = span.events.add(name=name, time_unix_nano=int(at * NS))
                    set_attr(event.attributes, "exception.type", record["error"] or "Error")
                    set_attr(event.attributes, "exception.message", f"{record['error']} (cenário sintético)")
            self.store.tracing.ingest(self.p, request, privacy)
            total += len(chunk)
        self.counts["spans"] = self.counts.get("spans", 0) + total

    # ------------------------------------------------------------------- logs
    def build_logs(self):
        templates = {
            "gateway": [
                ("info", "GET /api/products/{} 200 em {} ms"),
                ("info", "Rate limit aplicado ao cliente demo-{}: {} req/min"),
                ("debug", "Cache de rotas recarregado ({} entradas, {} ms)"),
                ("warn", "Upstream lento: checkout {} ms (limite {} ms)"),
            ],
            "checkout": [
                ("info", "Pedido DEMO-{} criado com {} itens"),
                ("info", "Carrinho DEMO-{} recalculado em {} ms"),
                ("debug", "Cupom DEMO{} validado ({} ms)"),
                ("warn", "Tentativa {} de reservar estoque para DEMO-{}"),
            ],
            "payments": [
                ("info", "Pagamento DEMO-{} autorizado em {} ms"),
                ("info", "Webhook do provider-demo recebido: lote {} ({} eventos)"),
                ("debug", "Pool de conexões: {} de {} em uso"),
                ("warn", "Retry {} ao consultar provider-demo para DEMO-{}"),
            ],
            "inventory": [
                ("info", "Estoque do SKU-{} atualizado: {} unidades"),
                ("info", "Reserva DEMO-{} confirmada em {} ms"),
                ("debug", "Sincronização parcial do catálogo: {} SKUs em {} ms"),
                ("warn", "Conflito de versão no SKU-{} (tentativa {})"),
            ],
            "auth": [
                ("info", "Sessão renovada para usuario-ficticio-{} ({} ms)"),
                ("info", "Login concluído para usuario-ficticio-{} em {} ms"),
                ("debug", "Chaves JWKS em cache: {} chaves, idade {} s"),
                ("warn", "Token expirado para usuario-ficticio-{} ({} s)"),
            ],
            "search": [
                ("info", "Busca por \"produto-demo-{}\" retornou {} resultados"),
                ("info", "Índice catalog-index atualizado: {} documentos ({} ms)"),
                ("debug", "Consulta reescrita com {} sinônimos ({} ms)"),
                ("warn", "Consulta lenta no catalog-index: {} ms ({} shards)"),
            ],
            "notifications": [
                ("info", "E-mail de confirmação DEMO-{} enfileirado ({} ms)"),
                ("info", "Push enviado para {} dispositivos em {} ms"),
                ("debug", "Template pedido-confirmado v{} renderizado em {} ms"),
                ("warn", "SMTP demo respondeu 421; nova tentativa em {} s ({})"),
            ],
            "worker": [
                ("info", "Mensagem orders.created processada em {} ms (partição {})"),
                ("info", "Lote de {} eventos consolidado em {} ms"),
                ("debug", "Offset do consumidor avançou para {} (lag {})"),
                ("warn", "Fila orders.created com {} mensagens pendentes (limite {})"),
            ],
        }
        weight = {
            "gateway": 5,
            "checkout": 3,
            "payments": 3,
            "inventory": 2,
            "auth": 3,
            "search": 3,
            "notifications": 1,
            "worker": 2,
        }
        ts = self.begin
        while ts < self.now:
            step = 60 if ts >= self.now - 3600 else 300
            for service in SERVICES:
                expected = weight[service] * diurnal(ts) * (1.6 if step == 300 else 0.45)
                for _ in range(int(expected + self.rng.random())):
                    at = min(self.now, ts + self.rng.uniform(0, step))
                    level, template = self.rng.choices(templates[service], [55, 30, 10, 5])[0]
                    message = template.format(self.rng.randint(1000, 9999), self.rng.randint(2, 900))
                    if self.degraded(at) and service in ("payments", "checkout", "worker") and self.rng.random() < 0.45:
                        level, message = {
                            "payments": (
                                "warn",
                                f"Circuit breaker do provider-demo em meia-abertura ({self.rng.randint(30, 70)}% de falhas)",
                            ),
                            "checkout": (
                                "warn",
                                f"Fila de confirmações acumulada: {self.rng.randint(80, 400)} pedidos aguardando pagamento",
                            ),
                            "worker": (
                                "warn",
                                f"Fila orders.created com {self.rng.randint(300, 900)} mensagens pendentes (limite 500)",
                            ),
                        }[service]
                    elif self.rng.random() < 0.012:
                        level, message = (
                            "error",
                            {
                                "gateway": "HTTP 499: cliente encerrou a conexão antes da resposta",
                                "checkout": "Falha ao serializar carrinho DEMO-{}: campo desconhecido".format(
                                    self.rng.randint(1000, 9999)
                                ),
                                "payments": "Webhook com assinatura inválida descartado (lote fictício)",
                                "inventory": "Deadlock detectado em stock_items; transação repetida",
                                "auth": "Falha ao validar assinatura do token (kid desconhecido)",
                                "search": "Timeout no shard 3 do catalog-index",
                                "notifications": "Falha permanente ao enviar e-mail: caixa inexistente (endereço fictício)",
                                "worker": "Mensagem malformada enviada para a DLQ orders.created.dlq",
                            }[service],
                        )
                    env = "staging" if service in STAGING and self.rng.random() < 0.08 else "production"
                    pod = self.rng.choice(self.pods[(service, env)])
                    self.logs.append(
                        {
                            "timestamp": at,
                            "service": service,
                            "env": env,
                            "level": level,
                            "message": message,
                            "tags": self.tags(pod),
                        }
                    )
            ts += step
        self.logs.append(
            {
                "timestamp": self.incident + 8 * 60,
                "service": "payments",
                "env": "production",
                "level": "critical",
                "message": f"Container payments reiniciado no pod {RESTARTING_POD}: OOMKilled (limite 512Mi)",
                "tags": self.tags(self.pods[("payments", "production")][0], reason="OOMKilled"),
            }
        )
        rows = sorted(self.logs, key=lambda row: row["timestamp"])
        for offset in range(0, len(rows), 2000):
            self.store.ingest_logs(self.p, rows[offset : offset + 2000])
        self.counts["logs"] = len(rows)
        self.otlp_logs()

    def otlp_logs(self):
        """A handful of OTLP log events correlated with incident spans (bodies are dropped by the privacy policy)."""
        request = ExportLogsServiceRequest()
        group = request.resource_logs.add()
        self.resource_attrs(group.resource.attributes, self.pods[("payments", "production")][1])
        scope = group.scope_logs.add()
        scope.scope.name = "loja-demo.events"
        incident = [item for item in self.error_spans if item[1] == "payments" and item[0] >= self.incident][:40]
        for start, _, trace, span in incident:
            record = scope.log_records.add(
                time_unix_nano=int(start * NS),
                severity_number=17,
                severity_text="ERROR",
                trace_id=bytes.fromhex(trace),
                span_id=bytes.fromhex(span),
                event_name="payments.authorization.failed",
            )
            record.body.string_value = "Falha de autorização (sintético)"
            set_attr(record.attributes, "error.type", "PaymentProviderTimeout")
        if incident:
            with self.store.connect() as db:
                privacy = project_privacy(db, self.p)
            self.store.signals.logs(self.p, request, privacy)
        self.counts["otlp_logs"] = len(incident)

    # ---------------------------------------------------------------- metrics
    def metric_times(self):
        times, ts = [], self.begin
        while ts < self.now - 3600:
            times.append(ts)
            ts += 300
        ts = self.now - 3600
        while ts <= self.now:
            times.append(ts)
            ts += 60
        return times

    def build_metrics(self):
        times = self.metric_times()
        series = []  # (pod-like resource, name, unit, kind, points)
        restart_at = [self.incident + 8 * 60, self.incident + 17 * 60, self.incident + 26 * 60]
        for (service, env), pods in self.pods.items():
            first = pods[0]
            # Latency histogram per service/env (delta temporality) with exemplars pointing at real spans.
            points, previous = [], times[0] - 300
            spans = self.server_spans.get(service, [])
            starts = [item[0] for item in spans]
            for ts in times:
                minutes = (ts - previous) / 60
                count = max(1, int(RATE[service] * diurnal(ts) * minutes * (0.15 if env == "staging" else 1)))
                samples = [self.service_ms(service, ts) for _ in range(min(count, 160))]
                scale = count / len(samples)
                buckets = [0] * (len(BOUNDS) + 1)
                for value in samples:
                    buckets[bisect.bisect_left(BOUNDS, value)] += 1
                buckets = [int(round(b * scale)) for b in buckets]
                point = {
                    "start": previous,
                    "t": ts,
                    "count": sum(buckets),
                    "sum": sum(samples) * scale,
                    "buckets": buckets,
                }
                lo, hi = bisect.bisect_left(starts, int(previous * NS)), bisect.bisect_right(starts, int(ts * NS))
                if hi > lo:
                    candidate = max(spans[lo:hi], key=lambda item: item[3])
                    point["exemplar"] = (candidate[1], candidate[2], candidate[3], candidate[0])
                points.append(point)
                previous = ts
            series.append((first, "http.server.request.duration", "ms", "histogram", points))
            for pod in pods:
                cpu, memory, restarts = [], [], []
                phase = self.rng.uniform(0, 6)
                base_memory = {"payments": 300, "search": 420, "worker": 260}.get(service, 180) * 2**20
                pod_start = self.begin - 3600
                for ts in times:
                    load = diurnal(ts)
                    value = 0.12 + 0.45 * load + 0.04 * math.sin(ts / 1700 + phase) + self.rng.gauss(0, 0.02)
                    mem = base_memory * (1 + 0.15 * load) + self.rng.gauss(0, 4 * 2**20)
                    restart_count = 0
                    if pod["pod"] == RESTARTING_POD:
                        restart_count = sum(ts >= r for r in restart_at)
                        if ts >= self.incident:
                            value += 0.35
                            since = ts - max([self.incident] + [r for r in restart_at if r <= ts])
                            mem = (
                                base_memory * 1.3 + since / 540 * 180 * 2**20
                            )  # climbs to the 512Mi limit, then restarts
                    elif service == "payments" and env == "production" and ts >= self.incident:
                        value += 0.22
                    if service == "worker" and ts >= self.incident:
                        value += 0.12
                    cpu.append({"t": ts, "value": round(min(0.99, max(0.01, value)), 4)})
                    memory.append({"t": ts, "value": float(int(max(64 * 2**20, mem)))})
                    restarts.append({"t": ts, "start": pod_start, "value": restart_count})
                series.append((pod, "system.cpu.utilization", "1", "gauge", cpu))
                series.append((pod, "process.memory.usage", "By", "gauge", memory))
                series.append((pod, "k8s.container.restarts", "{restart}", "sum", restarts))
            if env == "production" and service == "worker":
                depth = []
                for ts in times:
                    value = 20 + 60 * diurnal(ts) + self.rng.gauss(0, 6)
                    if ts >= self.incident:
                        value += min(320, (ts - self.incident) / 60 * 11)
                    depth.append({"t": ts, "value": round(max(0, value), 1)})
                series.append((first, "worker.queue.depth", "{message}", "gauge", depth))
            if env == "production" and service == "payments":
                pool = []
                for ts in times:
                    value = 8 + 18 * diurnal(ts) + self.rng.gauss(0, 2)
                    if ts >= self.incident:
                        value = 46 + self.rng.uniform(0, 4)
                    pool.append({"t": ts, "value": round(min(50, max(1, value)), 1)})
                series.append((first, "db.client.connection.count", "{connection}", "gauge", pool))
        self.export_metrics(series)
        self.legacy_metrics(times)

    def service_ms(self, service, ts):
        if service == "payments":
            ms = self.payments_server_ms(ts)
            if self.degraded(ts) and self.rng.random() < 0.03:
                ms = 3000 + self.rng.uniform(0, 50)
            return ms
        return {
            "gateway": lambda: self.latency("gw", ts) + self.latency("search", ts),
            "checkout": lambda: self.latency("checkout", ts) + self.latency("inv", ts) + self.payments_server_ms(ts),
            "inventory": lambda: self.latency("inv", ts) + self.latency("inv.db", ts),
            "auth": lambda: self.latency("auth", ts) + self.latency("auth.db", ts),
            "search": lambda: self.latency("search", ts) + self.latency("es", ts),
            "notifications": lambda: self.latency("notify", ts),
            "worker": lambda: self.latency("worker", ts) + self.latency("worker.db", ts),
        }[service]()

    def export_metrics(self, series):
        with self.store.connect() as db:
            privacy = project_privacy(db, self.p)
        request, budget, total = ExportMetricsServiceRequest(), 0, 0

        def flush():
            nonlocal request, budget
            if budget:
                self.store.signals.metrics(self.p, request, privacy)
            request, budget = ExportMetricsServiceRequest(), 0

        for pod, name, unit, kind, points in series:
            for offset in range(0, len(points), 1500):
                part = points[offset : offset + 1500]
                if budget + len(part) > 1800:
                    flush()
                group = request.resource_metrics.add(schema_url="https://opentelemetry.io/schemas/1.38.0")
                self.resource_attrs(group.resource.attributes, pod)
                scope = group.scope_metrics.add()
                scope.scope.name = "loja-demo.metrics"
                metric = scope.metrics.add(name=name, unit=unit, description=f"{name} (sintético)")
                if kind == "histogram":
                    metric.histogram.aggregation_temporality = 1
                    for item in part:
                        point = metric.histogram.data_points.add(
                            start_time_unix_nano=int(item["start"] * NS),
                            time_unix_nano=int(item["t"] * NS),
                            count=item["count"],
                            sum=item["sum"],
                        )
                        point.bucket_counts.extend(item["buckets"])
                        point.explicit_bounds.extend(BOUNDS)
                        if "exemplar" in item:
                            trace, span, value, at = item["exemplar"]
                            point.exemplars.add(
                                time_unix_nano=at,
                                as_double=value,
                                trace_id=bytes.fromhex(trace),
                                span_id=bytes.fromhex(span),
                            )
                elif kind == "sum":
                    metric.sum.aggregation_temporality = 2
                    metric.sum.is_monotonic = True
                    for item in part:
                        metric.sum.data_points.add(
                            start_time_unix_nano=int(item["start"] * NS),
                            time_unix_nano=int(item["t"] * NS),
                            as_int=int(item["value"]),
                        )
                else:
                    for item in part:
                        metric.gauge.data_points.add(time_unix_nano=int(item["t"] * NS), as_double=float(item["value"]))
                budget += len(part)
                total += len(part)
        flush()
        self.counts["otlp_metric_points"] = total

    def legacy_metrics(self, times):
        rows = []
        for ts in times:
            load = diurnal(ts)
            incident = self.degraded(ts)
            for service, name, kind, value in (
                (
                    "checkout",
                    "shop.checkout.orders",
                    "count",
                    max(0, int(40 * load * (0.55 if incident else 1) + self.rng.gauss(0, 3))),
                ),
                (
                    "payments",
                    "shop.payments.latency_ms",
                    "gauge",
                    self.payments_server_ms(ts) * (1.0 if not incident else 1.2),
                ),
                (
                    "payments",
                    "shop.payments.error_rate",
                    "gauge",
                    (11 + self.rng.uniform(-2, 3)) if incident else max(0, 0.4 + self.rng.gauss(0, 0.15)),
                ),
                (
                    "worker",
                    "shop.worker.queue_depth",
                    "gauge",
                    max(
                        0,
                        20
                        + 60 * load
                        + self.rng.gauss(0, 6)
                        + (min(320, (ts - self.incident) / 60 * 11) if incident else 0),
                    ),
                ),
                (
                    "gateway",
                    "shop.gateway.requests",
                    "count",
                    max(0, int(RATE["gateway"] * load * 5 + self.rng.gauss(0, 30))),
                ),
            ):
                rows.append(
                    {
                        "timestamp": ts,
                        "name": name,
                        "kind": kind,
                        "value": round(float(value), 2),
                        "service": service,
                        "env": "production",
                        "tags": self.tags(),
                    }
                )
        for offset in range(0, len(rows), 2000):
            self.store.ingest_metrics(self.p, rows[offset : offset + 2000])
        self.counts["legacy_metric_samples"] = len(rows)

    # ---------------------------------------------------------------- browser
    def build_browser(self):
        events = []
        sessions = 0
        before = [item for item in self.checkout_traces if item[0] < self.incident]
        during = [item for item in self.checkout_traces if item[0] >= self.incident]
        chosen = self.rng.sample(before, min(len(before), 40 + self.hours * 3)) + self.rng.sample(
            during, min(len(during), 22)
        )
        for ts, trace, failed in sorted(chosen):
            sessions += 1
            session = f"demo-{self.seed_id}-{sessions:03d}"
            degraded = ts >= self.incident
            counter = iter(range(1, 1000))

            def add(kind, at, **payload):
                events.append(
                    {
                        "kind": kind,
                        "session_id": session,  # noqa: B023 (called within this iteration)
                        "id": f"e{next(counter):03d}",  # noqa: B023 (called within this iteration)
                        "timestamp": at,
                        **payload,
                    }
                )

            start = ts - self.rng.uniform(4, 20)
            ttfb = 120 * math.exp(self.rng.gauss(0, 0.3))
            fcp = ttfb + 500 * math.exp(self.rng.gauss(0, 0.25))
            lcp = fcp + 900 * math.exp(self.rng.gauss(0, 0.3))
            if degraded:
                lcp *= self.rng.uniform(1.9, 2.8)
            add("navigation", start, duration_ms=round(lcp + 300, 1))
            for name, value in (
                ("TTFB", ttfb),
                ("FCP", fcp),
                ("LCP", lcp),
                ("CLS", abs(self.rng.gauss(0.04, 0.03)) + (0.08 if degraded else 0)),
                ("INP", 90 * math.exp(self.rng.gauss(0, 0.35)) * (1.8 if degraded else 1)),
            ):
                add("vital", start + 1.5, name=name, value=round(value, 3))
            for n in range(self.rng.randint(2, 4)):
                add("request", start + 2 + n, status=200, duration_ms=round(60 * math.exp(self.rng.gauss(0, 0.4)), 1))
            add(
                "request",
                ts,
                trace_id=trace,
                status=502 if failed else 200,
                duration_ms=round(400 + (2600 if failed else 0) + self.rng.uniform(0, 300), 1),
            )
            if failed:
                add("error", ts + 0.2, trace_id=trace, category="promise")
            replay = self.rng.random() < 0.45 or (degraded and failed)
            scroll = 0
            for n in range(self.rng.randint(3, 7)):
                at = start + 3 + n * 1.3
                if n % 2:
                    scroll = min(900, scroll + self.rng.randint(80, 260))
                    add("scroll", at, x=0, y=scroll)
                else:
                    add("click", at, x=self.rng.randint(40, 1180), y=self.rng.randint(120, 760))
                if replay:
                    add("frame", at + 0.1, **self.frame(scroll, failed and n > 2))
        for offset in range(0, len(events), 100):
            self.store.platform.ingest_browser(self.p, events[offset : offset + 100])
        self.counts["browser_sessions"], self.counts["browser_events"] = sessions, len(events)

    def frame(self, scroll, error):
        top = -scroll
        nodes = [
            [0, 0, 1280, 64, "#1f2a44ff", "#ffffffff", 0],  # header
            [32, 96 + top, 760, 40, "#00000000", "#1f2a44ff", 0],  # title
            [1180, 16, 72, 32, "#ffffff22", "#ffffffff", 16],
        ]  # cart badge
        for row in range(3):
            nodes.append([32, 160 + row * 140 + top, 760, 120, "#f4f6faff", "#1f2a44ff", 12])
            nodes.append([48, 176 + row * 140 + top, 88, 88, "#d9deebff", "#d9deebff", 8])
        nodes.append([832, 160 + top, 416, 360, "#ffffffff", "#1f2a44ff", 12])  # summary
        nodes.append([856, 440 + top, 368, 52, "#2f6fedff", "#ffffffff", 26])  # pay button
        if error:
            nodes.append([832, 540 + top, 416, 64, "#fde8e8ff", "#b42318ff", 10])  # error banner
        return {"version": 2, "width": 1280, "height": 800, "nodes": nodes}

    # --------------------------------------------------------------- profiles
    def build_profiles(self):
        count = 0
        stacks = {
            "payments": [
                ["main", "uvicorn.run", "handle_request", "charge", "provider_client.authorize", "ssl.read"],
                ["main", "uvicorn.run", "handle_request", "charge", "pool.acquire", "wait_for_connection"],
                ["main", "uvicorn.run", "handle_request", "charge", "json.dumps"],
                ["main", "uvicorn.run", "handle_request", "charge", "insert_payment", "psycopg.execute"],
            ],
            "checkout": [
                ["main", "gunicorn.worker", "checkout_view", "price_cart", "apply_promotions"],
                ["main", "gunicorn.worker", "checkout_view", "reserve_stock", "http.post"],
                ["main", "gunicorn.worker", "checkout_view", "serialize_order", "json.dumps"],
            ],
            "search": [
                ["main", "server.loop", "search_handler", "rank_results", "bm25_score"],
                ["main", "server.loop", "search_handler", "expand_synonyms"],
                ["main", "server.loop", "search_handler", "es_client.search", "socket.recv"],
            ],
            "gateway": [
                ["main", "event_loop", "proxy_request", "route_match"],
                ["main", "event_loop", "proxy_request", "tls.handshake"],
                ["main", "event_loop", "rate_limiter.check", "redis.get"],
            ],
            "worker": [
                ["main", "consumer.poll", "process_order_created", "render_template"],
                ["main", "consumer.poll", "reconcile_payments", "psycopg.fetchmany"],
            ],
        }
        healthy = {
            "payments": [8, 6, 3, 9],
            "checkout": [12, 6, 4],
            "search": [14, 5, 9],
            "gateway": [10, 7, 6],
            "worker": [9, 11],
        }
        step = (self.now - self.begin) / 8
        for service, frames in stacks.items():
            for n in range(8):
                ended = self.begin + (n + 1) * step - self.rng.uniform(0, 600)
                weights = [max(1, int(w * self.rng.uniform(5, 12))) for w in healthy[service]]
                self.store.platform.ingest_profile(
                    self.p,
                    {
                        "id": f"demo-{self.seed_id}-{service}-{n}",
                        "kind": "cpu",
                        "service": service,
                        "runtime": "python",
                        "timestamp": ended,
                        "started_at": ended - 30,
                        "stacks": [{"frames": f, "weight": w} for f, w in zip(frames, weights)],
                    },
                )
                count += 1
        for n, (start, trace, span, ms) in enumerate(sorted(self.slow_payments, key=lambda x: -x[3])[:4]):
            # CPU profile linked to a slow payments span: time dominated by TLS reads and pool waits.
            weights = [int(ms * 0.55), int(ms * 0.3), 6, 9]
            self.store.platform.ingest_profile(
                self.p,
                {
                    "id": f"demo-{self.seed_id}-payments-incident-{n}",
                    "kind": "cpu",
                    "service": "payments",
                    "runtime": "python",
                    "trace_id": trace,
                    "span_id": span,
                    "timestamp": start + ms / 1000,
                    "started_at": start,
                    "stacks": [{"frames": f, "weight": max(1, w)} for f, w in zip(stacks["payments"], weights)],
                },
            )
            count += 1
        if not self.slow_payments:
            self.notes.append("profiles: nenhum span lento de payments no incidente para vincular")
        self.store.platform.ingest_profile(
            self.p,
            {
                "id": f"demo-{self.seed_id}-payments-memory",
                "kind": "memory",
                "service": "payments",
                "runtime": "python",
                "timestamp": self.incident + 7 * 60,
                "stacks": [
                    {
                        "frames": ["main", "handle_request", "charge", "provider_client.authorize", "response_buffer"],
                        "weight": 182_000_000,
                    },
                    {"frames": ["main", "handle_request", "charge", "retry_queue.append"], "weight": 96_000_000},
                    {"frames": ["main", "metrics.registry", "histogram.observe"], "weight": 12_000_000},
                ],
            },
        )
        self.counts["profiles"] = count + 1

    # -------------------------------------------------------------- synthetic
    def build_synthetic(self):
        checks = [
            (
                "Demo · Jornada de checkout",
                "browser",
                "/checkout",
                journey_steps(
                    [
                        {"action": "navigate", "name": "Abrir produto", "path": "/produtos/demo-123"},
                        {"action": "click", "name": "Adicionar ao carrinho", "selector": "#adicionar"},
                        {"action": "navigate", "name": "Ir ao checkout", "path": "/checkout"},
                        {"action": "click", "name": "Pagar", "selector": "#pagar"},
                        {
                            "action": "assert_text",
                            "name": "Confirmar pagamento",
                            "selector": "body",
                            "value": "Pagamento aprovado",
                        },
                    ]
                ),
            ),
            ("Demo · API de saúde do gateway", "api", "/api/health", []),
            ("Demo · Busca de catálogo", "api", "/api/search?q=demo", []),
        ]
        with self.store.connect() as db:
            existing = {
                row["title"]: row for row in db.execute("SELECT * FROM synthetic_checks WHERE project=%s", (self.p,))
            }
        pending, results, logs = [], [], []
        self.synthetic = {}
        for title, kind, path, steps in checks:
            if title in existing:
                key, monitor = existing[title]["id"], existing[title]["monitor_id"]
            else:
                key, monitor = uuid.uuid4().hex, uuid.uuid4().hex
                with self.store.connect() as db:
                    db.execute(
                        """INSERT INTO monitors(id,project,title,metric,service,env,op,threshold,window_seconds,state,configuration)
                        VALUES(%s,%s,%s,'@synthetic','datalog-synthetic','synthetic','>',0,300,'NO_DATA',%s)""",
                        (monitor, self.p, title, Jsonb({"source": "synthetic", "check": key, "severity": "critical"})),
                    )
                    # next_at far ahead: the destination is fictional, so the live probe loop must not run it.
                    db.execute(
                        """INSERT INTO synthetic_checks(project,id,title,destination,kind,cadence,location,expected,next_at,monitor_id,steps)
                        VALUES(%s,%s,%s,%s,%s,300,'sa-east-1-demo',%s,%s,%s,%s)""",
                        (
                            self.p,
                            key,
                            title,
                            "loja-demo" + path.split("?")[0].replace("/", "-"),
                            kind,
                            "Pagamento aprovado" if kind == "browser" else None,
                            self.now + 30 * 86400,
                            monitor,
                            Jsonb(steps),
                        ),
                    )
            self.synthetic[title] = (key, monitor)
            ts = self.begin + 30
            while ts < self.now:
                ctx = {
                    "trace": self.ids.make(16),
                    "env": "synthetic",
                    "pods": {"datalog-synthetic": self.synthetic_pod()},
                }
                root = self.ids.make(8)
                failing = (kind == "browser" and ts >= self.incident + 3 * 60) or self.rng.random() < 0.004
                timeline, cursor = [], ts
                for index, step in enumerate([{"action": "open", "name": "Abrir destino"}, *steps] if steps else []):
                    ms = (
                        180
                        * math.exp(self.rng.gauss(0, 0.3))
                        * (4 if failing and step["action"] == "click" and index == 4 else 1)
                    )
                    ok = not (failing and index == len(steps))
                    timeline.append(
                        {"name": step["name"], "action": step["action"], "ok": ok, "duration_ms": round(ms, 1)}
                    )
                    self.span(
                        pending,
                        ctx,
                        "datalog-synthetic",
                        "synthetic." + step["action"],
                        INTERNAL,
                        cursor,
                        cursor + ms / 1000,
                        root,
                        {},
                        None if ok else "synthetic.assert_text",
                    )
                    cursor += ms / 1000
                if not steps:
                    cursor = ts + (60 + self.rng.uniform(0, 80)) / 1000
                step_name = (
                    (f"step_{len(steps) + 1}" if steps else "http")
                    if failing
                    else ("journey" if steps else "assertion" if kind == "api" else "http")
                )
                self.record(
                    pending,
                    ctx,
                    "datalog-synthetic",
                    "synthetic." + kind,
                    SERVER,
                    ts,
                    cursor,
                    None,
                    root,
                    {},
                    ("synthetic." + step_name) if failing else None,
                )
                results.append(
                    (
                        self.p,
                        key,
                        cursor,
                        not failing,
                        (cursor - ts) * 1000,
                        ctx["trace"].hex(),
                        root.hex(),
                        step_name,
                        Jsonb(timeline),
                    )
                )
                if failing or self.rng.random() < 0.05:
                    logs.append(
                        {
                            "timestamp": cursor,
                            "service": "datalog-synthetic",
                            "env": "synthetic",
                            "level": "error" if failing else "info",
                            "message": "synthetic." + step_name,
                            "trace_id": ctx["trace"].hex(),
                            "span_id": root.hex(),
                            "tags": self.tags(),
                        }
                    )
                ts += 300
        self.export_spans(pending)
        with self.store.connect() as db:
            db.cursor().executemany(
                """INSERT INTO synthetic_results(project,check_id,timestamp,ok,duration_ms,trace_id,span_id,step,steps)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                results,
            )
        for offset in range(0, len(logs), 2000):
            self.store.ingest_logs(self.p, logs[offset : offset + 2000])
        self.counts["synthetic_checks"], self.counts["synthetic_results"] = len(checks), len(results)

    def synthetic_pod(self):
        return {
            "service": "datalog-synthetic",
            "env": "synthetic",
            "pod": "datalog-synthetic-runner",
            "node": "demo-probe-sa-east",
            "container": "f" * 24,
            "uid": str(uuid.UUID(int=1)),
        }

    # --------------------------------------------------------------------- AI
    def build_ai(self):
        if self.p == "demo":
            from datalog.seed_demo import seed as seed_demo

            demo = seed_demo(self.store)
            self.counts["seed_demo"] = demo["created"]
            self.score_showcase(demo["seed_id"])
        else:
            self.notes.append("seed_demo só popula o projeto 'demo'; runs JSON de agentes não foram criados")
        self.agent_spans()
        self.prompt_history()

    def prompt_history(self):
        """Two weeks of runs for one prompt with five versions, including a regression (v3) and its fix.

        Prompt creation times are back-dated directly (the API stamps "now"), so the version
        timeline in Prompts & versões has history to show. All content is synthetic.
        """
        rng = random.Random(42)
        now = time.time()
        name = f"demo/suporte-politicas-{self.seed_id[:4]}"
        versions = [
            (13.5, "Você é o agente de suporte. Responda com base na política de atendimento.\nSeja cordial."),
            (
                10.2,
                "Você é o agente de suporte. Responda com base na política de atendimento.\nSeja cordial.\nCite o item da política usado.",
            ),
            (7.1, "Você é o agente de suporte. Responda de forma informal e curta.\nSeja cordial."),
            (
                6.4,
                "Você é o agente de suporte. Responda com base na política de atendimento.\nSeja cordial.\nCite o item e o parágrafo da política usados.\nSe não houver base na política, diga que não encontrou.",
            ),
            (
                1.8,
                "Você é o agente de suporte. Responda com base na política de atendimento.\nSeja cordial e objetivo, em até 80 palavras.\nCite o item e o parágrafo da política usados.\nSe não houver base na política, diga que não encontrou.",
            ),
        ]
        created = []
        for days_ago, content in versions:
            row = self.store.ai.create_prompt(self.p, {"name": name, "content": content, "model": "atlas-pro-demo"})
            created.append((now - days_ago * 86400, row["version"]))
        with self.store.connect() as db:
            for ts, version in created:
                db.execute(
                    "UPDATE ai_prompts SET timestamp=%s WHERE project=%s AND name=%s AND version=%s",
                    (ts, self.p, name, version),
                )
        quality = {1: (0.80, 0.88), 2: (0.86, 0.93), 3: (0.66, 0.76), 4: (0.88, 0.95), 5: (0.90, 0.96)}
        cost = {1: 0.006, 2: 0.0068, 3: 0.0049, 4: 0.0072, 5: 0.0064}
        traces, scores = [], []
        start_at = created[0][0]
        for i in range(int((now - 300 - start_at) / 2400)):
            ts = start_at + i * 2400 + rng.uniform(0, 1800)
            version = max(v for t, v in created if t <= ts)
            failed = rng.random() < (0.08 if version == created[2][1] else 0.02)
            duration = rng.uniform(2.2, 6.5) * (1.25 if version == created[2][1] else 1)
            tid = f"hist-{self.seed_id}-{i:04d}"
            tokens_in, tokens_out = rng.randint(900, 2600), rng.randint(120, 480)
            spans = [
                {
                    "id": "root",
                    "name": "Suporte ao cliente",
                    "kind": "agent",
                    "parent_id": None,
                    "start_time": ts,
                    "end_time": ts + duration,
                    "status": "error" if failed else "ok",
                    "metadata": {"dataset": "synthetic-history"},
                },
                {
                    "id": "kb",
                    "name": "Buscar contexto na base de conhecimento",
                    "kind": "retriever",
                    "parent_id": "root",
                    "start_time": ts + 0.1,
                    "end_time": ts + 0.7,
                    "status": "ok",
                    "metadata": {"dataset": "synthetic-history"},
                },
                {
                    "id": "answer",
                    "name": "Responder com a política",
                    "kind": "llm",
                    "parent_id": "root",
                    "start_time": ts + 0.8,
                    "end_time": ts + duration - 0.1,
                    "status": "error" if failed else "ok",
                    "model": "atlas-pro-demo",
                    "provider": "provider-demo",
                    "prompt_name": name,
                    "prompt_version": version,
                    "input_tokens": tokens_in,
                    "output_tokens": tokens_out,
                    "cost_usd": round(cost[version - created[0][1] + 1] * rng.uniform(0.85, 1.15), 6),
                    "cost_source": "estimated",
                    "metadata": {"dataset": "synthetic-history"},
                },
            ]
            traces.append(
                {
                    "id": tid,
                    "name": f"Suporte ao cliente · histórico {i:04d}",
                    "agent": "Suporte ao cliente",
                    "service": "support-agent",
                    "env": "demo",
                    "start_time": ts,
                    "end_time": ts + duration,
                    "status": "error" if failed else "ok",
                    "metadata": {"dataset": "synthetic-history", "seed_id": self.seed_id},
                    "spans": spans,
                }
            )
            if not failed and rng.random() < 0.8:
                lo, hi = quality[version - created[0][1] + 1]
                scores.append(
                    {
                        "trace_id": tid,
                        "name": "relevancia",
                        "value": round(rng.uniform(lo, hi), 2),
                        "source": "external",
                        "comment": "Nota fictícia de demonstração; nenhum modelo avaliador foi executado.",
                    }
                )
        for offset in range(0, len(traces), 200):
            self.store.ai.ingest(self.p, traces[offset : offset + 200])
        for score in scores:
            self.store.ai.score(self.p, score)
        # Scores are stamped "now"; align them with their runs so the daily quality timeline is real.
        with self.store.connect() as db:
            db.execute(
                "UPDATE ai_scores s SET timestamp=t.timestamp+60 FROM ai_traces t WHERE s.project=%s AND t.project=s.project AND t.id=s.trace_id AND t.id LIKE %s",
                (self.p, f"hist-{self.seed_id}-%"),
            )
        self.counts["prompt_history"] = {
            "prompt": name,
            "versions": len(created),
            "runs": len(traces),
            "scores": len(scores),
        }

    def score_showcase(self, demo_seed):
        rng = random.Random(7)
        scored = 0
        for i in range(0, 176, 3):
            if i % 17 == 0:
                continue
            late = i >= 120  # the most recent runs regress on fidelity (synthetic)
            for criterion, value in (
                ("fidelidade", rng.uniform(0.45, 0.7) if late else rng.uniform(0.78, 0.97)),
                ("relevancia", rng.uniform(0.7, 0.95)),
                ("tom", rng.uniform(0.8, 0.98)),
            ):
                try:
                    self.store.ai.score(
                        self.p,
                        {
                            "trace_id": f"showcase-{demo_seed}-{i:03d}",
                            "name": criterion,
                            "value": round(value, 2),
                            "source": "external",
                            "comment": "Nota fictícia de demonstração; nenhum modelo avaliador foi executado.",
                        },
                    )
                    scored += 1
                except ValueError as exc:
                    self.notes.append(f"ai score: {exc}")
                    return
        self.counts["ai_scores"] = scored

    def agent_spans(self):
        """OTLP GenAI spans (agents page), a golden dataset with paired evaluations and online evaluators."""
        pending = []
        with self.store.connect() as db:
            prompts = {
                (r["name"], r["version"])
                for r in db.execute("SELECT name,version FROM ai_prompts WHERE project=%s", (self.p,))
            }
        prompt = "demo/suporte-ao-cliente"
        pod = {
            "service": "support-agent",
            "env": "production",
            "pod": "support-agent-7c9d5-k2m8q",
            "node": "demo-node-b",
            "container": "a" * 24,
            "uid": str(uuid.UUID(int=2)),
        }
        canary = self.now - min(6, self.hours / 3) * 3600
        questions = [
            "Onde está meu pedido DEMO-{}?",
            "Quero trocar o produto do pedido DEMO-{}.",
            "Meu pagamento DEMO-{} foi recusado, o que fazer?",
            "Como cancelar a assinatura DEMO-{}?",
        ]

        def run(ts, env, version, service_pod, failed=False):
            ctx = {"trace": self.ids.make(16), "env": env, "pods": {"support-agent": service_pod}}
            root, chat_id = self.ids.make(8), self.ids.make(8)
            model = "atlas-pro-demo" if self.rng.random() < 0.7 else "atlas-fast-demo"
            ttft = 380 * math.exp(self.rng.gauss(0, 0.3)) * (1.3 if version.startswith("v2.5") else 1)
            chat_ms = ttft + 1400 * math.exp(self.rng.gauss(0, 0.35))
            tool_ms = 120 * math.exp(self.rng.gauss(0, 0.4)) * (3 if self.degraded(ts) else 1)
            tool_error = self.degraded(ts) and self.rng.random() < 0.2
            inp, out = self.rng.randint(900, 5200), self.rng.randint(120, 900)
            chat_attrs = {
                "gen_ai.operation.name": "chat",
                "gen_ai.provider.name": "provider-demo",
                "gen_ai.request.model": model,
                "gen_ai.response.model": model,
                "gen_ai.usage.input_tokens": inp,
                "gen_ai.usage.output_tokens": out,
                "datalog.cost.usd": round(inp * 2.5e-6 + out * 1e-5, 6),
                "datalog.cost.source": "estimated",
                "datalog.gen_ai.streaming": True,
                "datalog.gen_ai.ttft_ms": round(ttft, 1),
                "datalog.execution.version": version,
            }
            pv = 2 if version.startswith("v2.5") else 1
            if (prompt, pv) in prompts:
                chat_attrs.update({"datalog.prompt.name": prompt, "datalog.prompt.version": pv})
            t_tool = ts + 0.05
            self.record(
                pending,
                ctx,
                "support-agent",
                "execute_tool consultar_pagamento",
                INTERNAL,
                t_tool,
                t_tool + tool_ms / 1000,
                root,
                self.ids.make(8),
                {"gen_ai.operation.name": "execute_tool", "tool.name": "consultar_pagamento"},
                "ToolTimeout" if tool_error else None,
            )
            t_chat = t_tool + tool_ms / 1000 + 0.01
            self.record(
                pending,
                ctx,
                "support-agent",
                f"chat {model}",
                CLIENT,
                t_chat,
                t_chat + chat_ms / 1000,
                root,
                chat_id,
                chat_attrs,
                "ModelTimeout" if failed else None,
            )
            self.record(
                pending,
                ctx,
                "support-agent",
                "invoke_agent suporte-demo",
                INTERNAL,
                ts,
                t_chat + chat_ms / 1000 + 0.02,
                None,
                root,
                {"gen_ai.operation.name": "invoke_agent", "datalog.execution.version": version},
                "ModelTimeout" if failed else None,
            )
            return ctx["trace"].hex(), chat_id.hex()

        live = 0
        target = max(30, self.hours * 5)
        for n in range(target):
            ts = self.begin + (n + self.rng.random()) * (self.now - 60 - self.begin) / target
            version = "v2.5.0-demo" if ts >= canary and self.rng.random() < 0.6 else "v2.4.0-demo"
            run(ts, "production", version, pod, failed=self.rng.random() < 0.03)
            live += 1
        # Paired offline evaluation: same golden cases against both execution versions.
        cases = [
            {
                "id": f"caso-{n:02d}",
                "input": {"message": questions[n % 4].format(2000 + n)},
                "expected": {"intent": ["rastrear", "trocar", "pagamento", "cancelar"][n % 4]},
            }
            for n in range(20)
        ]
        dataset = self.store.agents.create_dataset(
            self.p,
            {
                "name": "demo/suporte-golden",
                "cases": cases,
                "splits": {
                    "pagamentos": [c["id"] for c in cases if c["id"] in {f"caso-{n:02d}" for n in range(2, 20, 4)}]
                },
            },
        )
        eval_pod = {**pod, "env": "evaluation", "pod": "support-agent-eval-runner"}
        pairs = []
        for case in cases:
            for version in ("v2.4.0-demo", "v2.5.0-demo"):
                pairs.append(
                    (
                        case["id"],
                        version,
                        *run(self.now - 1800 - self.rng.uniform(0, 600), "evaluation", version, eval_pod),
                    )
                )
        self.export_spans(pending)
        scores = 0
        rng = random.Random(11)
        for case_id, version, trace, span in pairs:
            new = version.startswith("v2.5")
            for criterion, value in (
                ("fidelidade", rng.uniform(0.5, 0.75) if new else rng.uniform(0.8, 0.97)),
                ("relevancia", rng.uniform(0.78, 0.95) if new else rng.uniform(0.75, 0.93)),
                ("tom", rng.uniform(0.86, 0.99) if new else rng.uniform(0.7, 0.9)),
            ):
                body = {
                    "dataset": dataset["name"],
                    "dataset_version": dataset["version"],
                    "case_id": case_id,
                    "trace_id": trace,
                    "span_id": span,
                    "execution_version": version,
                    "criterion": criterion,
                    "score": round(value, 3),
                }
                pv = 2 if new else 1
                if (prompt, pv) in prompts:
                    body.update(prompt_name=prompt, prompt_version=pv)
                self.store.agents.score(self.p, body)
                scores += 1
        self.counts.update(
            agent_runs_otlp=live, dataset=f"{dataset['name']} v{dataset['version']}", agent_evaluations=scores
        )
        # Online evaluators (created at the window start so existing spans are eligible).
        evaluators = [
            ("Demo · sucesso técnico", {"rule": "technical_success", "sample_rate": 1.0, "threshold": 0.0}),
            ("Demo · latência abaixo de 4 s", {"rule": "latency_ms", "sample_rate": 1.0, "threshold": 4000.0}),
            ("Demo · custo por chamada", {"rule": "cost_usd", "sample_rate": 0.5, "threshold": 0.02}),
        ]
        with self.store.connect() as db:
            for name, config in evaluators:
                if not db.execute(
                    "SELECT 1 FROM online_evaluators WHERE project=%s AND name=%s", (self.p, name)
                ).fetchone():
                    db.execute(
                        "INSERT INTO online_evaluators VALUES(%s,%s,1,%s,%s)",
                        (
                            self.p,
                            name,
                            Jsonb({**config, "service": "support-agent", "env": "production"}),
                            int((self.begin - 60) * NS),
                        ),
                    )
        processed = 0
        for _ in range(12):
            done = self.store.workflows.evaluate(self.p)["processed"]
            processed += done
            if not done:
                break
        self.counts["online_scores"] = processed

    # ------------------------------------------------------------------- data
    def build_data(self):
        assets = {
            "raw.orders": ["id", "customer_id", "total", "status", "created_at"],
            "raw.payments": ["id", "order_id", "amount", "status", "provider", "created_at"],
            "staging.stg_orders": ["order_id", "customer_id", "total_brl", "status", "created_at"],
            "staging.stg_payments": ["payment_id", "order_id", "amount_brl", "payment_status", "authorized_at"],
            "mart.orders_daily": ["day", "orders", "revenue_brl", "avg_ticket_brl"],
            "mart.payment_success_rate": ["hour", "attempts", "approved", "success_rate"],
        }
        jobs = [
            ("airflow.ingest_orders", [], "raw.orders", {}),
            ("airflow.ingest_payments", [], "raw.payments", {}),
            (
                "dbt.stg_orders",
                ["raw.orders"],
                "staging.stg_orders",
                {
                    "order_id": [("raw.orders", "id")],
                    "customer_id": [("raw.orders", "customer_id")],
                    "total_brl": [("raw.orders", "total")],
                    "status": [("raw.orders", "status")],
                    "created_at": [("raw.orders", "created_at")],
                },
            ),
            (
                "dbt.stg_payments",
                ["raw.payments"],
                "staging.stg_payments",
                {
                    "payment_id": [("raw.payments", "id")],
                    "order_id": [("raw.payments", "order_id")],
                    "amount_brl": [("raw.payments", "amount")],
                    "payment_status": [("raw.payments", "status")],
                    "authorized_at": [("raw.payments", "created_at")],
                },
            ),
            (
                "dbt.orders_daily",
                ["staging.stg_orders", "staging.stg_payments"],
                "mart.orders_daily",
                {
                    "day": [("staging.stg_orders", "created_at")],
                    "orders": [("staging.stg_orders", "order_id")],
                    "revenue_brl": [("staging.stg_orders", "total_brl"), ("staging.stg_payments", "amount_brl")],
                    "avg_ticket_brl": [("staging.stg_orders", "total_brl")],
                },
            ),
            (
                "dbt.payment_success_rate",
                ["staging.stg_payments"],
                "mart.payment_success_rate",
                {
                    "hour": [("staging.stg_payments", "authorized_at")],
                    "attempts": [("staging.stg_payments", "payment_id")],
                    "approved": [("staging.stg_payments", "payment_status")],
                    "success_rate": [("staging.stg_payments", "payment_status")],
                },
            ),
            (
                "bi.refresh_receita_diaria",
                ["mart.orders_daily"],
                "dashboard.receita_diaria",
                {"receita": [("mart.orders_daily", "revenue_brl")], "pedidos": [("mart.orders_daily", "orders")]},
            ),
        ]

        def ds(name):
            return {"namespace": "bi://demo-reports" if name.startswith("dashboard.") else WAREHOUSE, "name": name}

        runs = failed = 0
        hours = max(2, min(self.hours, 48))
        for h in range(hours - 1, -1, -1):
            slot = self.now - h * 3600 - 1200
            for offset, (job, inputs, output, columns) in enumerate(jobs):
                start = slot + offset * 120 + self.rng.uniform(0, 30)
                duration = self.rng.uniform(25, 95)
                last = h == 0
                # The latest payments runs fail because raw.payments stopped arriving during the incident.
                fail = last and job in ("dbt.stg_payments", "dbt.payment_success_rate")
                stale = last and job == "airflow.ingest_payments"
                run_id = str(uuid.UUID(bytes=self.ids.make(16)))
                output_ds = ds(output)
                if columns:
                    output_ds["facets"] = {
                        "columnLineage": {
                            "fields": {
                                field: {"inputFields": [{**ds(src), "field": col} for src, col in sources]}
                                for field, sources in columns.items()
                            }
                        }
                    }
                base = {
                    "job": {"namespace": "loja-demo", "name": job},
                    "run": {"runId": run_id, "facets": {"nominalTime": {"nominalStartTime": slot}}},
                    "inputs": [ds(i) for i in inputs],
                    "outputs": [output_ds],
                    "producer": "https://example.invalid/datalog-seed-full",
                    "schemaURL": "https://openlineage.io/spec/2-0-2/OpenLineage.json",
                }
                if fail:
                    base["run"]["facets"]["airflow"] = {
                        "dagRun": {"run_id": f"scheduled__{int(slot)}", "dag_id": "loja_demo_pagamentos"},
                        "taskInstance": {"try_number": 1},
                    }
                self.store.data.lineage.receive(self.p, {**base, "eventType": "START", "eventTime": start})
                if stale:
                    self.store.data.lineage.receive(self.p, {**base, "eventType": "RUNNING", "eventTime": start + 5})
                else:
                    self.store.data.lineage.receive(
                        self.p,
                        {
                            **base,
                            "eventType": "FAIL" if fail else "COMPLETE",
                            "eventTime": start + duration * (3 if fail else 1),
                        },
                    )
                runs += 1
                if fail:
                    failed += 1
                    retry = {
                        **base,
                        "run": {
                            "runId": str(uuid.UUID(bytes=self.ids.make(16))),
                            "facets": {
                                **base["run"]["facets"],
                                "airflow": {
                                    "dagRun": base["run"]["facets"]["airflow"]["dagRun"],
                                    "taskInstance": {"try_number": 2},
                                },
                            },
                        },
                    }
                    self.store.data.lineage.receive(
                        self.p, {**retry, "eventType": "START", "eventTime": start + duration * 3 + 60}
                    )
                    runs += 1
        self.counts["data_job_runs"], self.counts["data_job_failures"] = runs, failed
        # Quality: observations shaped like DataQuality.inspect() (no live warehouse connection exists).
        policies = {
            "raw.payments": {
                "freshness": {"column": "created_at", "max_age_seconds": 1800},
                "volume": {"min_rows": 1000},
            },
            "staging.stg_payments": {
                "checks": [{"kind": "not_null", "column": "payment_status"}, {"kind": "unique", "column": "payment_id"}]
            },
            "mart.orders_daily": {"volume": {"min_rows": 1, "max_change_percent": 50}},
            "mart.payment_success_rate": {
                "freshness": {"column": "hour", "max_age_seconds": 3600},
                "checks": [{"kind": "not_null", "column": "success_rate"}],
            },
            "staging.stg_orders": {"checks": [{"kind": "unique", "column": "order_id"}], "volume": {"min_rows": 1000}},
        }
        observations, ids = [], {}
        with self.store.connect() as db:
            for name, columns in assets.items():
                ident = digest(WAREHOUSE, name)[:32]
                ids[name] = ident
                db.execute(
                    """INSERT INTO data_assets(project,id,namespace,name) VALUES(%s,%s,%s,%s)
                    ON CONFLICT(project,namespace,name) DO NOTHING""",
                    (self.p, ident, WAREHOUSE, name),
                )
                schema = {
                    c: {
                        "type": "timestamp with time zone"
                        if c.endswith(("_at", "day", "hour"))
                        else "numeric"
                        if c.endswith(("_brl", "rate", "total", "amount"))
                        else "text",
                        "nullable": c not in ("id", "order_id"),
                    }
                    for c in columns
                }
                if name in policies:
                    db.execute(
                        "UPDATE data_assets SET policy=%s,interval_seconds=3600,schema_baseline=coalesce(schema_baseline,%s) WHERE project=%s AND id=%s",
                        (Jsonb(DataQuality.policy(policies[name])), Jsonb(schema), self.p, ident),
                    )
                    rows = self.rng.randint(40_000, 90_000)
                    for h in range(hours, -1, -1):
                        at = self.now - h * 3600 - 120
                        rows += self.rng.randint(800, 2500)
                        issues, checks = [], []
                        latest = at - self.rng.uniform(60, 900)
                        policy = policies[name]
                        incident = at >= self.incident
                        if incident and "freshness" in policy:
                            latest = self.incident - 300
                            age = at - latest
                            if age > policy["freshness"]["max_age_seconds"] or name == "mart.payment_success_rate":
                                issues.append(
                                    {
                                        "kind": "freshness",
                                        "age_seconds": round(age),
                                        "max_age_seconds": policy["freshness"]["max_age_seconds"],
                                    }
                                )
                        for check in policy.get("checks", []):
                            violations = 0
                            if incident and check["kind"] == "not_null" and name == "staging.stg_payments":
                                violations = self.rng.randint(120, 400)
                            checks.append({**check, "max_violations": 0, "violations": violations})
                            if violations:
                                issues.append(
                                    {"kind": check["kind"], "column": check["column"], "violations": violations}
                                )
                        result = {
                            "rows": rows,
                            "latest": latest,
                            "schema": schema,
                            "volume_baseline": rows - 1500,
                            "checks": checks,
                            "reconcile": None,
                            "reconcile_rows": None,
                            "issues": issues,
                        }
                        observations.append(
                            (self.p, ident, uuid.uuid4().hex, at, "alert" if issues else "ok", Jsonb(result))
                        )
            db.cursor().executemany("INSERT INTO data_observations VALUES(%s,%s,%s,%s,%s,%s)", observations)
            for name in policies:
                DataQuality.monitor(
                    db,
                    self.p,
                    "seed-full",
                    ids[name],
                    f"Demo · Qualidade de dados · {name}",
                    "data_quality",
                    {"asset_id": ids[name]},
                )
            job = digest("loja-demo", "dbt.stg_payments")
            DataQuality.monitor(
                db,
                self.p,
                "seed-full",
                job,
                "Demo · Job de dados · dbt.stg_payments",
                "data_job",
                {"namespace": "loja-demo", "job": "dbt.stg_payments", "max_duration_seconds": 600},
            )
            dashboard_id = digest("bi://demo-reports", "dashboard.receita_diaria")[:32]
            existing = {row["name"] for row in db.execute("SELECT name FROM data_products WHERE project=%s", (self.p,))}
        self.counts["data_assets"], self.counts["data_observations"] = len(assets) + 1, len(observations)
        products = [
            ("Demo · Receita e pedidos", [("mart.orders_daily", "table"), ("staging.stg_orders", "table")], True),
            (
                "Demo · Pagamentos",
                [("staging.stg_payments", "table"), ("mart.payment_success_rate", "table"), ("raw.payments", "table")],
                False,
            ),
        ]
        for title, members, report in products:
            if title in existing:
                continue
            items = [{"asset_id": ids[n], "role": r} for n, r in members]
            if report:
                items.append({"asset_id": dashboard_id, "role": "consumer"})
            self.store.data_products.save(self.p, "seed-full", {"name": title, "members": items})
        self.store.data_products.register_report(
            self.p, "seed-full", {"id": dashboard_id, "url": "https://bi.demo.example/receita-diaria"}
        )
        self.counts["data_products"] = len(products)

    # --------------------------------------------------------------- monitors
    def build_monitors(self):
        pay_alert = [item for item in self.error_spans if item[1] == "payments" and item[0] >= self.incident]
        evidence_span = pay_alert[-1] if pay_alert else None
        specs = [
            # title, metric, service, env, op, threshold, window, options, history
            (
                "Demo · p95 de latência em payments",
                "http.server.request.duration",
                "payments",
                "production",
                ">",
                800,
                300,
                {"source": "otlp", "stat": "p95", "severity": "critical"},
                [(-20 * 3600, "OK"), (-14.2 * 3600, "ALERT"), (-14 * 3600, "OK"), (self.incident + 240, "ALERT")],
            ),
            (
                "Demo · SLO de disponibilidade de payments (99,5%)",
                "@slo",
                "payments",
                "production",
                ">",
                6,
                300,
                {"source": "slo", "target": 0.995, "short_window": 600, "long_window": 3600, "severity": "critical"},
                [(-22 * 3600, "OK"), (self.incident + 420, "ALERT")],
            ),
            (
                "Demo · Reinícios do pod de payments",
                "k8s.container.restarts",
                "payments",
                "production",
                ">",
                0,
                900,
                {
                    "source": "otlp",
                    "stat": "value",
                    "severity": "warning",
                    "resource_key": "k8s.pod.name",
                    "resource_id": RESTARTING_POD,
                },
                [(-23 * 3600, "OK"), (self.incident + 8 * 60, "ALERT")],
            ),
            (
                "Demo · Erros em spans do search",
                "@otlp.span.errors",
                "search",
                "production",
                ">",
                10,
                600,
                {"source": "legacy", "severity": "warning"},
                [(-23 * 3600, "OK"), (-9 * 3600, "ALERT"), (-8.8 * 3600, "OK")],
            ),
            (
                "Demo · CPU do gateway",
                "system.cpu.utilization",
                "gateway",
                "production",
                ">",
                0.9,
                300,
                {"source": "otlp", "stat": "value", "severity": "warning"},
                [(-23 * 3600, "OK"), (-5.5 * 3600, "ALERT"), (-5.3 * 3600, "OK")],
            ),
            (
                "Demo · p95 de latência do search",
                "http.server.request.duration",
                "search",
                "production",
                ">",
                600,
                300,
                {"source": "otlp", "stat": "p95", "severity": "warning"},
                [(-23 * 3600, "OK")],
            ),
            (
                "Demo · Fila orders.created no worker",
                "worker.queue.depth",
                "worker",
                "production",
                ">",
                500,
                300,
                {"source": "otlp", "stat": "value", "severity": "warning"},
                [(-23 * 3600, "OK")],
            ),
            (
                "Demo · Qualidade online do agente de suporte",
                "Demo · sucesso técnico",
                "support-agent",
                "production",
                "<",
                0.8,
                3600,
                {"source": "online", "severity": "warning"},
                [(-12 * 3600, "NO_DATA"), (-11.9 * 3600, "OK")],
            ),
            (
                "Demo · Heartbeat do coletor de borda",
                "edge.collector.heartbeat",
                "",
                "production",
                "<",
                1,
                300,
                {"source": "legacy", "severity": "info"},
                [(-23 * 3600, "OK"), (-6 * 3600, "NO_DATA")],
            ),
            (
                "Demo · p95 de notifications em staging",
                "http.server.request.duration",
                "notifications",
                "staging",
                ">",
                500,
                300,
                {"source": "otlp", "stat": "p95", "severity": "info"},
                [(-23 * 3600, "NO_DATA")],
            ),
        ]
        with self.store.connect() as db:
            existing = {row["title"] for row in db.execute("SELECT title FROM monitors WHERE project=%s", (self.p,))}
            created = 0
            for title, metric, service, env, op, threshold, window, raw, history in specs:
                if title in existing:
                    continue
                ident = uuid.uuid4().hex
                db.execute(
                    """INSERT INTO monitors(id,project,title,metric,service,env,op,threshold,window_seconds,state,value,evaluated_at,configuration)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,'NO_DATA',NULL,NULL,%s)""",
                    (ident, self.p, title, metric, service, env, op, threshold, window, Jsonb(alert_options(raw))),
                )
                self.history(db, ident, history, threshold, window, evidence_span if service == "payments" else None)
                created += 1
            # History for the synthetic and data monitors created earlier in this run.
            history_targets = [
                (
                    self.synthetic["Demo · Jornada de checkout"][1],
                    [(-23 * 3600, "OK"), (self.incident + 6 * 60, "ALERT")],
                ),
                (self.synthetic["Demo · API de saúde do gateway"][1], [(-23 * 3600, "OK")]),
                (self.synthetic["Demo · Busca de catálogo"][1], [(-23 * 3600, "OK")]),
            ]
            for monitor_id, history in history_targets:
                if not db.execute(
                    "SELECT 1 FROM evaluations WHERE project=%s AND monitor_id=%s LIMIT 1", (self.p, monitor_id)
                ).fetchone():
                    self.history(db, monitor_id, history, 0, 300, None)
        self.counts["monitors_created"] = created

    def history(self, db, ident, history, threshold, window, span):
        last = None
        for offset, state in history:
            at = self.now + offset if offset < 0 else offset
            if at < self.begin:
                at = self.begin + 60
            value = (
                None
                if state == "NO_DATA"
                else (
                    threshold * self.rng.uniform(1.3, 3.1)
                    if state == "ALERT"
                    else threshold * self.rng.uniform(0.2, 0.7)
                )
            )
            evidence = {"from": at - window, "to": at}
            if state == "ALERT" and span and span[0] <= at + 600:
                evidence.update(trace_id=span[2], span_id=span[3])
            db.execute(
                "INSERT INTO evaluations(monitor_id,project,timestamp,state,value,evidence) VALUES(%s,%s,%s,%s,%s,%s)",
                (ident, self.p, at, state, value, Jsonb(evidence)),
            )
            last = (state, value, at)
        if last:
            db.execute("UPDATE monitors SET state=%s,value=%s,evaluated_at=%s WHERE id=%s", (*last, ident))

    # ------------------------------------------------------------- dashboards
    def build_dashboards(self):
        boards = [
            (
                "Demo · Checkout em produção",
                [
                    {
                        "type": "otlp",
                        "name": "http.server.request.duration",
                        "title": "Latência HTTP p95 (todos os serviços)",
                    },
                    {"type": "metrics", "name": "shop.checkout.orders", "title": "Pedidos confirmados"},
                    {"type": "metrics", "name": "shop.gateway.requests", "title": "Requisições no gateway"},
                    {"type": "traces", "title": "Operações mais lentas"},
                    {"type": "logs", "title": "Logs recentes"},
                ],
            ),
            (
                "Demo · Incidente em pagamentos",
                [
                    {"type": "metrics", "name": "shop.payments.latency_ms", "title": "Latência de payments (ms)"},
                    {"type": "metrics", "name": "shop.payments.error_rate", "title": "Taxa de erro de payments (%)"},
                    {"type": "otlp", "name": "db.client.connection.count", "title": "Conexões do pool payments-db"},
                    {"type": "otlp", "name": "k8s.container.restarts", "title": "Reinícios de contêiner"},
                    {"type": "traces", "title": "Traces com erro"},
                    {"type": "logs", "title": "Erros e avisos"},
                ],
            ),
            (
                "Demo · Infraestrutura e filas",
                [
                    {"type": "otlp", "name": "system.cpu.utilization", "title": "CPU por pod"},
                    {"type": "otlp", "name": "process.memory.usage", "title": "Memória por pod"},
                    {"type": "otlp", "name": "worker.queue.depth", "title": "Fila orders.created"},
                    {"type": "metrics", "name": "shop.worker.queue_depth", "title": "Fila (métrica legada)"},
                ],
            ),
        ]
        existing = {d["title"] for d in self.store.dashboards(self.p)}
        created = 0
        for title, panels in boards:
            if title not in existing:
                self.store.create_dashboard(self.p, {"title": title, "panels": panels})
                created += 1
        self.counts["dashboards_created"] = created

    def run(self):
        steps = [
            ("traces", self.build_traces),
            ("logs", self.build_logs),
            ("metrics", self.build_metrics),
            ("browser", self.build_browser),
            ("profiles", self.build_profiles),
            ("synthetic", self.build_synthetic),
            ("ai", self.build_ai),
            ("data", self.build_data),
            ("monitors", self.build_monitors),
            ("dashboards", self.build_dashboards),
        ]
        timings = {}
        for name, step in steps:
            began = time.time()
            step()
            timings[name] = round(time.time() - began, 2)
        self.store.evaluate(self.p)
        with self.store.connect() as db:
            states = {
                row["state"]: row["total"]
                for row in db.execute(
                    "SELECT state,count(*) total FROM monitors WHERE project=%s GROUP BY state", (self.p,)
                )
            }
        return {
            "project": self.p,
            "seed_id": self.seed_id,
            "hours": self.hours,
            "synthetic": True,
            "incident_started_at": self.incident,
            "created": self.counts,
            "monitor_states": states,
            "seconds": timings,
            "notes": self.notes,
        }


def seed(store, project="demo", hours=24):
    hours = max(2, min(int(hours), 24 * 7))
    return Story(store, project, hours, uuid.uuid4().hex[:10]).run()


def main():
    parser = argparse.ArgumentParser(description="Popula um projeto com telemetria sintética coerente (demonstração).")
    parser.add_argument("--project", default="demo")
    parser.add_argument("--hours", type=int, default=24)
    args = parser.parse_args()
    from pathlib import Path
    from datalog.app import ROOT, Store

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        for base in (Path.cwd(), ROOT.parent):
            local = base / ".data" / "database-url"
            if local.exists():
                dsn = local.read_text().strip()
                break
    if not dsn:
        raise SystemExit("Configure DATABASE_URL or start python -m datalog.run_local before seeding the demo.")
    store = Store(dsn)
    try:
        with store.connect() as db:
            if not db.execute("SELECT 1 FROM projects WHERE id=%s", (args.project,)).fetchone():
                raise SystemExit(f"Projeto {args.project!r} não existe.")
        print(json.dumps(seed(store, args.project, args.hours), ensure_ascii=False, indent=2))
    finally:
        store.close()


if __name__ == "__main__":
    main()
