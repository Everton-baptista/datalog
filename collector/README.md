# OpenTelemetry Collector (`collector/`)

Reference OpenTelemetry Collector Contrib (`0.140.1`) configurations and helper CLIs for bridging OTLP/gRPC or OTLP/HTTP into Datalog with persistent disk queues, privacy transforms and tail sampling.

| File | Purpose |
| --- | --- |
| [datalog.yaml](datalog.yaml) | Single-project Collector config with persistent file queue, memory limiter, default privacy filter and OTLP/HTTP export to Datalog. |
| [tail-sampling.yaml](tail-sampling.yaml) | Tail-sampling Collector config retaining errors and slow traces (`>= 1s`) while sampling normal traffic. |
| [trace-router.yaml](trace-router.yaml) | Load-balancing Trace ID router to keep all spans of a trace on the same tail-sampling instance. |
| [configure.py](configure.py) | Generates and validates multi-project Collector configs (`python -m collector.configure --projects ... --queue ... --output ...`). |
| [reconcile.py](reconcile.py) | Reconciles Collector `/metrics` against Datalog `/internal/metrics` (`python -m collector.reconcile --project local`). |

Install optional dependencies with `python -m pip install -e ".[collector]"`. See [docs/features.md](../docs/features.md#2-coleta-busca-e-apm) for full usage.
