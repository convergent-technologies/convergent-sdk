import { types } from "node:util";
import {
  context,
  trace,
  SpanStatusCode,
  type Attributes,
  type AttributeValue,
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
import type { ArtifactOptions, Artifacts } from "./artifacts.js";
import { reportOnce } from "./diagnostics.js";
import { activeConfiguration } from "./init.js";

export interface SpanOptions {
  name: string;
  operation: string;
  attributes?: Attributes;
  model?: string;
  systemInstructions?: string;
  tools?: readonly Record<string, unknown>[];
}

export type AgentOptions = Omit<SpanOptions, "operation">;
export type ToolOptions = Omit<SpanOptions, "name" | "operation"> & { name?: string };

export interface SpanHandle {
  readonly traceId: string | undefined;
  readonly spanId: string | undefined;
  setInput(value: unknown): void;
  setOutput(value: unknown): void;
  setAttribute(key: string, value: AttributeValue): void;
  inputArtifact(source: string | Uint8Array, options?: ArtifactOptions): void;
  outputArtifact(source: string | Uint8Array, options?: ArtifactOptions): void;
}

const noopHandle: SpanHandle = {
  traceId: undefined,
  spanId: undefined,
  setInput() {},
  setOutput() {},
  setAttribute() {},
  inputArtifact() {},
  outputArtifact() {},
};

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
    private readonly artifacts: Artifacts,
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

  setInput(value: unknown): void {
    this.setContent("input", value);
  }

  setOutput(value: unknown): void {
    this.setContent("output", value);
  }

  inputArtifact(source: string | Uint8Array, options?: ArtifactOptions): void {
    this.artifacts.attach(this.current, "input", source, options ?? {});
  }

  outputArtifact(source: string | Uint8Array, options?: ArtifactOptions): void {
    this.artifacts.attach(this.current, "output", source, options ?? {});
  }

  private setContent(direction: "input" | "output", value: unknown): void {
    safelyRecord(() => {
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
  const attributes = customAttributes(options.attributes);
  attributes["gen_ai.operation.name"] = operation;
  if (operation === "invoke_agent") {
    attributes["gen_ai.agent.name"] = options.name;
    attributes["gen_ai.agent.version"] = release;
    if (options.model !== undefined) attributes["convergent.agent.model"] = options.model;
    if (options.systemInstructions !== undefined) {
      attributes["convergent.agent.system_instructions"] = options.systemInstructions;
    }
    if (options.tools !== undefined) attributes["convergent.agent.tools"] = json(options.tools);
  } else {
    if (options.operation === "tool_call" || operation === "execute_tool") {
      attributes["gen_ai.tool.name"] = options.name;
      attributes["gen_ai.tool.type"] ??= "function";
    }
    if (
      options.model !== undefined ||
      options.systemInstructions !== undefined ||
      options.tools !== undefined
    ) {
      reportOnce(
        "agent_options",
        "Convergent ignored model, systemInstructions, or tools: these options describe an agent run.",
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
  const configuration = activeConfiguration();
  if (!configuration) return callback(noopHandle);

  let current: Span;
  let handle: SpanHandle;
  try {
    const operation = operations.get(options.operation) ?? options.operation;
    const name =
      operation === "invoke_agent" || operation === "execute_tool"
        ? `${operation} ${options.name}`
        : options.name;
    current = configuration.tracer.startSpan(name, {
      attributes: attributesFor(options, operation, configuration.release),
    });
    handle = current.isRecording()
      ? new RecordingSpanHandle(current, operation, configuration.processor.artifacts)
      : noopHandle;
  } catch {
    reportOnce("span_start", "Convergent could not start a span; application work will continue.");
    return callback(noopHandle);
  }

  let called = false;
  try {
    return context.with(trace.setSpan(context.active(), current), () => {
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

export function observe<This, Args extends unknown[], Result>(
  options: SpanOptions,
  callback: (this: This, ...args: Args) => Result,
): (this: This, ...args: Args) => Result {
  return function (this: This, ...args: Args): Result {
    return span(options, () => callback.apply(this, args));
  };
}

export function agent<This, Args extends unknown[], Result>(
  options: AgentOptions,
  callback: (this: This, ...args: Args) => Result,
): (this: This, ...args: Args) => Result {
  return observe({ ...options, operation: "agent_run" }, callback);
}

export function tool<This, Args extends unknown[], Result>(
  options: ToolOptions,
  callback: (this: This, ...args: Args) => Result,
): (this: This, ...args: Args) => Result {
  return observe(
    { ...options, name: options.name ?? (callback.name || "tool"), operation: "tool_call" },
    callback,
  );
}
