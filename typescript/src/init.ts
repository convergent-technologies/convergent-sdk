import { context, propagation, trace, type Tracer } from "@opentelemetry/api";
import { AsyncLocalStorageContextManager } from "@opentelemetry/context-async-hooks";
import {
  CompositePropagator,
  W3CBaggagePropagator,
  W3CTraceContextPropagator,
} from "@opentelemetry/core";
import { resourceFromAttributes } from "@opentelemetry/resources";
import { NodeTracerProvider } from "@opentelemetry/sdk-trace-node";
import { reportOnce } from "./diagnostics.js";
import { ConvergentSpanProcessor } from "./processor.js";

export interface InitOptions {
  apiKey?: string;
  endpoint?: string;
  release?: string;
}

export interface Status {
  enabled: boolean;
  release?: string;
  mode?: "owned" | "attached";
  reason?: "invalid_config" | "existing_provider" | "already_configured" | "setup_failed";
  destinations: readonly string[];
}

export interface FlushOptions {
  timeoutMs?: number;
  uploads?: boolean;
  uploadTimeoutMs?: number;
}

export interface FlushResult {
  ok: boolean;
  elapsedMs: number;
}

export interface ResolvedOptions {
  apiKey: string;
  endpoint: string;
  release: string;
}

interface Configuration {
  provider: NodeTracerProvider;
  processor: ConvergentSpanProcessor;
  tracer: Tracer;
  release: string;
}

let configuration: Configuration | undefined;
let disabledProvider: NodeTracerProvider | undefined;

function resolveOptions(options: InitOptions): ResolvedOptions | undefined {
  const apiKey = options.apiKey ?? process.env.CONVERGENT_API_KEY;
  const endpoint =
    options.endpoint ?? process.env.CONVERGENT_ENDPOINT ?? "https://ingest.convergent.dev";
  const release = options.release ?? process.env.CONVERGENT_RELEASE;
  if (typeof apiKey !== "string" || !apiKey.trim() || /[\r\n]/u.test(apiKey)) return;
  if (typeof release !== "string" || !release.trim()) return;
  if (typeof endpoint !== "string") return;
  const url = new URL(endpoint);
  if (
    !["http:", "https:"].includes(url.protocol) ||
    !url.hostname ||
    url.username ||
    url.password ||
    url.search ||
    url.hash
  ) {
    return;
  }
  return {
    apiKey: apiKey.trim(),
    endpoint: url.toString().replace(/\/+$/u, ""),
    release: release.trim(),
  };
}

function configuredStatus(reason?: "already_configured"): Status {
  return {
    enabled: true,
    release: configuration!.release,
    mode: "owned",
    destinations: ["convergent"],
    ...(reason ? { reason } : {}),
  };
}

function dispose(provider: NodeTracerProvider): void {
  void provider.shutdown().catch(() => {
    reportOnce("setup_cleanup", "Convergent could not shut down unused tracing components.");
  });
}

export function init(options: InitOptions = {}): Status {
  if (configuration) return configuredStatus("already_configured");
  let resolved: ResolvedOptions | undefined;
  try {
    resolved = resolveOptions(options);
  } catch {
    resolved = undefined;
  }
  if (!resolved) {
    reportOnce(
      "invalid_config",
      "Convergent tracing needs an API key, a nonblank release, and an HTTP(S) endpoint without credentials, query, or fragment.",
    );
    return { enabled: false, reason: "invalid_config", destinations: [] };
  }

  let provider: NodeTracerProvider | undefined;
  let manager: AsyncLocalStorageContextManager | undefined;
  try {
    const processor = new ConvergentSpanProcessor(resolved);
    provider = new NodeTracerProvider({
      resource: resourceFromAttributes({ "service.version": resolved.release }),
      spanProcessors: [processor],
    });
    const tracer = provider.getTracer("convergent.sdk", undefined, {
      schemaUrl: "https://opentelemetry.io/schemas/1.40.0",
    });
    manager = new AsyncLocalStorageContextManager();
    if (!trace.setGlobalTracerProvider(provider)) {
      dispose(provider);
      reportOnce(
        "existing_provider",
        "OpenTelemetry registration failed. A provider may already be registered, or another @opentelemetry/api version initialized the global API. Check your OpenTelemetry setup and run npm ls @opentelemetry/api.",
      );
      return { enabled: false, reason: "existing_provider", destinations: [] };
    }
    manager.enable();
    if (!context.setGlobalContextManager(manager)) {
      manager.disable();
      reportOnce(
        "existing_context",
        "Convergent is using the existing OpenTelemetry context manager.",
      );
    }
    manager = undefined;
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
    configuration = { provider, processor, tracer, release: resolved.release };
    return configuredStatus();
  } catch {
    manager?.disable();
    if (provider) dispose(provider);
    reportOnce("setup_failed", "Convergent could not initialize tracing.");
    return { enabled: false, reason: "setup_failed", destinations: [] };
  }
}

export function activeConfiguration(): Configuration | undefined {
  return configuration;
}

export function tracerProvider(): NodeTracerProvider {
  if (configuration) return configuration.provider;
  reportOnce(
    "unconfigured_provider",
    "Convergent tracing is not configured. Call init() before requesting its tracer provider.",
  );
  disabledProvider ??= new NodeTracerProvider({ spanProcessors: [] });
  return disabledProvider;
}

async function within(promise: Promise<unknown>, ms: number): Promise<boolean> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    return await Promise.race([
      promise.then(() => true),
      new Promise<false>((resolve) => {
        timer = setTimeout(() => resolve(false), ms);
      }),
    ]);
  } finally {
    clearTimeout(timer);
  }
}

export async function flush({
  timeoutMs = 5000,
  uploads = false,
  uploadTimeoutMs = 60_000,
}: FlushOptions = {}): Promise<FlushResult> {
  const started = performance.now();
  if (!configuration) return { ok: true, elapsedMs: 0 };
  if ([timeoutMs, uploadTimeoutMs].some((ms) => !Number.isFinite(ms) || ms < 0)) {
    reportOnce(
      "invalid_flush_timeout",
      "Convergent flush timeoutMs and uploadTimeoutMs must be finite nonnegative numbers.",
    );
    return { ok: false, elapsedMs: performance.now() - started };
  }
  let ok: boolean;
  try {
    ok = await within(configuration.processor.forceFlush(), timeoutMs);
    if (!ok)
      reportOnce("flush_timeout", "Convergent could not finish flushing before the timeout.");
  } catch {
    reportOnce("flush_failed", "Convergent could not flush completed spans.");
    ok = false;
  }
  if (uploads && !(await within(configuration.processor.artifacts.drained(), uploadTimeoutMs))) {
    reportOnce(
      "upload_timeout",
      "Convergent could not finish uploading files before uploadTimeoutMs.",
    );
    ok = false;
  }
  return { ok, elapsedMs: performance.now() - started };
}
