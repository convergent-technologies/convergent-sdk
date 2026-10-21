import { types } from "node:util";
import {
  context,
  isSpanContextValid,
  trace,
  SpanStatusCode,
  type Attributes,
  type AttributeValue,
  type Context,
  type Span,
} from "@opentelemetry/api";
import {
  acceptedAttribute,
  customAttributes,
  json,
  messages,
  operations,
  text,
} from "./attributes.js";
import type { ArtifactOptions } from "./artifacts.js";
import { reportOnce } from "./diagnostics.js";
import { activeConfiguration } from "./configuration.js";
import {
  applyUpdate,
  contextUpdate,
  resolveContextAttributes,
  withRequestScope,
  type ContextAttributes,
  type Marks,
} from "./filters.js";
import type { ConvergentProcessor } from "./processor.js";

export interface SpanOptions {
  name: string;
  operation: string;
  attributes?: Attributes;
  // Marks this span and every span started inside it, as convergent.attributes.<key>.
  contextAttributes?: ContextAttributes;
  model?: string;
  systemInstructions?: string;
  tools?: readonly Record<string, unknown>[];
}

// A wrapped function may also resolve its pairs from each call's arguments.
export type ObserveOptions<Args extends unknown[] = unknown[]> = Omit<
  SpanOptions,
  "contextAttributes"
> & { contextAttributes?: ContextAttributes | ((...args: Args) => ContextAttributes) };
export type AgentOptions<Args extends unknown[] = unknown[]> = Omit<
  ObserveOptions<Args>,
  "operation"
>;
// tool() takes no model, systemInstructions, or tools.
export type ToolOptions<Args extends unknown[] = unknown[]> = Pick<
  ObserveOptions<Args>,
  "attributes" | "contextAttributes"
> & { name?: string };

export interface TraceRef {
  readonly traceId: string;
  readonly spanId: string;
}

export interface SpanHandle {
  readonly traceId: string | undefined;
  readonly spanId: string | undefined;
  setInput(value: unknown): void;
  setOutput(value: unknown): void;
  setToolCallId(id: string): void;
  setAttribute(key: string, value: AttributeValue): void;
  setContextAttributes(attributes: ContextAttributes): void;
  inputArtifact(source: string | Uint8Array, options?: ArtifactOptions): void;
  outputArtifact(source: string | Uint8Array, options?: ArtifactOptions): void;
}

const noopHandle: SpanHandle = {
  traceId: undefined,
  spanId: undefined,
  setInput() {},
  setOutput() {},
  setToolCallId() {},
  setAttribute() {},
  setContextAttributes() {},
  inputArtifact() {},
  outputArtifact() {},
};

const openedSpans = new WeakMap<Span, { operation: string; scope: Marks }>();

function safelyRecord(callback: () => void): void {
  try {
    callback();
  } catch {
    reportOnce("span_recording", "Convergent could not record span data.");
  }
}

class RecordingSpanHandle implements SpanHandle {
  readonly traceId: string;
  readonly spanId: string;

  constructor(
    private readonly current: Span,
    private readonly operation: string,
    private readonly processor: ConvergentProcessor,
  ) {
    const spanContext = current.spanContext();
    this.traceId = spanContext.traceId;
    this.spanId = spanContext.spanId;
  }

  setAttribute(key: string, value: AttributeValue): void {
    safelyRecord(() => {
      if (acceptedAttribute(key, value)) this.current.setAttribute(key, value);
    });
  }

  setContextAttributes(attributes: ContextAttributes): void {
    safelyRecord(() => {
      const opened = openedSpans.get(this.current);
      if (!opened) {
        reportOnce(
          "set_context_attributes_foreign_span",
          "Convergent ignored setContextAttributes(): this is not a span Convergent opened. Call it on a span(), agent(), tool(), or observe() handle.",
        );
        return;
      }
      if (trace.getActiveSpan() !== this.current || !this.current.isRecording()) {
        reportOnce(
          "set_context_attributes_off_span",
          "Convergent ignored setContextAttributes(): the handle's span is not the active recording span. Call it inside the span it marks.",
        );
        return;
      }
      // Spans started earlier keep their saved pairs. Later children read the updated scope.
      const update = contextUpdate(attributes, "setContextAttributes()", true);
      applyUpdate(opened.scope, update);
      this.processor.updateContextAttributes(this.current, update);
    });
  }

  setInput(value: unknown): void {
    this.setContent("input", value);
  }

