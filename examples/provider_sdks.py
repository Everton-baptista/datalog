"""Run the official Anthropic and Groq clients with fixture or live responses."""

import os
import uuid

import httpx
from anthropic import Anthropic, APIError as AnthropicError, APIStatusError as AnthropicStatusError
from groq import Groq, APIError as GroqError, APIStatusError as GroqStatusError
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from datalog.instrumentation import instrument_ai_sdks


def response(request):
    if b'"failed-fixture"' in request.content:
        return httpx.Response(
            400, json={"error": {"type": "invalid_request_error", "message": "private-provider-error"}}
        )
    if request.url.path == "/v1/messages":
        return httpx.Response(
            200,
            json={
                "id": "msg_fixture",
                "model": "claude-fixture",
                "role": "assistant",
                "type": "message",
                "content": [{"type": "text", "text": "private-provider-output"}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 2, "output_tokens": 3},
            },
        )
    assert request.url.path == "/openai/v1/chat/completions"
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl_fixture",
            "model": "groq-fixture",
            "object": "chat.completion",
            "created": 1,
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "private-provider-output"},
                }
            ],
            "usage": {"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 12},
        },
    )


def exercise(provider):
    capture = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(capture))
    instrument_ai_sdks(provider)
    message = {"role": "user", "content": "private-provider-input"}
    with Anthropic(
        api_key="fixture",
        base_url="https://anthropic.invalid",
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(response)),
    ) as anthropic:
        with Groq(
            api_key="fixture",
            base_url="https://groq.invalid",
            max_retries=0,
            http_client=httpx.Client(transport=httpx.MockTransport(response)),
        ) as groq:
            with provider.get_tracer("native-sdk-fixture").start_as_current_span("agent.sdk.fixture") as root:
                anthropic.messages.create(model="claude-fixture", max_tokens=32, messages=[message])
                groq.chat.completions.create(model="groq-fixture", messages=[message])
                for call in (
                    lambda: anthropic.messages.create(model="failed-fixture", max_tokens=32, messages=[message]),
                    lambda: groq.chat.completions.create(model="failed-fixture", messages=[message]),
                ):
                    try:
                        call()
                    except (AnthropicStatusError, GroqStatusError):
                        pass
                    else:
                        raise AssertionError("O SDK não propagou a falha do provedor")
                trace_id = f"{root.get_span_context().trace_id:032x}"
    serialized = str([span.to_json() for span in capture.get_finished_spans()])
    assert "private-provider-" not in serialized
    spans = capture.get_finished_spans()
    assert len(spans) == 5
    assert sum(span.status.status_code == StatusCode.ERROR for span in spans) == 2
    return trace_id


def exercise_live(provider, anthropic_model, groq_model):
    """Send one synthetic request per provider, with no retry or raw response output."""
    marker = "datalog-live-" + uuid.uuid4().hex
    message = {"role": "user", "content": "Reply only with OK. Test reference: " + marker}
    instrument_ai_sdks(provider)
    with provider.get_tracer("native-sdk-live").start_as_current_span("agent.sdk.live") as root:
        trace_id = f"{root.get_span_context().trace_id:032x}"
        try:
            with Anthropic(
                api_key=os.environ["ANTHROPIC_API_KEY"], base_url="https://api.anthropic.com", max_retries=0, timeout=20
            ) as anthropic:
                reply = anthropic.messages.create(model=anthropic_model, max_tokens=32, messages=[message])
                anthropic_usage = (reply.usage.input_tokens, reply.usage.output_tokens)
        except AnthropicStatusError as exc:
            raise RuntimeError(f"Anthropic retornou HTTP {exc.status_code}") from None
        except (AnthropicError, AttributeError):
            raise RuntimeError("Falha de transporte ou resposta inválida da Anthropic") from None
        try:
            with Groq(
                api_key=os.environ["GROQ_API_KEY"], base_url="https://api.groq.com", max_retries=0, timeout=20
            ) as groq:
                reply = groq.chat.completions.create(model=groq_model, max_completion_tokens=32, messages=[message])
                groq_usage = (reply.usage.prompt_tokens, reply.usage.completion_tokens)
        except GroqStatusError as exc:
            raise RuntimeError(f"Groq retornou HTTP {exc.status_code}") from None
        except (GroqError, AttributeError):
            raise RuntimeError("Falha de transporte ou resposta inválida da Groq") from None
    usage = {"anthropic": anthropic_usage, "groq": groq_usage}
    if any(type(value) is not int or value < 0 for pair in usage.values() for value in pair):
        raise RuntimeError("Um provedor não informou a contagem de tokens esperada")
    return trace_id, usage, marker
