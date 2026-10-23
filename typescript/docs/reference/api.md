---
title: API Reference
description: Every call in the public surface.
---

Import everything here from `convergent-sdk`:

```ts
import { agent, flush, init, span } from "convergent-sdk";
```

`init()` and `ConvergentSpanProcessor` reject a setting that cannot work. By
default they log one warning and leave tracing off. With `strict: true` or
`CONVERGENT_STRICT=1`, they throw at startup. `File` and `Console` check their
own arguments and throw when you construct them. After setup, the SDK never
throws into your code. An error your own code throws inside a traced function
is thrown again unchanged.

The SDK has no error classes of its own:

- A value of the wrong type throws a `TypeError`.
- A value of the right type that cannot work throws an `Error`, or a
  `RangeError` from `File` and `Console`.

Each call below lists its signature, what it returns, and its options.

## Setup

### version

```ts
import { version } from "convergent-sdk";
```

This package's version string, or `"0.0.0"` when the source runs without a
build.

### init()

```ts
init(options?: InitOptions): Status
```

Sets up tracing for the process. Call it once at startup, before the work you
want to trace.

**Returns:** a [Status](#status) that says what was set up.

**Options:** every option is optional. Most fall back to an environment
variable when you leave them out. [Configuration](../configuration.md) lists
the variables.

| Option | Type | Default | Meaning |
| --- | --- | --- | --- |
| `apiKey` | `string` | `CONVERGENT_API_KEY` | the ingestion key. Sends spans to Convergent. Required unless a destination is set |
| `endpoint` | `string` | `CONVERGENT_ENDPOINT`, else `https://ingest.convergent.dev` | the receiver address, such as your own data plane or a collector |
| `release` | `string` | `CONVERGENT_RELEASE` | the version of your code, such as a Git SHA, a build ID, or a date. Required |
| `agents` | `string[]` | send every span | the agent names Convergent may see |
| `requireSpanAttributes` | [`SpanAttributeRules`](#spanattributerules) | `CONVERGENT_REQUIRE_SPAN_ATTRIBUTES`, else none | send a span only when it holds every listed value |
| `rejectSpanAttributes` | [`SpanAttributeRules`](#spanattributerules) | `CONVERGENT_REJECT_SPAN_ATTRIBUTES`, else none | withhold a span that holds any listed value |
| `destinations` | [`Destination[]`](#destinations) | none | also write spans to a file or the console |
| `debug` | `boolean` | `CONVERGENT_DEBUG`, else `false` | show the SDK's warnings on the console. See [Configuration](../configuration.md#see-the-sdks-warnings) |
| `strict` | `boolean` | `CONVERGENT_STRICT`, else `false` | throw at startup on a configuration that cannot work |

`agents`, `requireSpanAttributes`, and `rejectSpanAttributes` choose which
spans Convergent receives. [Span filters](../../../docs/span-filters.md)
explains them.

```ts
import { Console, File, init } from "convergent-sdk";

init({
  release: "2026.10.06.1",
  destinations: [new File("./traces"), new Console({ stream: "stderr" })],
});
```

With a bad configuration, `init()` logs one warning that names the first
problem and returns `enabled: false` with `reason: "invalid_config"`. With
strict mode on, it throws that problem instead.
[Strict startup](../configuration.md#strict-startup) lists every check.

After a successful call, a later `init()` keeps the first configuration,
returns `reason: "already_configured"`, and never throws. Its `debug` still
applies.

`init()` registers its tracer provider for the whole process. If OpenTelemetry
refuses, `init()` returns `reason: "existing_provider"`. To use a provider you
already have, see [ConvergentSpanProcessor](#convergentspanprocessor).

### Status

Returned by `init()`, and part of the [check()](#check) report. Every field is
always present. An unset field is `null` or `[]`.

| Field | Type | Meaning |
| --- | --- | --- |
| `enabled` | `boolean` | tracing is on |
| `release` | `string \| null` | the release this process reports |
| `agents` | `string[]` | the running `agents` filter, sorted |
| `destinations` | `string[]` | where spans go, such as `convergent`, `file:/srv/app/traces/spans.jsonl`, or `console:stderr` |
| `mode` | `"owned" \| "attached"` | `"owned"` when `init()` created the provider, `"attached"` when a `ConvergentSpanProcessor` is in your provider. `"owned"` while tracing is off |
| `reason` | `string \| null` | `null`, or why part of the setup is not working |
| `requireSpanAttributes` | `object \| null` | the running require filter, sorted, with every value in an array |
| `rejectSpanAttributes` | `object \| null` | the running reject filter, in the same shape |

| `reason` | What it means |
| --- | --- |
| `null` | tracing works |
| `invalid_config` | the configuration cannot work, so nothing is sent. The warning names the problem |
| `existing_provider` | OpenTelemetry refused to register the provider. Another library may have registered one, or two copies of `@opentelemetry/api` are loaded. Run `npm ls @opentelemetry/api` |
| `already_configured` | an earlier setup is running and this call's settings were not applied. Tracing works |
| `setup_failed` | setup ran but tracing could not start. A warning names what failed |
| `missing_config` | seen only in a `check()` report: no setup has run in this process |

### check()

```ts
check(): Promise<Report>
```

Reads what `init()` set up, then asks the server once whether it accepts the
API key. It always resolves. A network failure or a rejected key comes back as
a report that says so. Print the report:

```ts
import { check } from "convergent-sdk";

const report = await check();
console.log(String(report));
```

**Returns:** a [Report](#report). Takes no arguments.

`report.ok` is `true` when tracing is on and the server accepted the key. A
working setup that only writes to a file has `report.status.enabled` `true` and
`report.ok` `false`, because there is no key to check. A successful check does
not show that a trace was stored.

The request goes to `<endpoint>/v1/check?release=<release>`. It refuses
redirects, stops after 5 seconds, and reads at most 1 MiB. The printed report
cuts server text to 500 characters, adds `...` when it cuts, and turns
unprintable characters into spaces. It never shows the key or a response
body. When filters are set, it prints them on a `filters` line, reject first.

#### Report

| Field | Type | Meaning |
| --- | --- | --- |
| `ok` | `boolean` | tracing is on and the server accepted the key |
| `status` | [`Status`](#status) | what `init()` set up |
| `roundTrip` | `string` | `"ok"`, or why the request failed. See below |
| `roundTripMs` | `number \| undefined` | how long the request took. Set only when it succeeded |
| `endpoint` | `string \| undefined` | where the request went. Never holds the key |
| `organizationId` | `string \| undefined` | the workspace the key belongs to |
| `agents` | `string[]` | the agents the server has linked to this release |
| `agentsTruncated` | `boolean` | `true` when the server had more agents than it listed |
| `notes` | [`Note[]`](#note) | a note for each problem the server can see |

`roundTrip` is one of:

- `ok`
- `no_credentials`: there is no API key to check.
- `http_<status>`, such as `http_401`. A redirect also shows as its status.
- `not_a_check_response`, `bad_json`, or `response_too_large`: the server's
  answer was not a check response.
- `TimeoutError`: no answer within 5 seconds.
- A Node error code such as `ECONNREFUSED`, or `connection_failed` when Node
  gives no code.
- `unexpected`.

#### Note

| Field | Type | Meaning |
| --- | --- | --- |
| `code` | `string` | which problem. Any string the server sends |
| `message` | `string` | the server's own wording, ready to print |

## Tracing

### session()

```ts
session<T>(id: string, callback: () => T): T
```

Groups separate conversation turns. Every span started inside `callback`
carries the session ID, so each turn can have its own trace and still share
one session.

```ts
import { agent, currentSpan, currentTrace, session } from "convergent-sdk";

const answer = agent({ name: "order_support" }, async (question: string) => {
  currentSpan().setInput(question);
  const response = await model(question);
  currentSpan().setOutput(response);
  return { ...currentTrace(), answer: response };
});

const turns = await session("order-conversation-1042", async () => [
  await answer("Where is order 1042?"),
  await answer("When will it arrive?"),
]);
```

**Returns:** whatever `callback` returns. An error `callback` throws is thrown
again unchanged.

**Arguments:**

| Argument | Type | Meaning |
| --- | --- | --- |
| `id` | `string` | your conversation or thread ID. The SDK trims spaces, then takes 1 to 128 characters |
| `callback` | `() => T` | the work that belongs to the session |

- With an invalid ID, the callback still runs, without a new session. The
  first invalid ID in a process logs a warning.
- Nested sessions use the innermost ID.
- Async work keeps the session it was created in.
- A session creates no span and adds nothing to OpenTelemetry baggage, so it
  does not reach other services. Open a session in each service.
- The ID travels with every span, so do not put emails or other personal data
  in it.
- The SDK adds `convergent.session.id` only to the copy it sends to
  Convergent. A span that already holds a string `convergent.session.id` keeps
  its own value.

Run the
[sessions example](https://github.com/convergent-technologies/convergent-sdk/blob/main/typescript/examples/sessions/main.mjs)
after you build the package and set the same variables as the quickstart:

```sh
node examples/sessions/main.mjs
```

### observe()

```ts
observe<Args, Result>(options: ObserveOptions<Args>, fn: (...args: Args) => Result): (...args: Args) => Result
```

Wraps a function so each call records one span. The span ends when the
function returns, or when its returned Promise settles.

**Returns:** the wrapped function. It keeps the arguments, `this`, the return
type, and whatever `fn` throws. The wrapper gives you no handle. To record
input and output inside it, use [currentSpan()](#currentspan).

**Options:**

| Option | Type | Meaning |
| --- | --- | --- |
| `name` | `string` | required. The span's name. For an agent run, the agent's identity |
| `operation` | `string` | required. What the span records. See [Operations](#operations) |
| `attributes` | `Attributes` | attributes for this one span |
| `contextAttributes` | [`ContextAttributes`](#contextattributes) or `(...args: Args) => ContextAttributes` | marks this span and every span started inside it, for the span filters |
| `model` | `string` | the model the span asks for, written as `gen_ai.request.model` |
| `systemInstructions` | `string` | an agent run's system prompt |
| `tools` | `Record<string, unknown>[]` | the tool definitions an agent run offers |

`contextAttributes` takes an object, or a resolver that gets the wrapped
function's arguments by position and returns an object:

```ts
const support = agent(
  { name: "order_support", contextAttributes: (customerId: string) => ({ "customer.id": customerId }) },
  async (customerId: string) => {
    currentSpan().setContextAttributes({ "customer.tier": await lookupTier(customerId) });
    return answer(customerId);
  },
);
```

A resolver must return a plain object right away. An async resolver returns a
Promise, so it does not work. Look the value up inside the function and call
`setContextAttributes()` instead. A resolver that throws or returns something
other than a plain object leaves the span unmarked, and any attribute filter
then withholds it and its children.
[Span filters](../../../docs/span-filters.md) explains marks.

`systemInstructions` and `tools` apply only to an agent run. On other
operations the SDK ignores them and logs a warning.

### agent()

```ts
agent<Args, Result>(options: AgentOptions<Args>, fn: (...args: Args) => Result): (...args: Args) => Result
```

`observe()` with `operation: "agent_run"`. `name` is the agent's identity in
your workspace.

**Returns:** the wrapped function.

**Options:** the same as [observe()](#observe), without `operation`: `name`,
`attributes`, `contextAttributes`, `model`, `systemInstructions`, and `tools`.

```ts
const handle = agent(
  {
    name: "support-agent",
    model: "gpt-5.5",
    systemInstructions: "Look up the order before answering.",
    tools: [{ name: "lookup_order", description: "Fetch one order by ID." }],
  },
  async (ticket: string) => answerTicket(ticket),
);
```

### tool()

```ts
tool<Args, Result>(options: ToolOptions<Args>, fn: (...args: Args) => Result): (...args: Args) => Result
```

`observe()` with `operation: "tool_call"`.

**Returns:** the wrapped function.

**Options:**

| Option | Type | Meaning |
| --- | --- | --- |
| `name` | `string` | the tool's name. Defaults to `fn.name` |
| `attributes` | `Attributes` | as on [observe()](#observe) |
| `contextAttributes` | object or resolver | as on [observe()](#observe) |

`tool()` takes no other options.

### span()

```ts
span<T>(options: SpanOptions, callback: (handle: SpanHandle) => T): T
```

Starts a span, runs `callback` inside it, and ends the span when the callback
returns or its Promise settles.

```ts
import { agent, flush, init, span } from "convergent-sdk";

init({ release: "2026.10.06.1" });

const answer = agent({ name: "order_support" }, async (question: string) => {
  const order = await span({ name: "lookup_order", operation: "tool_call" }, (call) => {
    call.setInput({ orderId: "1042" });
    const order = { orderId: "1042", status: "shipped" };
    call.setOutput(order);
    return order;
  });
  return await span({ name: "gpt-5.5", operation: "model_call" }, async (call) => {
    call.setInput({ question, order });
    const text = await model(question, order);
    call.setOutput(text);
    call.setAttribute("gen_ai.usage.input_tokens", 24);
    return text;
  });
});

await answer("Where is order 1042?");
await flush();
```

**Returns:** whatever `callback` returns.

**Options:** the same as [observe()](#observe), except that
`contextAttributes` takes an object only. A span has no call arguments to give
a resolver.

A span is a child of whatever span is active when it starts. Above, the tool
and model spans sit under the agent run.

To trace a stream, read it inside the callback. If the callback returns an
iterator, the span ends when the callback returns, before the stream is read.

```ts
const text = await span({ name: "gpt-5.5", operation: "model_call" }, async (call) => {
  let output = "";
  for await (const chunk of stream) output += chunk;
  call.setOutput(output);
  return output;
});
```

When the callback throws or its Promise rejects, the span's status becomes an
error and the error is thrown again unchanged. The span records only the
error's class name. It records no message and no stack, and nothing from a
thrown string.

#### Operations

| You write | `gen_ai.operation.name` | What else the SDK writes |
| --- | --- | --- |
| `agent_run` | `invoke_agent` | names the span `invoke_agent <name>`. Writes `gen_ai.agent.name`, and your release as `gen_ai.agent.version` |
| `tool_call` | `execute_tool` | names the span `execute_tool <name>`. Writes `gen_ai.tool.name`, and `gen_ai.tool.type` as `function` unless you set it |
| `model_call` | `chat` | nothing more |
| `retrieval` | `retrieval` | |
| `embeddings` | `embeddings` | |
| `workflow` | `invoke_workflow` | |
| `agent_create` | `create_agent` | |
| `text_completion` | `text_completion` | |
| `generate_content` | `generate_content` | |
| any other string | that string | |

On any operation, `model` writes `gen_ai.request.model`. Every operation other
than `agent_run` and `tool_call` keeps the name you gave it, so
`span({ name: "gpt-5.5", operation: "model_call" }, ...)` appears as `gpt-5.5`.
[Attributes](../../../docs/attributes.md) lists every key Convergent reads.

### The span handle

`span()` passes a `SpanHandle` to its callback. [currentSpan()](#currentspan)
returns one for the active span.

| Member | What it does |
| --- | --- |
| `setInput(value)` | writes `gen_ai.input.messages`, or `gen_ai.tool.call.arguments` on a tool call |
| `setOutput(value)` | writes `gen_ai.output.messages`, or `gen_ai.tool.call.result` on a tool call |
| `setToolCallId(id)` | writes `gen_ai.tool.call.id`, which pairs a model's request for a tool with the call itself |
| `setAttribute(key, value)` | writes one attribute of your own |
| `setContextAttributes(pairs)` | marks this span and every span started inside it after the call, for the span filters |
| `inputArtifact(source, options?)` | records a file the span read and uploads a copy |
| `outputArtifact(source, options?)` | records a file the span produced and uploads a copy |
| `traceId` | the trace's ID as a hex string, or `undefined` |
| `spanId` | the span's ID as a hex string, or `undefined` |

When tracing is off, the handle does nothing and its IDs are `undefined`. When
the sampler drops a span, its handle keeps the IDs and its setters do nothing.

**setInput() and setOutput().** On a tool call, a string is written as it is
and any other value as compact JSON. On other operations, an array of message
objects that each have a `role` is written as JSON. Any other value becomes
one text message, with role `user` for input and `assistant` for output. The
SDK does not read your function's arguments or return value for you.

**setToolCallId().** Takes a string of 1 to 256 characters and keeps it as
given, spaces included. The first invalid ID logs a warning that does not show
the value.

**setAttribute().** Takes a string, number, boolean, or an array of one of
those types. It ignores, with a warning, keys that start with `convergent.`
and these keys the SDK writes itself:

```
gen_ai.operation.name    gen_ai.input.messages
gen_ai.agent.name        gen_ai.output.messages
gen_ai.agent.version     gen_ai.tool.call.arguments
gen_ai.tool.name         gen_ai.tool.call.result
```

A value set with `attributes` or `setAttribute()` belongs to that one span.
To decide a whole request by a value, such as a customer ID, use
`contextAttributes` or `setContextAttributes()`.

**setContextAttributes().** Use it for a value you learn partway through a
request. It works only on the active span, and only on a span the SDK opened.
Spans started after the call get the new pairs. Spans started before keep what
they had. A value must be a string, number, or boolean. Keys the SDK owns,
`convergent.*` keys, and session keys are ignored with a warning. If none of
the pairs you pass can be used, the span is unmarked and any attribute filter
withholds it.

**inputArtifact() and outputArtifact().**

```ts
inputArtifact(source: string | Uint8Array, options?: { filename?: string; mediaType?: string; origin?: ArtifactOrigin }): void
```

`source` is a local path, or the bytes as a `Uint8Array` or `Buffer`.
`origin` says who supplied the file: `"user"`, `"agent"`, `"model"`, `"tool"`,
or `"external"`. Files can be recorded only on spans the SDK opened. The SDK
uploads whatever path you pass, so check a path that a model or a user chose
before you pass it.
[Attach files to spans](../../../docs/attach-files.md) explains how to use
them, the limits, and how uploads work.

### currentSpan()

```ts
currentSpan(): SpanHandle
```

**Returns:** a [handle](#the-span-handle) on the active span, including a span
another OpenTelemetry library opened. Takes no arguments. The wrapped functions
from `observe()`, `agent()`, and `tool()` give no handle, so use this inside
them to record content.

It never returns `undefined` and never throws. With no valid active span, or
before setup, its methods do nothing and its IDs are `undefined`.

On another library's span, the handle reads `gen_ai.operation.name` to decide
whether `setInput()` and `setOutput()` write tool keys or message keys. Without
that attribute it writes message keys. `inputArtifact()`, `outputArtifact()`,
and `setContextAttributes()` log a warning once and record nothing there.

### currentTrace()

```ts
currentTrace(): TraceRef | undefined
```

**Returns:** a `TraceRef` for the active span, or `undefined` when there is no
valid active span. Takes no arguments. It works before setup and on a span
the sampler dropped.

#### TraceRef

| Field | Type | Meaning |
| --- | --- | --- |
| `traceId` | `string` | the trace's ID, lowercase hex |
| `spanId` | `string` | the active span's ID, lowercase hex |

### flush()

```ts
flush(options?: FlushOptions): Promise<FlushResult>
```

Sends the spans waiting in each Convergent destination, then waits for sends
already in progress. Call it after the traced work has finished. It never
throws.

```ts
const drained = await flush({ timeoutMs: 5000 }); // { ok, elapsedMs }
```

**Returns:** a [FlushResult](#flushresult).

**Options:**

| Option | Type | Default | Meaning |
| --- | --- | --- | --- |
| `timeoutMs` | `number` | `5000` | how long to wait for spans |
| `uploads` | `boolean` | `false` | also wait for the files from `inputArtifact()` and `outputArtifact()` to upload |
| `uploadTimeoutMs` | `number` | `60000` | how long to wait for uploads, after the spans. Separate because a large file takes longer than spans |

An invalid `timeoutMs` or `uploadTimeoutMs` logs a warning and uses the
default.

Every destination is flushed even when another one fails. For a `File`
destination, `flush()` also syncs the file to disk. Before setup, or after an
`invalid_config`, `flush()` does nothing and returns `{ ok: true, elapsedMs: 0 }`.

#### FlushResult

| Field | Type | Meaning |
| --- | --- | --- |
| `ok` | `boolean` | every destination finished within `timeoutMs`. With `uploads: true`, also no upload is left |
| `elapsedMs` | `number` | how long it took, in whole milliseconds |

A failure or a timeout gives `ok: false` and logs a warning the first time.

`ok` does not prove delivery. If the receiver answers HTTP 400, a batch that
`flush()` sends gives `ok: false`. A batch that was already sent in the
background gives `ok: true`, and the failure shows only as a warning. Spans
dropped earlier from a full queue are not counted. The result has no count of
pending or dropped spans, because OpenTelemetry for Node does not expose them.

Keep in mind:

- A span that has not ended is not sent yet. Finish and await the traced work
  before you call `flush()`. A call inside an active span logs a warning once.
- The timeout limits how long `flush()` waits. It does not cancel a request
  already sent, and Node can stay alive until that request ends.
- With your own provider, see
  [Flush and shut down](../opentelemetry.md#flush-and-shut-down).

#### Where to call flush()

| Runtime | What to do |
| --- | --- |
| Script | `await flush()` after the traced work. Use `flush({ uploads: true })` if the script records files |
| HTTP server | flush in your `SIGTERM` handler, after the server stops taking requests |
| Serverless function | flush in an outer handler, after the traced function ends |
| Your own provider | also call `provider.shutdown()` when the app exits |

When a script runs out of work, the SDK also flushes once on Node's
`beforeExit` event and then shuts down its processor. File uploads still
waiting are dropped, not waited for. This does not happen after
`process.exit()`, a crash, `SIGKILL`, or a platform freezing the process.

The SDK installs no signal handler. For an HTTP server, handle `SIGTERM`
yourself:

```ts
process.once("SIGTERM", () => {
  server.close(async () => {
    // close() stops intake and calls back after active requests end.
    await flush();
    process.exit(0);
  });
});
```

For a serverless function, flush in an outer handler. This also runs when the
traced work throws.

```ts
const answer = agent({ name: "order_support" }, async (event) => respond(event));

export async function handler(event) {
  try {
    return await answer(event);
  } finally {
    await flush();
  }
}
```

We tested these recipes locally. We have not yet checked on AWS Lambda or
Vercel whether the `finally` flush finishes before the platform freezes the
instance, what happens to a send in progress at a freeze, or whether
`beforeExit` runs there.

### ContextAttributes

```ts
type ContextAttributes = Readonly<Record<string, string | number | boolean>>;
```

The pairs that `contextAttributes` and `setContextAttributes()` take. Exported
so you can type your own code.

### SpanAttributeRules

```ts
type SpanAttributeRules = Readonly<Record<string, FilterValue | readonly FilterValue[]>>;
type FilterValue = string | number | boolean;
```

What `requireSpanAttributes` and `rejectSpanAttributes` take: each attribute
name maps to one value or an array of values.

### ArtifactOrigin

```ts
type ArtifactOrigin = "user" | "agent" | "model" | "tool" | "external";
```

What `origin` takes on `inputArtifact()` and `outputArtifact()`. The options
object is exported as `ArtifactOptions`.

## Destinations

Places to write spans besides Convergent, passed to
`init({ destinations: [...] })`. They need no API key and make no network
request. Constructing one only checks its arguments. `init()` opens the file.
[Local files](../../../docs/local-files.md) explains how to use them.

`Destination` is the type `File | Console`.

### File

```ts
new File(path: string, options?: FileOptions)
```

Writes every span the filters keep to `<path>/<filename>`, one OTLP/JSON line
per span.

| Argument | Type | Default | Meaning |
| --- | --- | --- | --- |
| `path` | `string` | required | the directory to write in |
| `filename` | `string` | `"spans.jsonl"` | the file's name. Must be a bare name with no directory part |
| `mode` | `number` | `0o600` | the file's permission bits, `0` to `0o777` |

A `path` that is not a nonempty string, or a `filename` or `mode` of the wrong
type, throws a `TypeError`. A `filename` with a directory part, or a `mode`
outside `0` to `0o777`, throws a `RangeError`.

### Console

```ts
new Console(options?: ConsoleOptions)
```

Writes every span the filters keep to stdout or stderr, in the same format as
a `File`.

| Option | Type | Default | Meaning |
| --- | --- | --- | --- |
| `stream` | `"stdout" \| "stderr"` | `"stdout"` | where the spans go |
| `pretty` | `boolean` | `false` | indent each span for reading |

A `pretty` that is not a boolean throws a `TypeError`. Any other `stream`
throws a `RangeError`.

## OpenTelemetry

### tracerProvider()

```ts
tracerProvider(): TracerProvider
```

**Returns:** the OpenTelemetry `TracerProvider` the SDK records through, for a
library that takes a provider as an argument. Takes no arguments.

After `init()`, it is the provider `init()` created. With a
`ConvergentSpanProcessor`, it is OpenTelemetry's global provider. Before setup
succeeds, it returns a provider that sends nothing and logs a warning once.
[Pass the provider yourself](../opentelemetry.md#pass-the-provider-yourself)
shows an example.

### ConvergentSpanProcessor

```ts
new ConvergentSpanProcessor(options?: ProcessorOptions)
```

A span processor you add to your own tracer provider instead of calling
`init()`. [OpenTelemetry](../opentelemetry.md) explains the setup.

```ts
const convergentProcessor = new ConvergentSpanProcessor({ release: "2026.10.06.1" });
const provider = new NodeTracerProvider({
  spanProcessors: [existingProcessor, convergentProcessor],
});
provider.register();
```

**Options:** the same as [init()](#init), with the same environment variables,
except `debug` and `destinations`. An API key is required.

| Option | Type |
| --- | --- |
| `apiKey` | `string` |
| `endpoint` | `string` |
| `release` | `string` |
| `agents` | `string[]` |
| `requireSpanAttributes` | [`SpanAttributeRules`](#spanattributerules) |
| `rejectSpanAttributes` | [`SpanAttributeRules`](#spanattributerules) |
| `strict` | `boolean` |

**Methods:**

| Method | What it does |
| --- | --- |
| `forceFlush(): Promise<void>` | sends Convergent's waiting spans. Rejects when an export fails |
| `shutdown(): Promise<void>` | flushes and stops Convergent's part. Safe to call again. Rejects when an export fails |

The provider calls `onStart()` and `onEnd()` itself.

An invalid configuration logs a warning and leaves the processor inactive.
With strict mode on, the constructor throws instead.

## Environment variables

Each `CONVERGENT_*` variable fills in its option when you leave the option out.
[Configuration](../configuration.md#convergent-variables) lists them and the
values each one takes.
