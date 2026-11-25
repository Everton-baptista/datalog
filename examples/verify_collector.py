"""Verify two SDKs, three OTLP signals and durable Collector restart against a disposable schema."""

import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import uuid

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
from opentelemetry._logs import SeverityNumber
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import SimpleLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags, set_span_in_context

from datalog.app import Store, make_handler

ROOT = Path(__file__).resolve().parents[1]


def port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def wait_until(predicate, description, seconds=25):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if value := predicate():
            return value
        time.sleep(0.12)
    raise RuntimeError(f"Prazo excedido: {description}")


def listening(number):
    try:
        with socket.create_connection(("127.0.0.1", number), timeout=0.3):
            return True
    except OSError:
        return False


def resource(service):
    return Resource.create({"service.name": service, "deployment.environment.name": "integration"})


def trace_provider(endpoint, service):
    provider = TracerProvider(resource=resource(service))
    provider.add_span_processor(SimpleSpanProcessor(OTLPSpanExporter(endpoint=endpoint + "/v1/traces", timeout=3)))
    return provider


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--collector", required=True)
    parser.add_argument("--auto", action="store_true", help="Também verificar instrumentação automática HTTP e SQLite")
    parser.add_argument(
        "--sampling", action="store_true", help="Também verificar distribuição de traces por dois amostradores"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--ai-providers", action="store_true", help="Verificar os SDKs Anthropic e Groq com HTTP simulado"
    )
    mode.add_argument("--live-ai-providers", action="store_true", help="Fazer uma chamada real a cada provedor")
    parser.add_argument("--anthropic-model", help="Modelo Anthropic disponível na conta")
    parser.add_argument("--groq-model", help="Modelo Groq disponível na conta")
    args = parser.parse_args()
    live_models = None
    if args.live_ai_providers:
        if not args.anthropic_model or not args.groq_model:
            parser.error("--live-ai-providers exige --anthropic-model e --groq-model")
        missing = [name for name in ("ANTHROPIC_API_KEY", "GROQ_API_KEY") if not os.environ.get(name, "").strip()]
        if missing:
            parser.error("Variáveis obrigatórias ausentes: " + ", ".join(missing))
        live_models = (args.anthropic_model, args.groq_model)
    dsn = os.environ["TEST_DATABASE_URL"]
    schema = "datalog_collector_" + uuid.uuid4().hex
    with psycopg.connect(dsn) as db:
        db.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    store = Store(make_conninfo(dsn, options=f"-c search_path={schema}"))
    try:
        with tempfile.TemporaryDirectory(prefix="datalog-collector-") as work:
            result = verify(
                store,
                Path(work),
                args.collector,
                auto=args.auto,
                sampling=args.sampling,
                ai_providers=args.ai_providers,
                live_models=live_models,
            )
            print(json.dumps(result, indent=2))
    finally:
        store.close()
        with psycopg.connect(dsn) as db:
            db.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def auto_instrumented(endpoint):
    import sqlite3
    from datalog.instrumentation import invocation
    from opentelemetry.instrumentation.sqlite3 import SQLite3Instrumentor
    from opentelemetry.instrumentation.urllib import URLLibInstrumentor

    tracer = trace_provider(endpoint, "python-auto")
    http, sqlite = URLLibInstrumentor(), SQLite3Instrumentor()
    http.instrument(tracer_provider=tracer)
    sqlite.instrument(tracer_provider=tracer)
    child = subprocess.Popen(
        ["node", str(ROOT / "examples/node/http-fixture.cjs"), endpoint + "/v1/traces"],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        import selectors

        with selectors.DefaultSelector() as polling:
            polling.register(child.stdout, selectors.EVENT_READ)
            if not polling.select(timeout=10):
                raise RuntimeError("O serviço Node não iniciou")
            remote_port = int(child.stdout.readline())
        with tracer.get_tracer("fixture").start_as_current_span("auto-instrumentation") as root:
            trace_id = f"{root.get_span_context().trace_id:032x}"
            with urlopen(f"http://127.0.0.1:{remote_port}/checkout", timeout=4) as response:
                assert response.read() == b"fixture"
            db = sqlite3.connect(":memory:")
            try:
                assert db.cursor().execute("SELECT 42").fetchone() == (42,)
            finally:
                db.close()
            with invocation(
                tracer.get_tracer("fixture"), "synthetic", "stream", streaming=True, execution_version="auto-v1"
            ) as call:
                call.token()
                call.usage(input_tokens=0, output_tokens=1)
        return trace_id
    finally:
        child.terminate()
        child.wait(timeout=10)
        tracer.shutdown()
        sqlite.uninstrument()
        http.uninstrument()


def verify_sampling(collector, work, env, span_count):
    from opentelemetry.trace import Status, StatusCode

    children = []
    ingress = port()
    sampler_ports = [port(), port()]

    def start(name, configurations, variables):
        output = work / (name + ".log")
        with output.open("wb") as log:
            proc = subprocess.Popen(
                [collector, *[f"--config={ROOT}/collector/{file}" for file in configurations]],
                env={**env, **variables},
                stdout=log,
                stderr=log,
            )
        children.append(proc)

    try:
        for i, tcp in enumerate(sampler_ports):
            start(
                f"sampler-{i}",
                ("datalog.yaml", "tail-sampling.yaml"),
                {
                    "DATALOG_COLLECTOR_GRPC": f"127.0.0.1:{tcp}",
                    "DATALOG_COLLECTOR_HTTP": f"127.0.0.1:{port()}",
                    "DATALOG_COLLECTOR_METRICS_PORT": str(port()),
                    "DATALOG_COLLECTOR_QUEUE": str(work / f"sample-queue-{i}"),
                },
            )
            wait_until(lambda: listening(tcp), f"Collector sampler {i}")  # noqa: B023 (called within this iteration)
        start(
            "trace-router",
            ("trace-router.yaml",),
            {
                "DATALOG_ROUTER_HTTP": f"127.0.0.1:{ingress}",
                "DATALOG_ROUTER_GRPC": f"127.0.0.1:{port()}",
                "DATALOG_SAMPLER_A": f"127.0.0.1:{sampler_ports[0]}",
                "DATALOG_SAMPLER_B": f"127.0.0.1:{sampler_ports[1]}",
            },
        )
        wait_until(lambda: listening(ingress), "roteador por Trace ID")
        provider = trace_provider(f"http://127.0.0.1:{ingress}", "sample-fixture")
        tracer = provider.get_tracer("datalog.tail.verify")
        required, ordinary = [], []
        sent = time.monotonic()
        for i in range(32):
            when = time.time_ns() - (1_500_000_000 if i == 1 else 0)
            with tracer.start_as_current_span(f"sample-{i}", start_time=when) as parent:
                with tracer.start_as_current_span("child") as child:
                    if i == 0:
                        child.set_status(Status(StatusCode.ERROR))
                trace_id = f"{parent.get_span_context().trace_id:032x}"
                (required if i < 2 else ordinary).append(trace_id)
        provider.shutdown()
        wait_until(lambda: all(span_count(item) == 2 for item in required), "trace de erro e trace lento completos")
        elapsed = time.monotonic() - sent
        fast = sum(span_count(item) == 2 for item in ordinary)
        assert all(span_count(item) in (0, 2) for item in ordinary)
        return {
            "samplers": 2,
            "error_and_slow_complete": 2,
            "other_retained": fast,
            "other_sent": len(ordinary),
            "decision_seconds": round(elapsed, 2),
        }
    finally:
        for proc in reversed(children):
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()


def verify(store, work, collector, auto=False, sampling=False, ai_providers=False, live_models=None):
    user, project = "collector-verifier", "pipeline"
    password = uuid.uuid4().hex
    store.access.create_user(user, password)
    store.access.create_project(user, project)
    key = store.access.create_key(project, user, "Test Collector")["token"]
    session = store.access.login(user, password)
    http_port, grpc_port, backend_port = port(), port(), port()
    endpoint, backend = f"http://127.0.0.1:{http_port}", f"http://127.0.0.1:{backend_port}"

    def api(path, query=None, body=None):
        request = Request(
            f"{backend}/api/{path}?" + urlencode({"project": project, **(query or {})}),
            headers={
                "Cookie": "datalog_session=" + session,
                "X-Datalog-Request": "1",
                "Content-Type": "application/json",
            },
            data=json.dumps(body).encode() if body is not None else None,
        )
        with urlopen(request, timeout=5) as response:
            return json.load(response)

    def start_backend():
        server = ThreadingHTTPServer(("127.0.0.1", backend_port), make_handler(store))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server

    env = {
        **os.environ,
        "DATALOG_PROJECT": project,
        "DATALOG_ENDPOINT": backend,
        "DATALOG_INGESTION_KEY": key,
        "DATALOG_COLLECTOR_HTTP": f"127.0.0.1:{http_port}",
        "DATALOG_COLLECTOR_GRPC": f"127.0.0.1:{grpc_port}",
        "DATALOG_COLLECTOR_QUEUE": str(work / "queue"),
        "DATALOG_COLLECTOR_METRICS_PORT": str(port()),
    }
    for credential in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "GROQ_API_KEY"):
        env.pop(credential, None)

    def start_collector(output):
        with output.open("wb") as log:
            proc = subprocess.Popen(
                [collector, f"--config={ROOT}/collector/datalog.yaml"], env=env, stdout=log, stderr=log
            )
        try:
            wait_until(lambda: listening(http_port), "Collector HTTP")
        except RuntimeError:
            proc.kill()
            proc.wait()
            raise RuntimeError(output.read_text()[-4000:]) from None
        return proc

    def stop_collector(proc, abrupt=False):
        if proc.poll() is None:
            proc.kill() if abrupt else proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()

    def stop_backend(server):
        server.shutdown()
        server.server_close()

    def span_count(ident):
        with store.connect() as db:
            return db.execute(
                "SELECT count(*) n FROM otlp_spans WHERE project=%s AND trace_id=%s", (project, ident)
            ).fetchone()["n"]

    first_log = work / "collector-before.log"
    server = start_backend()
    process = None
    try:
        process = start_collector(first_log)
        checkout = trace_provider(endpoint, "checkout")
        inventory = trace_provider(endpoint, "inventory")
        with checkout.get_tracer("fixture").start_as_current_span("POST /checkout") as root:
            root.set_attribute("gen_ai.operation.name", "invoke_agent")
            context = root.get_span_context()
            trace_id, root_id = f"{context.trace_id:032x}", f"{context.span_id:016x}"
            with inventory.get_tracer("fixture").start_as_current_span("reserve-stock"):
                child_id = subprocess.check_output(
                    ["node", str(ROOT / "examples/node/payments.cjs"), endpoint + "/v1/traces", trace_id, root_id],
                    text=True,
                    timeout=20,
                ).strip()
            wait_until(lambda: span_count(trace_id) == 2, "spans filhos antes da raiz")
            children = api("traces", {"attribute": "http.route", "value": "/charge"})
            assert children["items"][0]["has_root"] is False
        checkout.shutdown()
        inventory.shutdown()
        wait_until(lambda: span_count(trace_id) == 3, "raiz tardia")

        context = set_span_in_context(
            NonRecordingSpan(
                SpanContext(
                    trace_id=int(trace_id, 16),
                    span_id=int(child_id, 16),
                    is_remote=True,
                    trace_flags=TraceFlags.SAMPLED,
                )
            )
        )
        logger = LoggerProvider(resource=resource("payments"))
        logger.add_log_record_processor(
            SimpleLogRecordProcessor(OTLPLogExporter(endpoint=endpoint + "/v1/logs", timeout=3))
        )
        logger.get_logger("fixture").emit(
            timestamp=time.time_ns(),
            context=context,
            severity_number=SeverityNumber.ERROR,
            body=json.dumps({"error.type": "fixture.network", "api_key": "synthetic-private-content"}),
            attributes={"log.record.uid": "collector-fixture"},
        )
        logger.shutdown()

        reader = PeriodicExportingMetricReader(
            OTLPMetricExporter(endpoint=endpoint + "/v1/metrics", timeout=3), export_interval_millis=60000
        )
        meter = MeterProvider(resource=resource("payments"), metric_readers=[reader])
        instrument = meter.get_meter("fixture")
        instrument.create_counter("payments.requests").add(1)
        instrument.create_histogram("payments.duration", unit="ms").record(85)
        meter.force_flush()
        meter.shutdown()
        wait_until(lambda: api("trace/span", {"id": trace_id, "span_id": child_id})["logs"], "log ligado ao span")
        points = wait_until(lambda: api("otlp/metrics")["items"], "métricas OTLP")
        assert {m["kind"] for m in points} == {"sum", "histogram"}
        assert any(m["monotonic"] is True and m["temporality"] == 2 for m in points)
        span = api("trace/span", {"id": trace_id, "span_id": child_id})
        assert "synthetic-private-content" not in json.dumps(span)
        assert api("logs", {"trace_id": trace_id, "attrs": json.dumps({"error.type": "fixture.network"})})["total"] == 1
        edge = api("services")["edges"]
        assert {e["target"] for e in edge if e["source"] == "checkout"} == {"payments", "inventory"}
        monitor = api(
            "monitors",
            body={
                "title": "Pagamentos com erro",
                "service": "payments",
                "metric": "@otlp.span.errors",
                "threshold": 0,
                "window": 300,
            },
        )
        assert monitor["state"] == "ALERT" and monitor["span_id"] == child_id
        projection = api("agents/otlp", {"id": trace_id})["items"]
        assert {"gen-ai", "openinference"} <= {item["convention"] for item in projection}

        if auto:
            auto_trace = auto_instrumented(endpoint)
            automatic = wait_until(
                lambda: api("trace", {"id": auto_trace}) if span_count(auto_trace) >= 5 else None,
                "HTTP Python + HTTP Node + SQLite + streaming no mesmo trace",
            )
            assert {s["service"] for s in automatic["spans"]} == {"python-auto", "node-auto"}
            assert any(s["service"] == "node-auto" and s["kind"] == 2 for s in automatic["spans"])
            ai = api("agents/otlp", {"id": auto_trace})["items"][0]
            assert ai["streaming"] and ai["ttft_ms"] >= 0 and int(ai["first_token_ns"]) > 0
            assert ai["input_tokens"] == 0 and ai["execution_version"] == "auto-v1"

        if ai_providers:
            from examples.provider_sdks import exercise

            sdk_provider = trace_provider(endpoint, "native-ai-providers")
            sdk_trace = exercise(sdk_provider)
            sdk_provider.shutdown()
            wait_until(lambda: span_count(sdk_trace) == 5, "Anthropic e Groq no mesmo trace")
            native = api("agents/otlp", {"id": sdk_trace})["items"]
            assert {
                (i["provider"], i["model"], i["input_tokens"], i["output_tokens"]) for i in native if not i["error"]
            } == {
                ("anthropic", "claude-fixture", 2, 3),
                ("groq", "groq-fixture", 5, 7),
            }
            assert {i["provider"] for i in native if i["error"]} == {"anthropic", "groq"}
            assert all(i["convention"] == "openinference" for i in native)
            assert "private-provider-" not in json.dumps(api("trace", {"id": sdk_trace}))

        if live_models:
            from examples.provider_sdks import exercise_live

            sdk_provider = trace_provider(endpoint, "native-ai-providers-live")
            try:
                sdk_trace, usage, marker = exercise_live(sdk_provider, *live_models)
            finally:
                sdk_provider.shutdown()
            wait_until(lambda: span_count(sdk_trace) >= 3, "spans reais da Anthropic e da Groq")
            native = api("agents/otlp", {"id": sdk_trace})["items"]
            assert len(native) == 2 and all(not i["error"] and i["model"] for i in native), (
                "Spans de modelo incompletos"
            )
            assert {i["provider"]: (i["input_tokens"], i["output_tokens"]) for i in native} == usage
            detail = api("trace", {"id": sdk_trace})
            (root,) = [span for span in detail["spans"] if span["parent_span_id"] is None]
            assert all(span["parent_span_id"] == root["span_id"] for span in detail["spans"] if span != root)
            payloads = [
                api("trace/span", {"id": sdk_trace, "span_id": span["span_id"]})["payload"] for span in detail["spans"]
            ]
            assert marker not in json.dumps(payloads)
            private_prefixes = (
                "input.value",
                "output.value",
                "llm.input_messages",
                "llm.output_messages",
                "gen_ai.input.messages",
                "gen_ai.output.messages",
            )
            assert not any(
                attr["key"].startswith(private_prefixes)
                for payload in payloads
                for attr in payload.get("attributes", [])
            ), "Conteúdo capturado indevidamente"

        stop_backend(server)
        server = None
        recovery = trace_provider(endpoint, "recovery-fixture")
        with recovery.get_tracer("fixture").start_as_current_span("queued-while-offline") as queued:
            recovered_trace = f"{queued.get_span_context().trace_id:032x}"
            queued.set_attribute("gen_ai.input.messages", "synthetic-secret-before-queue")
        recovery.shutdown()
        wait_until(lambda: "Will retry" in first_log.read_text(errors="replace"), "tentativa com destino indisponível")

        def collector_queue_observable():
            with urlopen("http://127.0.0.1:" + env["DATALOG_COLLECTOR_METRICS_PORT"] + "/metrics", timeout=2) as reply:
                return b"otelcol_exporter_queue_size" in reply.read()

        wait_until(collector_queue_observable, "métricas da fila do Collector")
        queue_data = b"".join(file.read_bytes() for file in (work / "queue").iterdir() if file.is_file())
        assert b"queued-while-offline" in queue_data
        assert b"synthetic-secret-before-queue" not in queue_data
        assert span_count(recovered_trace) == 0
        stop_collector(process, abrupt=True)
        server = start_backend()
        process = start_collector(work / "collector-after.log")
        wait_until(lambda: span_count(recovered_trace) == 1, "replay da fila após reinício")
        sampled = verify_sampling(collector, work, env, span_count) if sampling else None
        return {
            "sdk": ["Python 1.39.1", "JavaScript (package-lock.json)"],
            "collector": "0.140.1",
            "late_parent": True,
            "signals": ["traces", "logs", "metrics"],
            "monitor": "ALERT",
            "alert_span": child_id,
            "privacy_default": True,
            "redaction_before_queue": True,
            "persistent_queue_recovered": True,
            "recovered_span_count": span_count(recovered_trace),
            "auto_http_and_sqlite": auto,
            "tail_sampling": sampled,
            "native_ai_sdks": ["Anthropic", "Groq"] if ai_providers or live_models else [],
            "provider_validation": "live" if live_models else "simulated_http" if ai_providers else None,
        }
    finally:
        if process is not None:
            stop_collector(process)
        if server is not None:
            stop_backend(server)


if __name__ == "__main__":
    main()
