"""Generate and validate a shared Collector config with dedicated project listeners and queues."""

import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from urllib.parse import urlsplit

import yaml

SIGNALS = {"traces": "spans", "logs": "log_records", "metrics": "metric_points"}
ROOT = Path(__file__).resolve().parent


def configuration(projects, queue):
    if not isinstance(projects, dict) or not projects or len(projects) > 32:
        raise ValueError("Configure de 1 a 32 projetos por Collector")
    if not Path(queue).is_absolute() or '$' in queue:
        raise ValueError("Informe um diretório absoluto para as filas")
    template = yaml.safe_load((ROOT / "datalog.yaml").read_text())
    config = {**template, "extensions": {}, "receivers": {}, "exporters": {}}
    config["service"]["extensions"] = []
    config["service"]["pipelines"] = {}
    occupied = set()
    for project, spec in projects.items():
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", project) or not isinstance(spec, dict):
            raise ValueError("Projeto inválido")
        if spec.keys() - {"http", "grpc", "key_env", "transform"}:
            raise ValueError(f"Opções de pipeline desconhecidas em {project}")
        key = spec.get("key_env")
        if not isinstance(key, str) or not re.fullmatch(r"[A-Z_][A-Z_0-9]*", key):
            raise ValueError(f"Variável de chave inválida em {project}")
        protocols = {}
        for protocol in ("http", "grpc"):
            port = spec.get(protocol)
            if port is None:
                continue
            if type(port) is not int or not 1 <= port <= 65535 or port in occupied:
                raise ValueError(f"Porta inválida ou repetida em {project}")
            occupied.add(port)
            protocols[protocol] = {"endpoint": f"127.0.0.1:{port}"}
        if not protocols:
            raise ValueError(f"Configure pelo menos uma porta em {project}")
        receiver = f"otlp/{project}"
        config["receivers"][receiver] = {"protocols": protocols}

        statements = spec.get("transform", {})
        contexts = {"traces": "trace_statements", "logs": "log_statements", "metrics": "metric_statements"}
        if (
            not isinstance(statements, dict)
            or statements.keys() - SIGNALS.keys()
            or any(
                not isinstance(value, list) or any(not isinstance(item, str) for item in value)
                for value in statements.values()
            )
        ):
            raise ValueError(f"Transformações inválidas em {project}")
        processor = f"transform/{project}_policy"
        if statements:
            config["processors"][processor] = {
                "error_mode": "propagate",
                **{contexts[signal]: rules for signal, rules in statements.items()},
            }
        for signal in SIGNALS:
            pipeline = f"{signal}/{project}"
            extension = f"file_storage/{project}_{signal}"
            exporter = f"otlphttp/{project}_{signal}"
            config["extensions"][extension] = {
                "directory": str(Path(queue) / project / signal),
                "create_directory": True,
                "fsync": True,
            }
            config["service"]["extensions"].append(extension)
            outbound = deepcopy(template["exporters"]["otlphttp/datalog"])
            for protocol_signal in SIGNALS:
                outbound.pop(f"{protocol_signal}_endpoint")
            outbound[f"{signal}_endpoint"] = "${env:DATALOG_ENDPOINT}/v1/" + signal + "?project=" + project
            outbound["headers"]["Authorization"] = "Bearer ${env:" + key + "}"
            outbound["sending_queue"]["storage"] = extension
            config["exporters"][exporter] = outbound
            processors = ["memory_limiter"]
            if signal == "logs":
                processors.append("transform/parse_logs")
            if signal in statements:
                processors.append(processor)
            processors.extend(["transform/default_privacy", "batch"])
            config["service"]["pipelines"][pipeline] = {
                "receivers": [receiver],
                "processors": processors,
                "exporters": [exporter],
            }
    return config


def write_validated(projects, queue, collector, output):
    output = Path(output)
    content = json.dumps(configuration(projects, queue), indent=2) + "\n"
    endpoint = urlsplit(os.environ.get("DATALOG_ENDPOINT", ""))
    if (
        endpoint.scheme not in ("http", "https")
        or not endpoint.hostname
        or endpoint.username is not None
        or endpoint.path
        or endpoint.query
        or endpoint.fragment
    ):
        raise ValueError("Configure DATALOG_ENDPOINT com a origem do Datalog")
    for spec in projects.values():
        if not os.environ.get(spec["key_env"]):
            raise ValueError(f'Configure a chave em {spec["key_env"]}')
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", dir=output.parent, delete=False) as file:
        file.write(content)
        staging = Path(file.name)
    try:
        subprocess.run([collector, "validate", f"--config={staging}"], check=True)
        staging.replace(output)
    finally:
        staging.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--projects", required=True, type=Path)
    parser.add_argument("--queue", required=True)
    parser.add_argument("--collector", default="otelcol-contrib")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    write_validated(json.loads(args.projects.read_text()), args.queue, args.collector, args.output)


if __name__ == "__main__":
    main()
