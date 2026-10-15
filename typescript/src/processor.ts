import type { Attributes, Context } from "@opentelemetry/api";
import { ExportResultCode, type ExportResult } from "@opentelemetry/core";
import { OTLPTraceExporter } from "@opentelemetry/exporter-trace-otlp-proto";
import { CompressionAlgorithm, OTLPExporterError } from "@opentelemetry/otlp-exporter-base";
import {
  BatchSpanProcessor,
  type ReadableSpan,
  type Span,
  type SpanExporter,
  type SpanProcessor,
} from "@opentelemetry/sdk-trace-base";
import { Artifacts } from "./artifacts.js";
import { reportOnce } from "./diagnostics.js";
import type { ResolvedOptions } from "./init.js";

function exportCopy(span: ReadableSpan, release: string): ReadableSpan {
  const spanContext = span.spanContext();
  const attributes: Attributes = {
    ...span.attributes,
    "convergent.semantic.version": "1",
    "convergent.execution.id": spanContext.traceId,
  };
  if (
    attributes["gen_ai.operation.name"] === "invoke_agent" &&
    attributes["gen_ai.agent.version"] === undefined
  ) {
    attributes["gen_ai.agent.version"] = release;
  }
  return {
    name: span.name,
    kind: span.kind,
    spanContext: () => spanContext,
    ...(span.parentSpanContext ? { parentSpanContext: span.parentSpanContext } : {}),
    startTime: span.startTime,
    endTime: span.endTime,
    status: span.status,
    attributes,
    links: span.links,
    events: span.events,
    duration: span.duration,
    ended: span.ended,
    resource: span.resource,
    instrumentationScope: span.instrumentationScope,
    droppedAttributesCount: span.droppedAttributesCount,
    droppedEventsCount: span.droppedEventsCount,
    droppedLinksCount: span.droppedLinksCount,
  };
}

class ConvergentExporter implements SpanExporter {
  private readonly destination: OTLPTraceExporter;
  private credentialRejected = false;

  constructor(private readonly options: ResolvedOptions) {
    this.destination = new OTLPTraceExporter({
      url: `${options.endpoint}/v1/traces`,
      headers: { Authorization: `Bearer ${options.apiKey}` },
      compression: CompressionAlgorithm.GZIP,
    });
  }

  export(spans: ReadableSpan[], callback: (result: ExportResult) => void): void {
    const failed = (): void => {
      callback({ code: ExportResultCode.FAILED, error: new Error("Convergent export failed.") });
    };
    if (this.credentialRejected) {
      failed();
      return;
    }
    try {
      this.destination.export(
        spans.map((span) => exportCopy(span, this.options.release)),
        (result) => {
          if (result.code === ExportResultCode.SUCCESS) {
            callback(result);
            return;
          }
          if (
            result.error instanceof OTLPExporterError &&
            (result.error.code === 401 || result.error.code === 403)
          ) {
            this.credentialRejected = true;
            reportOnce(
              "credential_rejected",
              "Convergent rejected the API key; network trace exports are disabled for this process.",
            );
          } else {
            reportOnce("export_failed", "Convergent could not export a batch of spans.");
          }
          failed();
        },
      );
    } catch {
      reportOnce("export_failed", "Convergent could not export a batch of spans.");
      failed();
    }
  }

  forceFlush(): Promise<void> {
    return this.destination.forceFlush();
  }

  shutdown(): Promise<void> {
    return this.destination.shutdown();
  }
}

export class ConvergentSpanProcessor implements SpanProcessor {
  private readonly exporter: ConvergentExporter;
  private readonly batch: BatchSpanProcessor;
  readonly artifacts: Artifacts;

  constructor(options: ResolvedOptions) {
    this.exporter = new ConvergentExporter(options);
    this.batch = new BatchSpanProcessor(this.exporter);
    this.artifacts = new Artifacts(options);
  }

  onStart(span: Span, parentContext: Context): void {
    this.batch.onStart(span, parentContext);
  }

  onEnd(span: ReadableSpan): void {
    try {
      this.batch.onEnd(span);
    } catch {
      reportOnce("span_export", "Convergent could not enqueue a completed span.");
    }
    this.artifacts.spanEnded(span);
  }

  async forceFlush(): Promise<void> {
    // Batch flushing alone does not wait for requests already in the exporter.
    try {
      await this.batch.forceFlush();
    } finally {
      await this.exporter.forceFlush();
    }
  }

  shutdown(): Promise<void> {
    this.artifacts.close();
    return this.batch.shutdown();
  }
}
