import { Observability } from "@mastra/observability";
import { OtelBridge } from "@mastra/otel-bridge";
import { flush, init, tracerProvider } from "convergent-sdk";
import { runOrderSupport } from "./agent.mjs";

const status = init();
if (!status.enabled) throw new Error(`Tracing could not start: ${status.reason}`);

const observability = new Observability({
  configs: {
    default: {
      serviceName: "convergent-mastra-bridge",
      bridge: new OtelBridge({ tracerProvider: tracerProvider() }),
    },
  },
});

try {
  await runOrderSupport(observability);
} finally {
  await observability.shutdown();
  const drained = await flush();
  if (!drained.ok) {
    console.error("Telemetry did not finish flushing.");
    process.exitCode = 1;
  }
}
