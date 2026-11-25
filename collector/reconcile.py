"""Read the Collector and Datalog counters for a project on the shared pipeline."""

import argparse
import json
import os
from urllib.request import Request, urlopen

from prometheus_client.parser import text_string_to_metric_families

from collector.configure import SIGNALS


def scrape(url, token=None):
    request = Request(url, headers={"Authorization": "Bearer " + token} if token else {})
    with urlopen(request, timeout=5) as response:
        maximum = 4 * 1024 * 1024
        document = response.read(maximum + 1)
    if len(document) > maximum:
        raise ValueError("Resposta Prometheus excede 4 MiB")
    return document.decode()


def sample(document):
    return [item for family in text_string_to_metric_families(document) for item in family.samples]


def measure(samples, name, default=None, **labels):
    values = [
        item.value
        for item in samples
        if item.name in (name, name + "_total") and all(item.labels.get(k) == v for k, v in labels.items())
    ]
    return sum(values) if values else default


def snapshot(collector, datalog, project):
    upstream, downstream = sample(collector), sample(datalog)
    rows = {}
    for signal, unit in SIGNALS.items():
        exporter = f"otlphttp/{project}_{signal}"
        receiver = f"otlp/{project}"
        collector_values = {
            "collector_received": measure(upstream, f"otelcol_receiver_accepted_{unit}", 0, receiver=receiver),
            "collector_refused": measure(upstream, f"otelcol_receiver_refused_{unit}", 0, receiver=receiver),
            "collector_sent": measure(upstream, f"otelcol_exporter_sent_{unit}", 0, exporter=exporter),
            "collector_send_failed": measure(upstream, f"otelcol_exporter_send_failed_{unit}", 0, exporter=exporter),
            "collector_enqueue_failed": measure(
                upstream, f"otelcol_exporter_enqueue_failed_{unit}", 0, exporter=exporter
            ),
            "queue_requests": measure(upstream, "otelcol_exporter_queue_size", exporter=exporter),
            "queue_capacity": measure(upstream, "otelcol_exporter_queue_capacity", exporter=exporter),
        }
        rows[signal] = {
            **collector_values,
            **{
                result: measure(
                    downstream, "datalog_otlp_wire_records", 0, project=project, signal=signal, result=result
                )
                for result in ("received", "accepted", "rejected", "duplicate")
            },
            "lag_seconds": measure(downstream, "datalog_otlp_wire_lag_seconds", project=project, signal=signal),
        }
    instance = next((item.labels.get("service_instance_id") for item in upstream if item.name == "target_info"), None)
    return {
        "project": project,
        "collector_instance": instance,
        "collector_uptime": measure(upstream, "otelcol_process_uptime"),
        "datalog_uptime": measure(downstream, "datalog_uptime_seconds"),
        "signals": rows,
    }


GAUGES = {"queue_requests", "queue_capacity", "lag_seconds"}


def reconcile(before, after):
    if before["project"] != after["project"]:
        raise ValueError("As duas capturas precisam ser do mesmo projeto")
    resets = {
        side: before.get(side + "_uptime") is not None
        and after.get(side + "_uptime") is not None
        and after[side + "_uptime"] < before[side + "_uptime"]
        for side in ("collector", "datalog")
    }
    if before["collector_instance"] and after["collector_instance"]:
        resets["collector"] |= before["collector_instance"] != after["collector_instance"]
    result = {}
    for signal, measurements in after["signals"].items():
        previous = before["signals"][signal]
        values = {}
        for name, value in measurements.items():
            upstream = name.startswith("collector_")
            if name in GAUGES:
                values[name] = value
            else:
                reset = resets["collector" if upstream else "datalog"]
                old = previous[name]
                values[name] = value - old if not reset and value >= old else None
        sent, received = values["collector_sent"], values["received"]
        values["sent_minus_received"] = sent - received if sent is not None and received is not None else None
        result[signal] = values
    return {"project": after["project"], "counter_reset": resets, "signals": result}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--collector", default="http://127.0.0.1:8888/metrics")
    parser.add_argument("--datalog", default="http://127.0.0.1:4310/internal/metrics")
    parser.add_argument("--since", type=argparse.FileType())
    args = parser.parse_args()
    current = snapshot(
        scrape(args.collector), scrape(args.datalog, os.environ["DATALOG_INTERNAL_METRICS_TOKEN"]), args.project
    )
    print(json.dumps(reconcile(json.load(args.since), current) if args.since else current, indent=2))


if __name__ == "__main__":
    main()
