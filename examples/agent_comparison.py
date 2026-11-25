"""Seed a public synthetic comparison in the demo project using the operator's database."""

import os
import time
import uuid

from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

from datalog.app import Store
from datalog.auth.privacy import Privacy


def seed_comparison(store):
    project = "demo"
    dataset_name = "demo/pagamentos"
    prompt_name = "demo/verificar-pagamento"
    criterion = "correcao"
    cases = [
        {"id": "declined", "input": "Cartão recusado na simulação"},
        {"id": "timeout", "input": "Serviço de pagamento expirou na simulação"},
    ]
    prompts = store.ai.prompts(project)["items"]
    prompt = next((p for p in prompts if p["name"] == prompt_name), None)
    if prompt is None:
        prompt = store.ai.create_prompt(
            project, {"name": prompt_name, "content": "Classifique a falha sintética de pagamento."}
        )
    datasets = store.agents.datasets(project)["items"]
    dataset = next((d for d in datasets if d["name"] == dataset_name), None)
    if dataset is None:
        dataset = store.agents.create_dataset(project, {"name": dataset_name, "cases": cases})
    request = ExportTraceServiceRequest()
    resources = request.resource_spans.add()
    resources.resource.attributes.add(key="service.name").value.string_value = "payment-agent"
    resources.resource.attributes.add(key="deployment.environment.name").value.string_value = "demo"
    scope = resources.scope_spans.add()
    now = time.time_ns() - 500_000_000
    evaluations = []
    for index, version in enumerate(("A", "B")):
        for j, case in enumerate(cases):
            trace_id = uuid.uuid4().bytes
            span_id = uuid.uuid4().bytes[:8]
            start = now + (index * 2 + j) * 100_000_000
            span = scope.spans.add(
                trace_id=trace_id,
                span_id=span_id,
                name=f"Classificar {case['id']}",
                start_time_unix_nano=start,
                end_time_unix_nano=start + 80_000_000,
            )
            span.status.code = 2 if version == "A" and j == 1 else 1
            if version == "A":
                span.attributes.add(key="gen_ai.operation.name").value.string_value = "chat"
                span.attributes.add(key="gen_ai.response.model").value.string_value = "modelo-demo-a"
            else:
                span.attributes.add(key="openinference.span.kind").value.string_value = "LLM"
                span.attributes.add(key="llm.model_name").value.string_value = "modelo-demo-b"
                span.attributes.add(key="llm.token_count.prompt").value.int_value = 124
                span.attributes.add(key="datalog.recovered").value.bool_value = j == 1
            span.attributes.add(key="datalog.execution.version").value.string_value = version
            evaluations.append(
                {
                    "dataset": dataset_name,
                    "dataset_version": dataset["version"],
                    "case_id": case["id"],
                    "trace_id": trace_id.hex(),
                    "span_id": span_id.hex(),
                    "execution_version": version,
                    "criterion": criterion,
                    "score": ((0.65, 0.35), (0.95, 0.85))[index][j],
                    "prompt_name": prompt_name,
                    "prompt_version": prompt["version"],
                }
            )
    store.tracing.ingest(project, request, Privacy({"capture_content": False, "allowed_attributes": []}))
    for evaluation in evaluations:
        store.agents.score(project, evaluation)
    return {"dataset": dataset_name, "version": dataset["version"], "criterion": criterion, "before": "A", "after": "B"}


if __name__ == "__main__":
    store = Store(os.environ["DATABASE_URL"])
    try:
        print(seed_comparison(store))
    finally:
        store.close()
