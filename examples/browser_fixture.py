"""Serve an isolated Datalog and an instrumented website for the browser acceptance checks."""

import json
import os
import signal
import threading
import time
import uuid
from http.server import ThreadingHTTPServer
from wsgiref.simple_server import make_server

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceRequest
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.trace import SpanKind
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from datalog.app import Store, make_handler
from datalog.platform.browser_relay import BrowserRelay
from datalog.auth.privacy import Privacy

PAGE = b"""<!doctype html><html lang="pt"><meta charset="utf-8"><title>RUM checkout fixture</title>
<style>body{min-height:180vh}main{background:#b72032;color:white;padding:24px;border-radius:12px;width:320px}</style>
<main id="checkout"><h1>private-heading-fixture</h1><label>Senha<input type="password" id="password"></label>
<button id="buy" data-datalog-public-text>Confirmar pedido</button><output id="trace"></output></main>
<script src="/_datalog/browser.js"></script><script>
window.fixtureRUM = DatalogBrowser({replay:{publicText:true}});
document.querySelector('#buy').onclick = async () => {
  document.querySelector('main').style.backgroundColor = '#137e58';
  const reply = await fixtureRUM.fetch('/checkout');
  const result = await reply.json();
  document.querySelector('#trace').value = result.trace;
  window.dispatchEvent(new ErrorEvent('error',{message:'private-error-fixture'}));
};
</script></html>"""


def run(store):
    user = "fixture-admin"
    store.access.create_user(user, "synthetic fixture password", adopt_existing=True)
    with store.connect() as db:
        db.execute("UPDATE projects SET capture_content=true WHERE id='local'")
    key = store.access.create_key("local", user, "RUM fixture")["token"]
    datalog = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(store))
    origin = f"http://127.0.0.1:{datalog.server_port}"
    provider = TracerProvider(resource=Resource.create({"service.name": "rum-checkout", "host.id": "rum-host"}))
    provider.add_span_processor(
        SimpleSpanProcessor(
            OTLPSpanExporter(endpoint=origin + "/v1/traces?project=local", headers={"Authorization": "Bearer " + key})
        )
    )
    tracer = provider.get_tracer("datalog-browser-fixture")

    def application(environ, start_response):
        if environ["PATH_INFO"] != "/checkout":
            start_response("200 OK", [("Content-Type", "text/html"), ("Content-Length", str(len(PAGE)))])
            return [PAGE]
        context = TraceContextTextMapPropagator().extract({"traceparent": environ.get("HTTP_TRACEPARENT", "")})
        with tracer.start_as_current_span("GET /checkout", context=context, kind=SpanKind.SERVER) as span:
            ids = span.get_span_context()
        metrics = ExportMetricsServiceRequest()
        group = metrics.resource_metrics.add()
        group.resource.attributes.add(key="host.id").value.string_value = "rum-host"
        group.resource.attributes.add(key="service.name").value.string_value = "rum-checkout"
        meter = group.scope_metrics.add().metrics.add(name="system.cpu.utilization")
        now = time.time_ns()
        older = now - 180_000_000_000
        expired = meter.gauge.data_points.add(time_unix_nano=older, as_double=0.2)
        expired.exemplars.add(trace_id=b"\x09" * 16, span_id=b"\x09" * 8, time_unix_nano=older, as_double=0.2)
        meter.gauge.data_points.add(time_unix_nano=now - 90_000_000_000, as_double=0.3)
        point = meter.gauge.data_points.add(time_unix_nano=now, as_double=0.95)
        point.exemplars.add(
            trace_id=ids.trace_id.to_bytes(16, "big"),
            span_id=ids.span_id.to_bytes(8, "big"),
            time_unix_nano=now,
            as_double=0.95,
        )
        with store.connect() as db:
            privacy = Privacy(db.execute("SELECT * FROM projects WHERE id='local'").fetchone())
        store.signals.metrics("local", metrics, privacy)
        payload = json.dumps({"trace": f"{ids.trace_id:032x}", "span": f"{ids.span_id:016x}"}).encode()
        start_response("200 OK", [("Content-Type", "application/json"), ("Content-Length", str(len(payload)))])
        return [payload]

    website = make_server("127.0.0.1", 0, application)
    site = f"http://127.0.0.1:{website.server_port}"
    website.set_app(BrowserRelay(application, origin=site, endpoint=origin, project="local", ingestion_key=key))
    threads = [threading.Thread(target=server.serve_forever, daemon=True) for server in (datalog, website)]
    for thread in threads:
        thread.start()
    print(json.dumps({"datalog": origin, "site": site}), flush=True)
    stopped = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    try:
        stopped.wait()
    finally:
        for server in (website, datalog):
            server.shutdown()
            server.server_close()
        provider.shutdown()
        for thread in threads:
            thread.join()


def main():
    base = os.environ["TEST_DATABASE_URL"]
    schema = "datalog_browser_" + uuid.uuid4().hex
    with psycopg.connect(base) as db:
        db.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    store = Store(make_conninfo(base, options=f"-c search_path={schema}"))
    try:
        run(store)
    finally:
        store.close()
        with psycopg.connect(base) as db:
            db.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


if __name__ == "__main__":
    main()
