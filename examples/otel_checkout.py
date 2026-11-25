"""Send a synthetic checkout trace and a correlated log using the official SDK."""

import argparse
import json
import os
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from opentelemetry.exporter.otlp.proto.http import Compression
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.trace import SpanKind, Status, StatusCode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:4310")
    parser.add_argument("--project", default="local")
    args = parser.parse_args()
    base = args.base_url.rstrip("/")
    query = urlencode({"project": args.project})
    key = os.environ["DATALOG_INGESTION_KEY"]
    headers = {"Authorization": f"Bearer {key}"}
    providers = []

    def tracer(service):
        provider = TracerProvider(
            resource=Resource.create({"service.name": service, "deployment.environment.name": "dev"})
        )
        providers.append(provider)
        provider.add_span_processor(
            SimpleSpanProcessor(
                OTLPSpanExporter(
                    endpoint=f"{base}/v1/traces?{query}", headers=headers, compression=Compression.Gzip, timeout=5
                )
            )
        )
        return provider.get_tracer("datalog.checkout.example")

    try:
        checkout, payments = tracer("checkout"), tracer("payments")
        with checkout.start_as_current_span("POST /checkout", kind=SpanKind.SERVER) as root:
            trace_id = f"{root.get_span_context().trace_id:032x}"
            time.sleep(0.025)
            with payments.start_as_current_span("charge-card", kind=SpanKind.CLIENT) as charge:
                time.sleep(0.06)
                charge.set_attribute("http.response.status_code", 503)
                charge.add_event("payment.timeout", {"retryable": True})
                charge.set_status(Status(StatusCode.ERROR, "Payment service timed out"))
            root.set_status(Status(StatusCode.ERROR, "Checkout returned 503"))
        log = {
            "service": "payments",
            "env": "dev",
            "level": "error",
            "trace_id": trace_id,
            "message": "[EXEMPLO] Tempo limite ao processar pagamento",
        }
        request = Request(
            f"{base}/api/logs?{query}",
            data=json.dumps(log).encode(),
            headers={"Content-Type": "application/json", **headers},
        )
        with urlopen(request, timeout=5):
            pass
        print(f"Projeto: {args.project} · Abra {base}/#traces?id={trace_id}")
    finally:
        for provider in providers:
            provider.shutdown()


if __name__ == "__main__":
    main()
