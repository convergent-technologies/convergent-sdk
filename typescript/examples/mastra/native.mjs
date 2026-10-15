import { Observability } from "@mastra/observability";
import { OtelExporter } from "@mastra/otel-exporter";
import { runOrderSupport } from "./agent.mjs";

const apiKey = process.env.CONVERGENT_API_KEY;
if (!apiKey?.trim() || /[\r\n]/u.test(apiKey)) {
  throw new Error("Set CONVERGENT_API_KEY to a nonblank key without line breaks.");
}
const endpoint = (process.env.CONVERGENT_ENDPOINT ?? "https://ingest.convergent.dev").replace(
  /\/+$/u,
  "",
);
const release = process.env.CONVERGENT_RELEASE?.trim();
if (!release) throw new Error("Set CONVERGENT_RELEASE before running the example.");
const observability = new Observability({
  configs: {
    default: {
      serviceName: "convergent-mastra-native",
      exporters: [
        new OtelExporter({
          logLevel: "debug",
          provider: {
            custom: {
              endpoint: `${endpoint}/v1/traces`,
              protocol: "http/protobuf",
              headers: { Authorization: `Bearer ${apiKey.trim()}` },
            },
          },
          resourceAttributes: { "service.version": release },
          signals: { traces: true, logs: false },
        }),
      ],
    },
  },
});

try {
  await runOrderSupport(observability);
} finally {
  await observability.shutdown();
}
