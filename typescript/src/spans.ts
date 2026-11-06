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
import {
  CONVERGENT_AGENT_SYSTEM_INSTRUCTIONS,
  CONVERGENT_AGENT_TOOLS,
  CONVERGENT_CONTENT_SOURCE,
  GEN_AI_AGENT_NAME,
  GEN_AI_AGENT_VERSION,
  GEN_AI_INPUT_MESSAGES,
  GEN_AI_OPERATION_NAME,
  GEN_AI_OUTPUT_MESSAGES,
  GEN_AI_REQUEST_MODEL,
  GEN_AI_TOOL_CALL_ARGUMENTS,
  GEN_AI_TOOL_CALL_ID,
  GEN_AI_TOOL_CALL_RESULT,
  GEN_AI_TOOL_NAME,
  GEN_AI_TOOL_TYPE,
  OPERATIONS,
} from "./telemetry-keys.js";

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

// `output` is what setOutput() recorded, held until the callback returns and
// written only when it succeeds.
const openedSpans = new WeakMap<Span, { operation: string; scope: Marks; output?: Attributes }>();

const nameLimit = 128;
const knownOperations: ReadonlySet<string> = new Set([
  ...operations.keys(),
  ...operations.values(),
]);

// The span is still recorded: ingest rejects a bad name, where it costs the caller nothing.
// The messages leave out the caller's value, as every other diagnostic here does.
function reportInvalid(name: unknown, operation: unknown): void {
  // Characters, not UTF-16 units, matching Python's len(). A string never has more
  // characters than units, so a short name skips the count.
  if (
    typeof name !== "string" ||
    !name.trim() ||
    (name.length > nameLimit && Array.from(name).length > nameLimit)
  ) {
    reportOnce(
      "invalid_name",
      `Convergent recorded a span with an unusable name. A name must be 1-${nameLimit} characters; ingest will reject this one. The span was still sent.`,
    );
  }
  if (typeof operation !== "string" || !knownOperations.has(operation)) {
    reportOnce(
      "invalid_operation",
      `Convergent recorded a span with an unrecognized operation. It is stored as given but will not map to a first-class concept. Known operations: ${[...operations.keys()].join(", ")}.`,
    );
  }
}

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
      this.current.setAttribute(GEN_AI_TOOL_CALL_ID, id);
    });
  }

  private setContent(direction: "input" | "output", value: unknown): void {
    safelyRecord(() => {
      if (!this.current.isRecording()) return;
      const toolCall = this.operation === OPERATIONS.tool_call;
      const encoded = toolCall
        ? text(value)
        : messages(direction === "input" ? "user" : "assistant", value);
      const opened = openedSpans.get(this.current);
      if (encoded === undefined) {
        // A dropped final answer must not leave an earlier partial one to ship.
        if (direction === "output" && opened) delete opened.output;
        return;
      }
      const content: Attributes = toolCall
        ? {
            [direction === "input" ? GEN_AI_TOOL_CALL_ARGUMENTS : GEN_AI_TOOL_CALL_RESULT]: encoded,
          }
        : {
            [direction === "input" ? GEN_AI_INPUT_MESSAGES : GEN_AI_OUTPUT_MESSAGES]: encoded,
            // Same literal as the Python SDK's _semantic.py and ingest's otel_span_mapper.py; change all three.
            [CONVERGENT_CONTENT_SOURCE]: "sdk",
          };
      // A span this SDK opened holds its output until the callback succeeds.
      // Another library's span ends where this SDK cannot see, so it is written now.
      if (direction === "output" && opened) opened.output = content;
      else this.current.setAttributes(content);
    });
  }
}

function attributesFor(options: SpanOptions, operation: string, release: string): Attributes {
  // First, so OpenTelemetry's attribute count limit drops caller attributes before it.
  const attributes: Attributes = {
    [GEN_AI_OPERATION_NAME]: operation,
    ...customAttributes(options.attributes),
  };
  if (typeof options.model === "string" && !options.model.trim()) {
    reportOnce(
      "empty_model",
      "Convergent ignored an empty model name. Pass the model's name, or leave model out.",
    );
  } else if (options.model !== undefined) {
    attributes[GEN_AI_REQUEST_MODEL] = options.model;
  }
  if (operation === OPERATIONS.agent_run) {
    attributes[GEN_AI_AGENT_NAME] = options.name;
    attributes[GEN_AI_AGENT_VERSION] = release;
    if (options.systemInstructions !== undefined) {
      attributes[CONVERGENT_AGENT_SYSTEM_INSTRUCTIONS] = options.systemInstructions;
    }
    const tools = options.tools === undefined ? undefined : json(options.tools);
    if (tools !== undefined) attributes[CONVERGENT_AGENT_TOOLS] = tools;
  } else {
    if (operation === OPERATIONS.tool_call) {
      attributes[GEN_AI_TOOL_NAME] = options.name;
      attributes[GEN_AI_TOOL_TYPE] ??= "function";
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
    // An aborted call was stopped by its caller, not failed, so its status stays
    // unset. Matched by name, since the class varies by runtime.
    if ((error as { name?: unknown } | null)?.name === "AbortError") return;
    current.setStatus({
      code: SpanStatusCode.ERROR,
      ...(error instanceof Error ? { message: error.name } : {}),
    });
  });
}

// The held output is written only when the call succeeded. A failed or aborted
// call drops it, so a partial answer never ships as the result.
function end(current: Span, succeeded: boolean): void {
  const output = openedSpans.get(current)?.output;
  if (succeeded && output) safelyRecord(() => current.setAttributes(output));
  safelyRecord(() => current.end());
}

function runCallback<T>(current: Span, handle: SpanHandle, callback: (handle: SpanHandle) => T): T {
  let result: T;
  try {
    result = callback(handle);
  } catch (error) {
    recordError(current, error);
    end(current, false);
    throw error;
  }
  if (types.isPromise(result)) {
    try {
      return result.then(
        (value: unknown) => {
          end(current, true);
          return value;
        },
        (error: unknown) => {
          recordError(current, error);
          end(current, false);
          throw error;
        },
      ) as T;
    } catch {
      reportOnce("promise_tracking", "Convergent could not track the returned Promise.");
    }
  }
  end(current, true);
  return result;
}

export function span<T>(options: SpanOptions, callback: (handle: SpanHandle) => T): T {
  reportInvalid(options?.name, options?.operation);
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
      operation === OPERATIONS.agent_run || operation === OPERATIONS.tool_call
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
    GEN_AI_OPERATION_NAME
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
  // Checked once, when the function is wrapped, as Python's decorator does.
  reportInvalid(options?.name, options?.operation);
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
