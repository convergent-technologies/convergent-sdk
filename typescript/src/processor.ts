import {
  isSpanContextValid,
  ProxyTracerProvider,
  ROOT_CONTEXT,
  trace,
  type Attributes,
  type Context,
  type TracerProvider,
} from "@opentelemetry/api";
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
import {
  activeConfiguration,
  createTracer,
  resolveOptions,
  setConfiguration,
  type Credentials,
  type ProcessorOptions,
  type ResolvedOptions,
} from "./configuration.js";
import { destinationName, File } from "./destinations.js";
import { reportOnce } from "./diagnostics.js";
import { setupFailure } from "./failures.js";
import { drainBeforeExit } from "./lifecycle.js";
import { createLocalExporter } from "./local-export.js";
import {
  agentDecision,
  applyUpdate,
  exportMarks,
  inheritedMarks,
  markAttributes,
  passesAttributeRules,
  spanMarks,
  type Filters,
  type Marks,
  type SpanMetadata,
} from "./filters.js";
import { sessionContextKey } from "./session.js";

function exportCopy(span: ReadableSpan, release: string, metadata?: SpanMetadata): ReadableSpan {
  const spanContext = span.spanContext();
  const attributes: Attributes = {
    ...span.attributes,
    ...exportMarks(spanMarks(span.attributes, metadata)),
  };
  const operation = metadata?.operation;
  const sessionId = metadata?.sessionId;
  // Only a span that declares a GenAI operation at start is agent work.
  if (operation) {
    attributes["convergent.semantic.version"] = "1";
    attributes["convergent.execution.id"] = spanContext.traceId;
  }
  if (sessionId !== undefined && typeof attributes["convergent.session.id"] !== "string") {
    attributes["convergent.session.id"] = sessionId;
  }
  if (operation === "invoke_agent" && attributes["gen_ai.agent.version"] === undefined) {
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

const headerVariables = ["OTEL_EXPORTER_OTLP_HEADERS", "OTEL_EXPORTER_OTLP_TRACES_HEADERS"];

function networkExporter({ apiKey, endpoint }: Credentials): SpanExporter {
  // OpenTelemetry adds these headers to Convergent requests too. Name the
  // variables and never their values. The headers themselves stay as configured.
  const configured = headerVariables.filter((name) => process.env[name]?.trim());
  if (configured.length) {
    reportOnce(
      "otlp_headers",
      `${configured.join(" and ")} ${configured.length > 1 ? "are" : "is"} set. OpenTelemetry adds the headers in OTEL_EXPORTER_OTLP_HEADERS and OTEL_EXPORTER_OTLP_TRACES_HEADERS to every Convergent export request as well, so headers meant for another collector or proxy also reach Convergent. The Convergent API key still takes precedence over an authorization entry. Their values are not shown.`,
    );
  }
  return new OTLPTraceExporter({
    url: `${endpoint}/v1/traces`,
    headers: { Authorization: `Bearer ${apiKey}` },
    compression: CompressionAlgorithm.GZIP,
  });
}

/**
 * Applies the attribute rules and Convergent's export copy in front of one
 * destination's exporter, and turns its failures into safe, once-only
 * diagnostics. The rules run here, after the batch processor resolved async
 * resource attributes.
 */
class ConvergentExporter implements SpanExporter {
  private credentialRejected = false;

  constructor(
    private readonly destination: SpanExporter,
    private readonly name: string,
    private readonly release: string,
    private readonly filters: Filters,
    private readonly metadata: WeakMap<object, SpanMetadata>,
  ) {}

  export(spans: ReadableSpan[], callback: (result: ExportResult) => void): void {
    const failed = (): void => {
      callback({ code: ExportResultCode.FAILED, error: new Error("Convergent export failed.") });
    };
    const reportFailure = (): void => {
      reportOnce(
        `export_failed:${this.name}`,
        `Convergent could not export a batch of spans to ${this.name}.`,
      );
    };
    if (this.credentialRejected) {
      failed();
      return;
    }
    try {
      const kept = spans.flatMap((span) => {
        const metadata = this.metadata.get(span);
        try {
          if (!passesAttributeRules(this.filters, span, metadata)) return [];
        } catch {
          reportOnce(
            "filter_decision_failed",
            "Convergent could not decide whether a span may be sent and is not sending it.",
          );
          return [];
        }
        return [exportCopy(span, this.release, metadata)];
      });
      if (kept.length === 0) {
        callback({ code: ExportResultCode.SUCCESS });
        return;
      }
      this.destination.export(kept, (result) => {
        if (result.code === ExportResultCode.SUCCESS) {
          callback(result);
          return;
        }
        const status = result.error instanceof OTLPExporterError ? result.error.code : undefined;
        if (status === 401 || status === 403) {
          this.credentialRejected = true;
          reportOnce(
            "credential_rejected",
            "Convergent rejected the API key; network trace exports are disabled for this process.",
          );
        } else if (status === 413) {
          reportOnce(
            "batch_too_large",
            "Convergent lost a span batch: the server refused it as too large, and a 413 is not retried. Later batches are still sent. Lower OTEL_BSP_MAX_EXPORT_BATCH_SIZE so each request carries fewer spans. A single span whose own content exceeds the server's limit cannot be exported at any batch size.",
          );
        } else {
          reportFailure();
        }
        failed();
      });
    } catch {
      reportFailure();
      failed();
    }
  }

  forceFlush(): Promise<void> {
    return this.destination.forceFlush?.() ?? Promise.resolve();
  }

  shutdown(): Promise<void> {
    return this.destination.shutdown();
  }
}

interface DestinationPipeline {
  exporter: ConvergentExporter;
  batch: BatchSpanProcessor;
}

/** Thrown when no destination could start, which setup reports as `invalid_config`. */
export class NoDestinationError extends Error {}

/** Carries the error from a destination that would not open, which strict mode throws. */
export class StrictDestinationError extends Error {
  constructor(override readonly cause: unknown) {
    super("A Convergent destination could not start.");
  }
}

/** Waits for every destination, so one failure does not stop the others, then reports it. */
async function settleAll(work: Promise<unknown>[]): Promise<void> {
  const failure = (await Promise.allSettled(work)).find((result) => result.status === "rejected");
  if (failure) throw failure.reason;
}

/** One standard batch processor per destination, behind one start-time capture and filter. */
export class ConvergentProcessor implements SpanProcessor {
  private readonly pipelines: DestinationPipeline[] = [];
  readonly artifacts: Artifacts;
  readonly filters: Filters;
  // Keyed by the span object, so an entry lives as long as its span is reachable.
  private readonly metadata = new WeakMap<object, SpanMetadata>();
  private probed: { delegate: TracerProvider; carried: boolean } | undefined;
  readonly destinations: string[] = [];
  // What check() asks the server about.
  readonly credentials: Credentials | undefined;

  constructor(options: ResolvedOptions) {
    this.credentials = options.credentials;
    this.filters = options.filters;
    // Uploads need the API key, so a local-only setup accepts no files.
    this.artifacts = new Artifacts(options.credentials ?? { apiKey: "", endpoint: "" });
    if (!options.credentials) this.artifacts.close();
    try {
      if (options.credentials) {
        this.add("convergent", networkExporter(options.credentials), options.release);
      }
      for (const destination of options.destinations) {
        const name = destinationName(destination);
        let exporter: SpanExporter;
        try {
          exporter = createLocalExporter(destination);
        } catch (error) {
          if (options.strict) throw new StrictDestinationError(error);
          reportOnce(
            `destination:${name}`,
            destination instanceof File
              ? `Convergent could not open ${destination.target}; spans are not written there.`
              : `Convergent could not start its ${name} destination.`,
          );
          continue;
        }
        this.add(name, exporter, options.release);
      }
    } catch (error) {
      disposeUnused(this);
      throw error;
    }
    if (!this.pipelines.length) {
      this.artifacts.close();
      throw new NoDestinationError();
    }
  }

  private add(name: string, destination: SpanExporter, release: string): void {
    const exporter = new ConvergentExporter(
      destination,
      name,
      release,
      this.filters,
      this.metadata,
    );
    try {
      this.pipelines.push({ exporter, batch: new BatchSpanProcessor(exporter) });
    } catch (error) {
      disposeUnused(exporter);
      throw error;
    }
    this.destinations.push(name);
  }

  onStart(span: Span, parentContext: Context): void {
    // Withheld until decided, so a failure below never sends a filtered span.
    const metadata: SpanMetadata = { pairs: {}, unresolved: false, keep: !this.filters.agents };
    this.metadata.set(span, metadata);
    try {
      const operation = span.attributes["gen_ai.operation.name"];
      if (typeof operation === "string" && operation) metadata.operation = operation;
      const sessionId = parentContext.getValue(sessionContextKey);
      if (typeof sessionId === "string") metadata.sessionId = sessionId;
      const startMarks = markAttributes(span.attributes);
      if (Object.keys(startMarks).length) metadata.startMarks = startMarks;
      const parentSpan = trace.getSpan(parentContext);
      const parent = parentSpan && this.metadata.get(parentSpan);
      Object.assign(metadata, inheritedMarks(parentContext, parent));
      const isParent = parentSpan?.spanContext().spanId === span.parentSpanContext?.spanId;
      metadata.keep = agentDecision(this.filters.agents, span, isParent ? parent : undefined);
    } catch {
      // Lost marks must not let a reject rule pass the span.
      metadata.unresolved = true;
      reportOnce("span_metadata", "Convergent could not read a span's start context.");
    }
    for (const { batch } of this.pipelines) batch.onStart(span, parentContext);
  }

  // setContextAttributes on a running span: later children inherit the update.
  updateContextAttributes(span: object, update: Marks): void {
    const metadata = this.metadata.get(span);
    if (metadata) applyUpdate(metadata, update);
  }

  /**
   * Whether the registered global provider carries this processor. The answer
   * is cached per provider and asked again only when the global provider
   * changes, so a provider registered after construction still works. Asking
   * starts one probe span on that provider and never ends it, because ending
   * it would export it. Each provider therefore holds at most one open probe.
   */
  carriedByGlobalProvider(): boolean {
    const global = trace.getTracerProvider();
    const delegate = global instanceof ProxyTracerProvider ? global.getDelegate() : global;
    if (this.probed?.delegate === delegate) return this.probed.carried;
    const probe = delegate
      .getTracer("convergent.sdk")
      .startSpan("convergent.provider_probe", {}, ROOT_CONTEXT);
    let carried: boolean;
    if (probe.isRecording()) {
      carried = this.metadata.has(probe);
    } else if (isSpanContextValid(probe.spanContext())) {
      // The sampler dropped the probe, so it reached no processor and leaks
      // nothing. That answers nothing either: assume carried and ask next time.
      return true;
    } else {
      // An unregistered global provider gives an invalid span.
      carried = false;
    }
    this.probed = { delegate, carried };
    return carried;
  }

  onEnd(span: ReadableSpan): void {
    // The agent rule applies to every destination, so a withheld span takes no queue slot.
    const metadata = this.metadata.get(span);
    if (!metadata?.keep) {
      this.artifacts.spanEnded(span, false);
      return;
    }
    for (const { batch } of this.pipelines) {
      try {
        batch.onEnd(span);
      } catch {
        reportOnce("span_export", "Convergent could not enqueue a completed span.");
      }
    }
    this.artifacts.spanEnded(span, this.sendsFiles(span, metadata));
  }

  // Files go out at span end, before the batch resolves async resource
  // attributes, so a span whose resource is still loading keeps its files
  // back under an attribute rule rather than risk sending a rejected span's files.
  private sendsFiles(span: ReadableSpan, metadata: SpanMetadata): boolean {
    if (!this.filters.require && !this.filters.reject) return true;
    try {
      return (
        !span.resource.asyncAttributesPending && passesAttributeRules(this.filters, span, metadata)
      );
    } catch {
      return false;
    }
  }

  async forceFlush(): Promise<void> {
    await settleAll(
      this.pipelines.map(async ({ batch, exporter }) => {
        // Batch flushing alone does not wait for requests already in the
        // exporter, and a file exporter syncs to disk here.
        try {
          await batch.forceFlush();
        } finally {
          await exporter.forceFlush();
        }
      }),
    );
  }

  async shutdown(): Promise<void> {
    this.artifacts.close();
    await settleAll(this.pipelines.map(({ batch }) => batch.shutdown()));
  }
}

export function createSpanProcessor(options: ResolvedOptions): ConvergentProcessor {
  return new ConvergentProcessor(options);
}

export function disposeUnused(component: Pick<SpanProcessor, "shutdown">): void {
  void component.shutdown().catch(() => {
    reportOnce("setup_cleanup", "Convergent could not shut down unused tracing components.");
  });
}

export class ConvergentSpanProcessor implements SpanProcessor {
  private readonly processor?: ConvergentProcessor;

  constructor(options: ProcessorOptions = {}) {
    if (activeConfiguration()) {
      reportOnce(
        "processor_inactive",
        "Convergent tracing is already configured; this ConvergentSpanProcessor stays inactive.",
      );
      return;
    }
    const resolved = resolveOptions(options);
    if (!resolved) {
      setupFailure("invalid_config");
      return;
    }
    let processor: ConvergentProcessor | undefined;
    try {
      processor = createSpanProcessor(resolved);
      const provider = trace.getTracerProvider();
      const tracer = createTracer(provider);
      setConfiguration({
        provider,
        processor,
        tracer,
        release: resolved.release,
        mode: "attached",
      });
      this.processor = processor;
      drainBeforeExit(processor);
    } catch (error) {
      if (processor) disposeUnused(processor);
      if (error instanceof NoDestinationError) {
        setupFailure("invalid_config", resolved);
        return;
      }
      setupFailure("setup_failed", resolved);
      reportOnce("setup_failed", "Convergent could not initialize tracing.");
    }
  }

  onStart(span: Span, parentContext: Context): void {
    this.processor?.onStart(span, parentContext);
  }

  onEnd(span: ReadableSpan): void {
    this.processor?.onEnd(span);
  }

  forceFlush(): Promise<void> {
    return this.processor?.forceFlush() ?? Promise.resolve();
  }

  shutdown(): Promise<void> {
    return this.processor?.shutdown() ?? Promise.resolve();
  }
}
