import assert from "node:assert/strict";
import { trace } from "@opentelemetry/api";
import { resourceFromAttributes } from "@opentelemetry/resources";
import { BatchSpanProcessor, InMemorySpanExporter } from "@opentelemetry/sdk-trace-base";
import { NodeTracerProvider } from "@opentelemetry/sdk-trace-node";
import {
  ConvergentSpanProcessor,
  agent,
  currentSpan,
  currentTrace,
  flush,
  session,
} from "convergent-sdk";

const existingExporter = new InMemorySpanExporter();
const convergentProcessor = new ConvergentSpanProcessor();
const provider = new NodeTracerProvider({
  resource: resourceFromAttributes({ "service.name": "order-support" }),
  spanProcessors: [new BatchSpanProcessor(existingExporter), convergentProcessor],
});
provider.register();

try {
  const applicationTracer = trace.getTracer("order-support");
  const answer = agent({ name: "order_support", model: "scripted-model" }, async (question) => {
    currentSpan().setInput(question);
    const lookup = applicationTracer.startSpan("lookup_order", {
      attributes: { "business.order_id": "1042" },
    });
    lookup.end();
    const response = "Order 1042 has shipped.";
    currentSpan().setOutput(response);
    return { ...currentTrace(), answer: response };
  });

  const result = await session("order-conversation-1042", () => answer("Where is order 1042?"));
  assert.ok(result.traceId, "Set CONVERGENT_API_KEY and CONVERGENT_RELEASE before running.");
  const drained = await flush();
  assert.equal(drained.ok, true, "Convergent did not finish flushing.");
  await provider.forceFlush();

  const spans = existingExporter.getFinishedSpans();
  assert.equal(spans.length, 2);
  const root = spans.find((span) => span.name === "invoke_agent order_support");
  const lookup = spans.find((span) => span.name === "lookup_order");
  assert.equal(root.spanContext().traceId, result.traceId);
  assert.equal(lookup.parentSpanContext.spanId, root.spanContext().spanId);
  assert.equal(root.attributes["gen_ai.request.model"], "scripted-model");
  assert.ok(root.attributes["gen_ai.input.messages"]);
  assert.ok(root.attributes["gen_ai.output.messages"]);
  for (const span of spans) {
    assert.equal(span.attributes["convergent.semantic.version"], undefined);
    assert.equal(span.attributes["convergent.execution.id"], undefined);
    assert.equal(span.attributes["convergent.session.id"], undefined);
  }
  console.log(JSON.stringify({ ...result, existingExporterSpans: spans.length }, null, 2));
} finally {
  await provider.shutdown();
}
