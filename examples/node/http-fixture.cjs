const { NodeTracerProvider } = require('@opentelemetry/sdk-trace-node');
const { SimpleSpanProcessor } = require('@opentelemetry/sdk-trace-base');
const { OTLPTraceExporter } = require('@opentelemetry/exporter-trace-otlp-proto');
const { resourceFromAttributes } = require('@opentelemetry/resources');
const { HttpInstrumentation } = require('@opentelemetry/instrumentation-http');
const provider = new NodeTracerProvider({
  resource: resourceFromAttributes({'service.name':'node-auto','deployment.environment.name':'integration'}),
  spanProcessors: [new SimpleSpanProcessor(new OTLPTraceExporter({url:process.argv[2]}))],
});
provider.register();
const instrumentation = new HttpInstrumentation();
instrumentation.setTracerProvider(provider);
instrumentation.enable();
const http = require('node:http');
const server = http.createServer((_request,response) => response.end('fixture'));
server.listen(0,'127.0.0.1',() => process.stdout.write(String(server.address().port)+'\n'));
process.on('SIGTERM',() => server.close(async () => {
  await provider.shutdown();
  process.exitCode = 0;
}));
