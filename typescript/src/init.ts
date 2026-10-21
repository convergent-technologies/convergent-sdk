import { randomUUID } from "node:crypto";
import { context, propagation, trace } from "@opentelemetry/api";
import { AsyncLocalStorageContextManager } from "@opentelemetry/context-async-hooks";
import {
  CompositePropagator,
  W3CBaggagePropagator,
  W3CTraceContextPropagator,
} from "@opentelemetry/core";
import {
  defaultResource,
  detectResources,
  envDetector,
  resourceFromAttributes,
} from "@opentelemetry/resources";
import { NodeTracerProvider } from "@opentelemetry/sdk-trace-node";
import {
  activeConfiguration,
  configuredStatus,
  createTracer,
  resolveOptions,
  setConfiguration,
  type Configuration,
  type InitOptions,
  type Status,
} from "./configuration.js";
import { reportOnce } from "./diagnostics.js";
import { setupFailure } from "./failures.js";
import { enableDebug } from "./flags.js";
import { drainBeforeExit } from "./lifecycle.js";
import {
  createSpanProcessor,
  disposeUnused,
  NoDestinationError,
  StrictDestinationError,
  type ConvergentProcessor,
} from "./processor.js";

export function init(options: InitOptions = {}): Status {
  // Before validation and the repeat-call return, so both report through the debug logger.
  enableDebug(options?.debug);
  const active = activeConfiguration();
  if (active) return configuredStatus(active, "already_configured");
  const resolved = resolveOptions(options, ["debug", "strict"], true);
  if (!resolved) return setupFailure("invalid_config");

  let provider: NodeTracerProvider | undefined;
  let processor: ConvergentProcessor | undefined;
  let configuration: Configuration;
  try {
    processor = createSpanProcessor(resolved);
    provider = new NodeTracerProvider({
      // A random service.instance.id per process, as OpenTelemetry SDKs add by default.
      // OTEL_RESOURCE_ATTRIBUTES can still set it.
      resource: defaultResource()
        .merge(resourceFromAttributes({ "service.instance.id": randomUUID() }))
        .merge(detectResources({ detectors: [envDetector] }))
        .merge(resourceFromAttributes({ "service.version": resolved.release })),
      spanProcessors: [processor],
    });
    configuration = {
      provider,
      processor,
      tracer: createTracer(provider),
      release: resolved.release,
      mode: "owned",
    };
    if (!trace.setGlobalTracerProvider(provider)) {
      disposeUnused(provider);
      reportOnce(
        "existing_provider",
        "OpenTelemetry registration failed. A provider may already be registered, or another @opentelemetry/api version initialized the global API. Check your OpenTelemetry setup and run npm ls @opentelemetry/api.",
      );
      return setupFailure("existing_provider", resolved);
    }
    setConfiguration(configuration);
    drainBeforeExit(processor);
  } catch (error) {
    const unused = provider ?? processor;
    if (unused) disposeUnused(unused);
    // Strict mode throws when a spans file will not open.
    if (error instanceof StrictDestinationError) throw error.cause;
    if (error instanceof NoDestinationError) return setupFailure("invalid_config", resolved);
    reportOnce("setup_failed", "Convergent could not initialize tracing.");
    return setupFailure("setup_failed", resolved);
  }

  let manager: AsyncLocalStorageContextManager | undefined;
  try {
    manager = new AsyncLocalStorageContextManager().enable();
    if (context.setGlobalContextManager(manager)) {
      manager = undefined;
    } else {
      reportOnce(
        "existing_context",
        "Convergent is using the existing OpenTelemetry context manager.",
      );
    }
  } catch {
    reportOnce("context_setup", "Convergent could not register its OpenTelemetry context manager.");
  } finally {
    try {
      manager?.disable();
    } catch {
      reportOnce("context_cleanup", "Convergent could not disable its unused context manager.");
    }
  }
  try {
    if (
      !propagation.setGlobalPropagator(
        new CompositePropagator({
          propagators: [new W3CTraceContextPropagator(), new W3CBaggagePropagator()],
        }),
      )
    ) {
      reportOnce(
        "existing_propagator",
        "Convergent is using the existing OpenTelemetry propagator.",
      );
    }
  } catch {
    reportOnce("propagation_setup", "Convergent could not register its OpenTelemetry propagator.");
  }
  return configuredStatus(configuration);
}
