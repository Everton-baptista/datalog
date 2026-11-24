'use strict';

const { context, trace, SpanKind, SpanStatusCode, TraceFlags } = require('@opentelemetry/api');
const { OTLPTraceExporter } = require('@opentelemetry/exporter-trace-otlp-proto');
const { resourceFromAttributes } = require('@opentelemetry/resources');
const { NodeTracerProvider, SimpleSpanProcessor } = require('@opentelemetry/sdk-trace-node');

async function main() {
  const [endpoint, traceId, parentId] = process.argv.slice(2);
  const provider = new NodeTracerProvider({
    resource: resourceFromAttributes({'service.name':'payments', 'deployment.environment.name':'integration'}),
    spanProcessors: [new SimpleSpanProcessor(new OTLPTraceExporter({url: endpoint}))],
  });
  const parent = trace.setSpanContext(context.active(), {
    traceId, spanId: parentId, traceFlags: TraceFlags.SAMPLED, isRemote: true,
  });
  const span = provider.getTracer('datalog.node.fixture').startSpan('charge-card', {kind: SpanKind.SERVER}, parent);
  span.setAttribute('http.route', '/charge');
  span.setAttribute('http.response.status_code', 503);
  span.setAttribute('openinference.span.kind', 'TOOL');
  span.setAttribute('gen_ai.input.messages', 'synthetic-private-content');
  span.setStatus({code: SpanStatusCode.ERROR, message: 'synthetic-private-content'});
  await new Promise(resolve => setTimeout(resolve, 15));
  span.end();
  await provider.shutdown();
  process.stdout.write(span.spanContext().spanId + '\n');
}

main().catch(error => { process.stderr.write(error.message + '\n'); process.exitCode = 1; });