  setOutput(value: unknown): void {
    this.setContent("output", value);
  }

  inputArtifact(source: string | Uint8Array, options?: ArtifactOptions): void {
    this.attachArtifact("input", source, options);
  }

  outputArtifact(source: string | Uint8Array, options?: ArtifactOptions): void {
    this.attachArtifact("output", source, options);
  }

  private attachArtifact(
    role: "input" | "output",
    source: string | Uint8Array,
    options: ArtifactOptions | undefined,
  ): void {
    // Another tracer's span may never reach this processor's onEnd, which is
    // what releases the held copy.
    if (!openedSpans.has(this.current)) {
      reportOnce(
        "artifact_foreign_span",
        "Convergent ignored a file recorded on a span it did not open. Record files on a span() or observe() span.",
      );
      return;
    }
    this.processor.artifacts.attach(this.current, role, source, options ?? {});
  }

  setToolCallId(id: string): void {
    safelyRecord(() => {
      if (typeof id !== "string" || !id || Array.from(id).length > 256) {
        reportOnce(
          "invalid_tool_call_id",
          "Convergent ignored an invalid tool call id: use a string of 1-256 characters.",
        );
        return;
      }
      this.current.setAttribute("gen_ai.tool.call.id", id);
    });
  }

  private setContent(direction: "input" | "output", value: unknown): void {
    safelyRecord(() => {
      if (!this.current.isRecording()) return;
      if (this.operation === "execute_tool") {
        const key =
          direction === "input" ? "gen_ai.tool.call.arguments" : "gen_ai.tool.call.result";
        this.current.setAttribute(key, text(value));
        return;
      }
      this.current.setAttribute(
        `gen_ai.${direction}.messages`,
        messages(direction === "input" ? "user" : "assistant", value),
      );
      this.current.setAttribute("convergent.content.source", "sdk");
    });
  }
}

function attributesFor(options: SpanOptions, operation: string, release: string): Attributes {
  // First, so OpenTelemetry's attribute count limit drops caller attributes before it.
  const attributes: Attributes = {
    "gen_ai.operation.name": operation,
    ...customAttributes(options.attributes),
  };
  if (options.model !== undefined) attributes["gen_ai.request.model"] = options.model;
  if (operation === "invoke_agent") {
    attributes["gen_ai.agent.name"] = options.name;
    attributes["gen_ai.agent.version"] = release;
    if (options.systemInstructions !== undefined) {
      attributes["convergent.agent.system_instructions"] = options.systemInstructions;
    }
    if (options.tools !== undefined) attributes["convergent.agent.tools"] = json(options.tools);
  } else {
    if (operation === "execute_tool") {
      attributes["gen_ai.tool.name"] = options.name;
      attributes["gen_ai.tool.type"] ??= "function";
    }
    if (options.systemInstructions !== undefined || options.tools !== undefined) {
      reportOnce(
        "agent_options",
        "Convergent ignored systemInstructions or tools: these options describe an agent run.",
      );
    }
  }
  return attributes;
}

function recordError(current: Span, error: unknown): void {
  safelyRecord(() => {
    current.setStatus({
      code: SpanStatusCode.ERROR,
      ...(error instanceof Error ? { message: error.name } : {}),
    });
  });
}

function runCallback<T>(current: Span, handle: SpanHandle, callback: (handle: SpanHandle) => T): T {
  let result: T;
  try {
    result = callback(handle);
  } catch (error) {
    recordError(current, error);
    safelyRecord(() => current.end());
    throw error;
  }
  if (types.isPromise(result)) {
    try {
      return result.then(
        (value: unknown) => {
          safelyRecord(() => current.end());
          return value;
        },
        (error: unknown) => {
          recordError(current, error);
          safelyRecord(() => current.end());
          throw error;
        },
      ) as T;
    } catch {
      reportOnce("promise_tracking", "Convergent could not track the returned Promise.");
    }
  }
  safelyRecord(() => current.end());
  return result;
}

export function span<T>(options: SpanOptions, callback: (handle: SpanHandle) => T): T {
  if (!activeConfiguration()) return callback(noopHandle);
  // A resolver belongs on a wrapped function, which has arguments to give it,
  // so a function here is unresolved.
  return record(
    options,
    contextUpdate(options.contextAttributes, "contextAttributes", false),
    callback,
  );
}

