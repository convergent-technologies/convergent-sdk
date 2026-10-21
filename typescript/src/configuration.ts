import { type Tracer, type TracerProvider } from "@opentelemetry/api";
import { NodeTracerProvider } from "@opentelemetry/sdk-trace-node";
import { resolveDestinations, type Destination } from "./destinations.js";
import { reportOnce } from "./diagnostics.js";
import {
  describeFilters,
  parseFilters,
  type FilterDescription,
  type FilterOptions,
  type Filters,
} from "./filters.js";
import { checkFlags, strictEnabled, typeName, type Flag } from "./flags.js";
import type { ConvergentProcessor } from "./processor.js";

export interface ProcessorOptions extends FilterOptions {
  apiKey?: string;
  endpoint?: string;
  release?: string;
  strict?: boolean;
}

export interface InitOptions extends ProcessorOptions {
  debug?: boolean;
  destinations?: readonly Destination[];
}

export type StatusReason =
  | "missing_config"
  | "invalid_config"
  | "existing_provider"
  | "already_configured"
  | "setup_failed";

/** Every field is always present. An unset field is null or an empty list. */
export interface Status extends FilterDescription {
  enabled: boolean;
  release: string | null;
  destinations: readonly string[];
  // "owned" when tracing is disabled.
  mode: "owned" | "attached";
  reason: StatusReason | null;
}

export interface Credentials {
  apiKey: string;
  endpoint: string;
}

export interface ResolvedOptions {
  credentials?: Credentials;
  release: string;
  destinations: readonly Destination[];
  strict: boolean;
  filters: Filters;
}

export interface Configuration {
  provider: TracerProvider;
  processor: ConvergentProcessor;
  tracer: Tracer;
  release: string;
  mode: "owned" | "attached";
}

let configuration: Configuration | undefined;
let disabledProvider: NodeTracerProvider | undefined;

const defaultEndpoint = "https://ingest.convergent.dev";

function endpointUrl(endpoint: string): URL | undefined {
  try {
    const url = new URL(endpoint);
    const usable =
      ["http:", "https:"].includes(url.protocol) &&
      url.hostname &&
      !url.username &&
      !url.password &&
      !url.search &&
      !url.hash;
    return usable ? url : undefined;
  } catch {
    return undefined;
  }
}

/**
 * Throws a TypeError for a wrong type and an Error for a value that cannot work.
 * Only `init()` takes local destinations. A processor sends to Convergent alone, so it needs a key.
 */
function parseOptions(
  options: InitOptions,
  flags: readonly Flag[],
  local: boolean,
): ResolvedOptions {
  checkFlags(options, flags);
  for (const name of ["apiKey", "endpoint", "release"] as const) {
    const value = options[name];
    if (value !== undefined && typeof value !== "string") {
      throw new TypeError(`${name} takes a string, got ${typeName(value)}`);
    }
  }
  const destinations = local ? resolveDestinations(options.destinations) : [];
  // Invalid filters disable Convergent export rather than send unfiltered spans.
  const filters = parseFilters(options);
  const apiKey = (options.apiKey ?? process.env.CONVERGENT_API_KEY ?? "").trim();
  const endpoint = options.endpoint ?? process.env.CONVERGENT_ENDPOINT ?? defaultEndpoint;
  const release = (options.release ?? process.env.CONVERGENT_RELEASE ?? "").trim();
  // The endpoint is not echoed, in case a key was pasted into it.
  const url = endpointUrl(endpoint);
  if (!url) {
    throw new Error(
      `endpoint takes an http or https URL without credentials, query, or fragment. The configured value is not echoed here in case it holds a secret by mistake. Check endpoint and CONVERGENT_ENDPOINT. The default is ${defaultEndpoint}.`,
    );
  }
  if (!apiKey && !local) {
    throw new Error(
      "ConvergentSpanProcessor takes an API key: pass apiKey or set CONVERGENT_API_KEY. It sends to Convergent and nowhere else, so there is no destination to fall back on. init({ destinations: [new File(...)] }) traces without credentials.",
    );
  }
  if (!apiKey && !destinations.length) {
    throw new Error(
      "Convergent has nothing to send spans to: pass apiKey or set CONVERGENT_API_KEY. To trace without credentials, pass destinations: [new File(...)] or set CONVERGENT_SPANS_DIR, which writes spans to a file and sends nothing over the network. A process that wants no telemetry does not call init().",
    );
  }
  if (/[\r\n]/u.test(apiKey)) throw new Error("apiKey must not contain line breaks.");
  if (!release) {
    throw new Error(
      "Convergent needs a release: pass release or set CONVERGENT_RELEASE. Any string that names the version of your code works: a git sha, a build id, a date. It is how one version's traces are compared with another's.",
    );
  }
  return {
    ...(apiKey ? { credentials: { apiKey, endpoint: url.toString().replace(/\/+$/u, "") } } : {}),
    release,
    destinations,
    strict: strictEnabled(options.strict),
    filters,
  };
}

/**
 * `flags` are the on/off options this caller accepts, in the shared check order. Strict mode
 * throws the first problem. Otherwise it is logged and the result is undefined. `local` is
 * true for `init()`, which also takes File and Console destinations.
 */
export function resolveOptions(
  options: InitOptions,
  flags: readonly Flag[] = ["strict"],
  local = false,
): ResolvedOptions | undefined {
  try {
    return parseOptions(options, flags, local);
  } catch (error) {
    // options can be null from JavaScript callers.
    if (strictEnabled(options?.strict)) throw error;
    const reason = error instanceof Error ? error.message : "the options could not be read.";
    reportOnce(
      "invalid_config",
      `Convergent tracing is disabled because the configuration cannot work: ${reason} Strict mode is off, so the SDK logged this instead of throwing. Set strict: true or CONVERGENT_STRICT=1 to stop the process at startup instead.`,
    );
    return undefined;
  }
}

export function createTracer(provider: TracerProvider): Tracer {
  return provider.getTracer("convergent.sdk", undefined, {
    schemaUrl: "https://opentelemetry.io/schemas/1.40.0",
  });
}

export function setConfiguration(value: Configuration): void {
  configuration = value;
}

export function configuredStatus(
  { release, mode, processor }: Configuration,
  reason?: "already_configured",
): Status {
  // Listed one by one so the keys come in the shared field order.
  const { agents, requireSpanAttributes, rejectSpanAttributes } = describeFilters(
    processor.filters,
  );
  return {
    enabled: true,
    release,
    agents,
    destinations: processor.destinations,
    mode,
    reason: reason ?? null,
    requireSpanAttributes,
    rejectSpanAttributes,
  };
}

export function disabledStatus(reason: StatusReason, release?: string): Status {
  return {
    enabled: false,
    release: release ?? null,
    agents: [],
    destinations: [],
    mode: "owned",
    reason,
    requireSpanAttributes: null,
    rejectSpanAttributes: null,
  };
}

export function activeConfiguration(): Configuration | undefined {
  return configuration;
}

export function tracerProvider(): TracerProvider {
  if (configuration) return configuration.provider;
  reportOnce(
    "unconfigured_provider",
    "Convergent tracing is not configured. Call init() or construct a ConvergentSpanProcessor before requesting its tracer provider.",
  );
  disabledProvider ??= new NodeTracerProvider({ spanProcessors: [] });
  return disabledProvider;
}
