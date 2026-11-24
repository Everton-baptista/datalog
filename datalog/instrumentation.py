"""Provider-neutral GenAI and OpenInference instrumentation for an OpenTelemetry tracer."""

from contextlib import contextmanager
import time

from opentelemetry.trace import SpanKind, Status


def instrument_ai_sdks(tracer_provider, *, capture_content=False):
    """Enable the supported native Python integrations once at process startup."""
    from openinference.instrumentation import TraceConfig
    from openinference.instrumentation.anthropic import AnthropicInstrumentor
    from openinference.instrumentation.groq import GroqInstrumentor
    from wrapt import ObjectProxy

    class PrivateSpan(ObjectProxy):
        def record_exception(self, exception, *args, **kwargs):
            self.__wrapped__.set_attribute("error.type", type(exception).__name__)

        def set_status(self, status, description=None):
            self.__wrapped__.set_status(status.status_code if isinstance(status, Status) else status)

    # OpenInference dispatches through the tracer's concrete class.
    class PrivateTracer:
        def __init__(self, tracer, name):
            self._delegate = tracer
            self._provider = name.rsplit(".", 1)[-1]

        def start_span(self, *args, **kwargs):
            kwargs.update(record_exception=False, set_status_on_exception=False)
            span = self._delegate.start_span(*args, **kwargs)
            span.set_attribute("llm.provider", self._provider)
            return PrivateSpan(span)

    class PrivateProvider:
        def get_tracer(self, name, *args, **kwargs):
            return PrivateTracer(tracer_provider.get_tracer(name, *args, **kwargs), name)

    config = TraceConfig(
        hide_inputs=not capture_content,
        hide_outputs=not capture_content,
        hide_llm_invocation_parameters=True,
        hide_llm_tools=True,
    )
    provider = PrivateProvider()
    for instrumentor in (AnthropicInstrumentor(), GroqInstrumentor()):
        instrumentor.instrument(tracer_provider=provider, config=config)


class AIInvocation:
    def __init__(self, span, convention, streaming):
        self.span = span
        self.convention = convention
        self.streaming = streaming
        self.started = time.perf_counter_ns()
        self.first = False
        self.span.set_attribute("datalog.gen_ai.streaming", streaming)

    def token(self):
        """Call when the first nonempty token or content chunk reaches the caller."""
        if self.streaming and not self.first:
            self.first = True
            self.span.set_attribute("datalog.gen_ai.ttft_ms", (time.perf_counter_ns() - self.started) / 1e6)
            self.span.set_attribute("datalog.gen_ai.first_token_ns", str(time.time_ns()))

    def usage(self, input_tokens=None, output_tokens=None, cost_usd=None):
        keys = (
            ("gen_ai.usage.input_tokens", "gen_ai.usage.output_tokens")
            if self.convention == "gen-ai"
            else ("llm.token_count.prompt", "llm.token_count.completion")
        )
        for key, value in zip(keys, (input_tokens, output_tokens)):
            if type(value) is int and 0 <= value <= 2**53 - 1:
                self.span.set_attribute(key, value)
        if cost_usd is not None:
            self.span.set_attribute("datalog.cost.usd", cost_usd)
            self.span.set_attribute("datalog.cost.source", "reported")


@contextmanager
def invocation(tracer, provider, model, *, convention="gen-ai", streaming=False, execution_version=None):
    """Observe a provider call without taking ownership of its input or output."""
    if convention == "gen-ai":
        attributes = {"gen_ai.operation.name": "chat", "gen_ai.provider.name": provider, "gen_ai.request.model": model}
    elif convention == "openinference":
        attributes = {"openinference.span.kind": "LLM", "llm.provider": provider, "llm.model_name": model}
    else:
        raise ValueError("Unknown telemetry convention")
    if execution_version is not None:
        attributes["datalog.execution.version"] = execution_version
    with tracer.start_as_current_span(
        "gen_ai.chat", kind=SpanKind.CLIENT, attributes=attributes, record_exception=False, set_status_on_exception=True
    ) as span:
        yield AIInvocation(span, convention, streaming)