function record<T>(
  options: Omit<SpanOptions, "contextAttributes">,
  update: Marks,
  callback: (handle: SpanHandle) => T,
): T {
  const configuration = activeConfiguration();
  if (!configuration) return callback(noopHandle);

  let current: Span;
  let handle: SpanHandle;
  let parentContext: Context;
  try {
    // Decide before opening a span, so a provider without this
    // processor never receives one.
    if (configuration.mode === "attached" && !configuration.processor.carriedByGlobalProvider()) {
      reportOnce(
        "no_provider",
        "Convergent cannot find the tracer provider your ConvergentSpanProcessor was added to, so span(), agent(), tool(), and observe() are recording nothing. Spans your own tracers record still reach Convergent. Register that provider globally, for example with provider.register().",
      );
      return callback(noopHandle);
    }
    const operation = operations.get(options.operation) ?? options.operation;
    const name =
      operation === "invoke_agent" || operation === "execute_tool"
        ? `${operation} ${options.name}`
        : options.name;
    const scoped = withRequestScope(context.active(), update);
    parentContext = scoped.context;
    current = configuration.tracer.startSpan(
      name,
      { attributes: attributesFor(options, operation, configuration.release) },
      parentContext,
    );
    openedSpans.set(current, { operation, scope: scoped.scope });
    handle = new RecordingSpanHandle(current, operation, configuration.processor);
  } catch {
    reportOnce("span_start", "Convergent could not start a span; application work will continue.");
    return callback(noopHandle);
  }

  let called = false;
  try {
    return context.with(trace.setSpan(parentContext, current), () => {
      called = true;
      return runCallback(current, handle, callback);
    });
  } catch (error) {
    if (called) throw error;
    safelyRecord(() => current.end());
    reportOnce(
      "span_context",
      "Convergent could not activate a span; application work will continue.",
    );
    return callback(noopHandle);
  }
}

// Best effort: the OpenTelemetry SDK span exposes `attributes`, but the Span API
// does not promise it. Without an operation, content goes to message keys.
function publicOperation(current: Span): string {
  const value = (current as Span & { readonly attributes?: Attributes }).attributes?.[
    "gen_ai.operation.name"
  ];
  return typeof value === "string" ? value : "";
}

export function currentSpan(): SpanHandle {
  const configuration = activeConfiguration();
  if (!configuration) return noopHandle;
  try {
    const current = trace.getActiveSpan();
    if (!current || !isSpanContextValid(current.spanContext())) return noopHandle;
    const operation = openedSpans.get(current)?.operation ?? publicOperation(current);
    return new RecordingSpanHandle(current, operation, configuration.processor);
  } catch {
    reportOnce("current_span", "Convergent could not read the active span.");
    return noopHandle;
  }
}

export function currentTrace(): TraceRef | undefined {
  try {
    const spanContext = trace.getSpanContext(context.active());
    if (!spanContext || !isSpanContextValid(spanContext)) return undefined;
    return {
      traceId: spanContext.traceId.toLowerCase(),
      spanId: spanContext.spanId.toLowerCase(),
    };
  } catch {
    reportOnce("current_trace", "Convergent could not read the active trace.");
    return undefined;
  }
}

export function observe<This, Args extends unknown[], Result>(
  options: ObserveOptions<Args>,
  callback: (this: This, ...args: Args) => Result,
): (this: This, ...args: Args) => Result {
  return function (this: This, ...args: Args): Result {
    // Resolved only while tracing is configured, so a disabled SDK never runs the resolver.
    if (!activeConfiguration()) return callback.apply(this, args);
    const update = resolveContextAttributes(options.contextAttributes, args);
    return record(options, update, () => callback.apply(this, args));
  };
}

export function agent<This, Args extends unknown[], Result>(
  options: AgentOptions<Args>,
  callback: (this: This, ...args: Args) => Result,
): (this: This, ...args: Args) => Result {
  return observe({ ...options, operation: "agent_run" }, callback);
}

export function tool<This, Args extends unknown[], Result>(
  options: ToolOptions<Args>,
  callback: (this: This, ...args: Args) => Result,
): (this: This, ...args: Args) => Result {
  // Picked by name, so a JavaScript caller's model or tools are not recorded.
  const { name, attributes, contextAttributes } = options;
  return observe(
    {
      name: name ?? (callback.name || "tool"),
      operation: "tool_call",
      ...(attributes ? { attributes } : {}),
      ...(contextAttributes ? { contextAttributes } : {}),
    },
    callback,
  );
}
