# Convergent TypeScript SDK

The Convergent SDK for TypeScript and JavaScript applications on Node.js. It
records agent runs, model calls, and tool calls as OpenTelemetry spans and sends
them to Convergent. Requires Node.js 22.15 or newer. Both `import` and `require`
load the same ES module.

## Build from source

The package is built from the `typescript/` directory of the SDK repository
(`sdks/typescript/` in the monorepo). It has its own dependencies and lockfile.

```sh
pnpm --ignore-workspace install
pnpm build
```

`pnpm pack` builds the package before creating the tarball. `pnpm test` installs
that tarball into a temporary application and checks that `import` and `require`
share one configuration and record one connected trace.

Set `CONVERGENT_API_KEY` and `CONVERGENT_RELEASE`, then run the quickstart:

```sh
node examples/quickstart/main.mjs
```

The [quickstart](https://github.com/convergent-technologies/convergent-sdk/blob/main/typescript/examples/quickstart/main.mjs)
records one agent run that calls a tool and a scripted model, prints the trace
ID, and flushes. It makes no model provider request.

## Record work

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

What each call does:

```
span(options, callback)   starts a span, runs callback(handle) inside it, ends the span
                          when the callback returns or its Promise settles
observe(options, fn)      returns fn wrapped in span(); keeps arguments, this, the return
                          type, and whatever fn throws
agent({ name }, fn)       observe() with operation "agent_run"; name is the agent's identity
tool({ name? }, fn)       observe() with operation "tool_call"; name defaults to fn.name
```

A span is a child of whatever span is active when it starts, so the tool and
model spans above sit under the agent run. The handle has `setInput`,
`setOutput`, `setToolCallId`, `setAttribute`, `inputArtifact`, `outputArtifact`,
`traceId`, and `spanId`. When tracing is off, the handle does nothing and its IDs
are `undefined`. When the sampler drops a span, its handle keeps the IDs and its
setters do nothing.

What the SDK writes on the span:

| You write | The span carries |
| --- | --- |
| `operation: "agent_run"` | name `invoke_agent <name>`, `gen_ai.operation.name=invoke_agent`, `gen_ai.agent.name`, `gen_ai.agent.version=<release>` |
| `operation: "tool_call"` | name `execute_tool <name>`, `gen_ai.operation.name=execute_tool`, `gen_ai.tool.name`, `gen_ai.tool.type=function` |
| `operation: "model_call"` | `gen_ai.operation.name=chat` |
| `model: m` on any operation | `gen_ai.request.model=m` |
| any other operation | `gen_ai.operation.name` set to that string |
| `setInput(x)` on a tool span | `gen_ai.tool.call.arguments`: `x` as is if a string, else compact JSON |
| `setInput(x)` elsewhere | `gen_ai.input.messages`: a JSON message list; a bare value becomes one user text message |
| `setOutput(x)` | the same, as `gen_ai.tool.call.result` or `gen_ai.output.messages` with role `assistant` |
| `setToolCallId(id)` | `gen_ai.tool.call.id`, preserving the supplied string of 1–256 Unicode characters, including whitespace |
| `setAttribute(key, value)` | the attribute, unless the key is reserved by the SDK or starts with `convergent.` |
| `inputArtifact(file)` | `convergent.artifacts.input`: a JSON list with one `{digest, size_bytes, media_type, filename, origin}` entry per file |
| `outputArtifact(file)` | the same, as `convergent.artifacts.output` |

`agent()` also accepts `systemInstructions` and `tools` to describe the
configured agent. `tool()` takes only `name`, `attributes`, and
`contextAttributes`.

To trace a stream, consume it inside the callback. A returned iterator ends the
span when the callback returns.

```ts
const text = await span({ name: "gpt-5.5", operation: "model_call" }, async (call) => {
  let output = "";
  for await (const chunk of stream) output += chunk;
  call.setOutput(output);
  return output;
});
```

## Record files

```ts
await span({ name: "summarize_invoices", operation: "tool_call" }, async (call) => {
  call.inputArtifact("./invoices.csv");
  const report = await summarize("./invoices.csv");
  call.outputArtifact(report, { filename: "summary.pdf", origin: "tool" });
  return report;
});
```

`inputArtifact(source, { filename?, mediaType?, origin? })` records a file the
span read, such as a document given to the agent, content handed to a model, or
a file a tool loaded. `outputArtifact()` takes the same arguments and records a
file the span produced, such as the agent's deliverable. `source` is a local
path or the bytes as a `Uint8Array` or `Buffer`. `filename` defaults to the
last part of the path. Any file is accepted, and `mediaType` defaults to
`application/octet-stream`. A filename longer than 1,024 characters is logged
once and left off the listing; the file is still recorded.

`origin` says who supplied the file, and the span lists it only when you pass
one of these values. Any other value is logged once and left off; the file is
still recorded.

| `origin` | Who supplied the file |
| --- | --- |
| `user` | an end user handed it in |
| `agent` | the agent's own code wrote it |
| `model` | a model generated it |
| `tool` | a tool produced or fetched it |
| `external` | any other system, such as storage, the web, or another service |

The call copies the file and hashes it with SHA-256 before it returns, so a
later change to the file or the buffer does not reach Convergent. It reads a
path synchronously, which takes tens of milliseconds for a 16 MiB file. It
reads only regular files, not pipes or devices. The SDK uploads whatever path
you pass, so do not pass a path a model or a user chose without checking it. The
span lists each file's digest, size, media type, filename, and origin as one
JSON list per role. The digest is `sha256:` followed by the 64 lowercase hex
characters of the file's SHA-256 hash.

The upload runs in the background after the span ends. The SDK sends the copy
to `<endpoint>/v1/artifacts/<digest>`, zstd-compressed unless it is already a
compressed image or archive, with the filename so the server can identify the
file's type. It first asks whether your workspace already holds the file, and
sends no bytes when it does. A file the server stored or refused (413 or 422) is
not sent again by this process. A file whose upload ran out of retries or hit an
error, a bare 404 included, is sent again when a later span lists it. If the
upload route says file uploads are off for your organization (a 403 whose
`details.reason`, or `X-Convergent-Reason` header, is
`artifact_uploads_disabled`), the SDK pauses uploading and listing files for 10
minutes, then asks again. A 401 stops them for the rest of the process. Uploads
send `User-Agent: convergent-sdk-typescript/<version>`.

A span lists at most 100 files, and spans that have not ended hold at most
128 MiB of copies across the process. A file past either limit, one larger
than 16 MiB, one that cannot be read, one given as an `http://` or `https://`
URL, or one whose `mediaType` is not printable ASCII of at most 255 characters
is logged once and counted in
`convergent.artifacts.dropped`. At most 64 MiB of files wait for upload. A
file that arrives when that queue is full is logged and not sent.

Pending uploads do not keep the process alive. Call `flush({ uploads: true })`
before a short-lived process exits.

## Sessions and active handles

Use `session(id, callback)` to group separate conversation turns. Each turn can
have its own trace ID while sharing one session ID.

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

`session()` trims the ID and accepts 1–128 Unicode characters. The first invalid
ID in a process logs a warning. Any invalid ID runs the callback in the existing
context. `session()` preserves the callback's return value and exceptions. Nested
sessions use the innermost ID. Asynchronous work keeps the session in which it
was created, using the context manager that `init()` registers. A session creates
no span and adds nothing to OpenTelemetry baggage. Session IDs travel with every
span, so do not put emails or other personal data in them.

Spans started in a session receive `convergent.session.id` on their Convergent
export copy. Original spans and resources stay unchanged. An existing
string-valued session attribute on a span takes precedence.

`currentSpan()` returns a handle for the active span, including spans created by
other OpenTelemetry instrumentation. It reads the span's `gen_ai.operation.name`
to choose tool or message content keys. A foreign span without that attribute
uses message keys. With no valid active span or no SDK configuration, its methods
do nothing and its IDs are `undefined`. Files can be recorded only on spans the
SDK opened. On another library's span, `inputArtifact` and `outputArtifact` log a
warning once and record nothing. `currentTrace()` returns a `TraceRef` containing
`{ traceId, spanId }` in lowercase, or `undefined` when the active context is
invalid. It also works with a valid nonrecording span and before SDK configuration.
The first invalid tool-call ID logs a warning without the rejected value.

Run the [sessions example](https://github.com/convergent-technologies/convergent-sdk/blob/main/typescript/examples/sessions/main.mjs)
after building and setting the same environment variables as the quickstart:

```sh
node examples/sessions/main.mjs
```

## Configure

`init({ apiKey?, endpoint?, release?, strict?, debug?, destinations? })` reads
an environment variable for each option you omit. The filter options are described in
[Filter spans](#filter-spans).

| Option | Environment variable | Default |
| --- | --- | --- |
| `apiKey` | `CONVERGENT_API_KEY` | required unless a local destination is set |
| `endpoint` | `CONVERGENT_ENDPOINT` | `https://ingest.convergent.dev` |
| `release` | `CONVERGENT_RELEASE` | required |
| `strict` | `CONVERGENT_STRICT` | `false` |
| `debug` | `CONVERGENT_DEBUG` | `false` |

The two flag variables take `1`, `true`, `yes`, or `on` to turn the flag on,
and `0`, `false`, `no`, or `off` to leave it off, in any case. An empty value
counts as unset. Any other value, or a flag option that is not a boolean, is an
invalid configuration. A flag is on when either the option or its variable
turns it on.

With an API key, spans go to `<endpoint>/v1/traces` as OTLP protobuf, gzipped, with the key as a
Bearer header. SDK-owned providers use the release as `service.version`; agent
runs use it as `gen_ai.agent.version`. Owned providers include OpenTelemetry's
default resource attributes and a random `service.instance.id` for each
process, and honor
`OTEL_SERVICE_NAME` and `OTEL_RESOURCE_ATTRIBUTES`. The configured release wins
over an environment `service.version`.

`init()` returns `{ enabled, release, agents, destinations, mode, reason,
requireSpanAttributes, rejectSpanAttributes }`. Every field is always present.
An unset field is `null` or `[]`, and `mode` is `"owned"` while tracing is
disabled. The filter fields are described in
[Filter spans](#filter-spans). With a bad
configuration it logs once and returns `enabled: false` with
`reason: "invalid_config"`. The warning names the first problem found. With
strict mode on, the same problem is thrown instead, before tracing is set up,
so a deployment can stop at startup. An option of the wrong type throws a
`TypeError`. A value that cannot work, such as a missing key or release, an
unusable endpoint, or an unreadable flag variable, throws an `Error`. Strict
mode affects only that check. After a successful call, a later call keeps the
first configuration and never throws. Its `debug` still applies.

`import { version } from "convergent-sdk"` gives this package's version, or
`"0.0.0"` when the source runs without a build.

`debug` makes Convergent's warnings visible when the application has not set
an OpenTelemetry diagnostic logger. It then installs `DiagConsoleLogger` at the
`INFO` level, which also shows OpenTelemetry's export failure and retry
messages. It never replaces a logger the application set, so with your own
logger, choose its level yourself. OpenTelemetry's `DEBUG` level prints the
spans being exported, so set it only where logs may hold trace content.

`init()` registers an OpenTelemetry tracer provider for the process. If the
registration is refused, the result is `reason: "existing_provider"`. Two
things cause that: another library already registered a provider, or two
different `@opentelemetry/api` versions are loaded. `npm ls @opentelemetry/api`
is the first check; a bundled copy does not appear there.

`tracerProvider()` returns the public OpenTelemetry `TracerProvider` interface,
for libraries that take one explicitly, such as [Mastra](https://github.com/convergent-technologies/convergent-sdk/blob/main/typescript/examples/mastra/README.md). Before successful configuration it
returns a provider that exports nothing. Keep your application provider to call
provider-wide methods such as `forceFlush()` and `shutdown()`.

Warnings go through OpenTelemetry's diagnostics. Use `debug`, or set your own
logger before `init()` and leave `debug` off:

```ts
import { diag, DiagConsoleLogger, DiagLogLevel } from "@opentelemetry/api";

diag.setLogger(new DiagConsoleLogger(), DiagLogLevel.WARN);
```

OpenTelemetry's own settings apply unchanged, including `OTEL_BSP_*` for
batching. OpenTelemetry also adds the headers in `OTEL_EXPORTER_OTLP_HEADERS`
and `OTEL_EXPORTER_OTLP_TRACES_HEADERS` to every Convergent request. When either
is set, the SDK warns once and names the variable, never its value. The
Convergent API key still wins over an `authorization` entry there.

If the server refuses a batch as too large (HTTP 413), the SDK warns once. That
batch is lost and later batches are still sent. Lower
`OTEL_BSP_MAX_EXPORT_BATCH_SIZE` to send fewer spans per request. A single span
that is too large on its own needs smaller content.

## Write spans to a file or the console

`File` and `Console` are destinations: places the SDK writes finished spans.
They need no API key and make no network request. Pass them to `init()`. With
an API key they are added to Convergent. `ConvergentSpanProcessor` takes no
destinations and needs an API key, because the application owns its other
exporters.

```ts
import { Console, File, init } from "convergent-sdk";

init({
  release: "2026.10.06.1",
  destinations: [new File("./traces"), new Console({ stream: "stderr" })],
});
```

| Destination | Writes |
| --- | --- |
| `new File(path, { filename = "spans.jsonl", mode = 0o600 })` | `<path>/<filename>`, one OTLP/JSON `ExportTraceServiceRequest` per line, one span per request |
| `new Console({ stream = "stdout", pretty = false })` | the same lines to stdout or stderr. `pretty` indents each span. |

Both write every recorded prompt and output. In a hosted runtime, console output
usually reaches the platform's log service.

`CONVERGENT_SPANS_DIR=<dir>` adds `new File(<dir>)`.
`CONVERGENT_TRACES_EXPORTER=console` adds a `Console` when none is configured.
Both add to `destinations`. The exporter value ignores case, and a blank value
is ignored. Any other value makes the configuration invalid. Two names for the
same file write it once. `status.destinations` lists the destinations that
started: `convergent`, `file:<absolute file path>` such as
`file:/srv/app/traces/spans.jsonl`, and `console:<stream>`.

The file format is the same for every Convergent SDK: hex trace and span IDs,
integer enums, 64-bit numbers as strings, and default values left out. A constructor rejects a
filename with a directory part or a mode outside `0..0o777`, and throws. Setup
opens the file, so a file that cannot be opened is skipped with a warning while
the other destinations keep working. In strict mode `init()` throws that error
instead. If nothing is left, `init()` returns
`reason: "invalid_config"`. A failed write is an export failure and never throws
into your code. `flush()` waits for written spans and calls `fsync` on the file.

On Linux and macOS the file is opened with `O_NOFOLLOW`, so a symbolic link at
the file itself is refused. Links in the parent directories are followed. The
SDK also refuses a target that is not a regular file or that another user owns.
The mode applies to a new file and to an existing one. If the filesystem
refuses the change, the SDK warns and keeps the existing permissions. On Windows
there is no `O_NOFOLLOW`, and access depends on the file's ACLs, not on `mode`.

Each batch is written as one append on every platform. Several processes can
share one local file without splitting a line, unless a write is cut short and
finished with a second append. A network filesystem gives no such guarantee.

## Check the setup

`check()` reports the local configuration, then asks the server once whether
it accepts the API key. It always resolves.

```ts
import { check } from "convergent-sdk";

const report = await check();
console.log(String(report));
```

`report.ok` is true when tracing is on and the server accepted the key. A
working file-only setup has `report.status.enabled` true and `report.ok` false,
because there is no key to check. `report.roundTrip` is `ok` or the reason the
request failed: `no_credentials`, `http_<status>`, `not_a_check_response`,
`bad_json`, `response_too_large`, `TimeoutError`, a Node error code such as
`ECONNREFUSED`, `connection_failed` when Node gives no code, or `unexpected`.
The request goes to `<endpoint>/v1/check?release=<release>`. It
refuses redirects, stops after 5 seconds, and reads at most 1 MiB. The report
shows server text cut to 500 characters, with `...` added when cut. Control,
format, and other unprintable characters become spaces, so server text cannot
rewrite the terminal. It
never shows the key or a response body. A successful check does not show that a
trace was stored.

## Filter spans

These options choose which spans Convergent receives. They apply to every
Convergent destination: the network, `File`, and `Console`. The application's
other exporters keep every span, because the filters are not a sampler.

```ts
init({
  agents: ["order_support"],
  requireSpanAttributes: { "customer.tier": ["paid", "trial"] },
  rejectSpanAttributes: { "customer.id": "internal-test" },
});
```

`agents` names the agents Convergent may see. A span that starts with a
`gen_ai.agent.name` in the list is sent, with its unnamed descendants. A
span naming another agent is withheld, and so are its unnamed children. A
named agent nested inside a withheld one starts its own allowed branch. A
local root with no agent name is withheld. Under a parent from another
process, a span is kept when it looks like model work: it has a `gen_ai.*`
attribute, or its tracer is `convergent.sdk`, `pydantic-ai`, `openinference`,
or `litellm`, or a dotted child of one. An agent that spans two processes
needs `agents` in each one. An empty list sends nothing. Leaving `agents` out
applies no agent rule.

The agent rule is decided when a span starts. A name added after the span
starts does not change the decision, and the SDK never waits for a parent or
root span to finish. Mastra's bridge adds the agent name only when a span
ends, so leave `agents` unset with the bridge.

`requireSpanAttributes` maps attribute names to the values a span must hold.
`rejectSpanAttributes` maps names to values that withhold a span. A single
value names one accepted value, and an array matches any listed value. An
empty require array matches nothing, and an empty reject array rejects
nothing. Reject runs first. Then every require key must match, and a missing
key never matches. Values compare by type and value, so `true` never matches
`1`. `CONVERGENT_REQUIRE_SPAN_ATTRIBUTES` and
`CONVERGENT_REJECT_SPAN_ATTRIBUTES` take the same rules as JSON when the
option is absent.

Each key is read from the first source that holds it: the request marks
described below, then the span's own attributes, then the resource
attributes. A `convergent.attributes.<key>` attribute written on the span also
counts as a mark. A request mark replaces one the span held when it started,
and a value written after the span starts replaces the request mark. A later source never replaces an earlier one. The attribute rules
run after OpenTelemetry finishes loading asynchronous resource attributes, so
a resource value that arrives late can still reject a span. A resource
detector that fails leaves its key missing, and a missing key never matches a
reject rule. Use a require rule when a missing value must withhold the span.

A value set through `attributes` or `setAttribute()` belongs to that one span.
Its children are judged on their own attributes. Use `contextAttributes` for a
value that should decide a whole request, such as a customer ID.

A malformed `agents` list or filter disables Convergent export. That includes
an empty rule object such as `requireSpanAttributes: {}`, a blank or padded key,
a `null` value, an agent list over 256 names or with a name over 512
characters, and filter JSON that does not parse. `init()` then returns `enabled: false` with
`reason: "invalid_config"`, and a `ConvergentSpanProcessor` stays inactive.
With strict mode on, the problem is thrown instead, as a `TypeError` for the
wrong kind of value and an `Error` for a value that cannot work. A successful
`init()` echoes the running rules in `agents`, `requireSpanAttributes`, and
`rejectSpanAttributes`, sorted, with every value in an array. The `check()`
report prints the attribute rules on a `filters` line, reject first.

Mark a request with `contextAttributes`. The pairs apply to the span and to
every span started inside it, including other libraries' spans. They are sent
as `convergent.attributes.<key>` on Convergent's copy, and they stay in the
process: they never enter baggage. Values are strings, numbers, or booleans.
SDK keys, `convergent.*` keys, and session keys are ignored with a warning.

```ts
const support = agent(
  { name: "order_support", contextAttributes: (customerId: string) => ({ "customer.id": customerId }) },
  async (customerId: string) => {
    currentSpan().setContextAttributes({ "customer.tier": await lookupTier(customerId) });
    return answer(customerId);
  },
);
```

`agent()`, `tool()`, and `observe()` accept an object or a resolver that
receives the wrapped function's arguments by position. `span()` accepts an
object only. Nested calls inherit the outer pairs, and an inner pair wins for
the same key. `setContextAttributes()` works only inside the active span the
SDK opened. Spans started after the call inherit the new pairs, and spans
started before keep what they had. Some inputs leave the marks unresolved: a
resolver that throws, a resolver result or `setContextAttributes()` argument
that is not a plain object, and a resolver result or `setContextAttributes()`
argument whose pairs are all ignored. An unresolved span and its children are
withheld under any attribute rule, even if an inner span names valid pairs. An
async resolver returns a Promise, so it is unresolved too. Look the value up
inside the function and call `setContextAttributes()` instead. In attached mode,
the application's context manager must carry context across `await`.

Spans that pass the agent rule enter OpenTelemetry's batch queue before the
attribute rules run, so rejected spans still take queue slots. The queue holds
2,048 spans per processor by default. When it is full, OpenTelemetry drops new
spans, including spans that would pass the filters. Set
`OTEL_BSP_MAX_QUEUE_SIZE` before setup to change the limit. A larger queue
handles a larger burst and uses more memory, but it does not reserve room for
allowed spans. Agent exclusions happen before the queue.

Numbers compare as JavaScript numbers, so `1` matches `1.0`.
`setContextAttributes()` updates the span's request scope in place, so a
context saved earlier inside that span also sees the new pairs.

## Use an application-owned OpenTelemetry provider

Add `ConvergentSpanProcessor` alongside your existing processor when constructing
the provider. It accepts the same options and environment variables as `init()`,
except `debug` and `CONVERGENT_DEBUG`, which it ignores, and the destination
settings. It requires an API key.
The application registers the provider and owns its context manager, propagator,
resources, and shutdown.

```ts
import { NodeTracerProvider } from "@opentelemetry/sdk-trace-node";
import { ConvergentSpanProcessor, flush } from "convergent-sdk";

const convergentProcessor = new ConvergentSpanProcessor({ release: "2026.10.06.1" });
const provider = new NodeTracerProvider({
  spanProcessors: [existingProcessor, convergentProcessor],
});
provider.register();

// Record application work with Convergent helpers or OpenTelemetry.
await flush();
await provider.shutdown();
```

Construct the processor before the provider, and register the provider globally
before using Convergent helpers. Construction makes no network request and
registers no globals. A private provider that is never registered globally is
outside this setup. If the registered provider does not carry the processor,
helpers record nothing and log one warning. Spans your own tracers
record on the processor's provider still reach Convergent. To check this, the
first helper call after each change of global provider starts one probe span,
`convergent.provider_probe`, on that provider. The SDK never ends it, so no
exporter sends it. Each provider holds at most one such open span.

A successful constructor claims the process configuration, even if the
processor is never added to a provider. That claim is not released, so a later
`init()` or processor stays inactive.

The first successful configuration wins. After attachment, `init()` returns
`enabled: true`, `mode: "attached"`, and `reason: "already_configured"`, with
the processor's release, destinations such as `["convergent"]`, and filter
fields. After successful `init()`, the mode is `"owned"`.
Later processors log one warning and stay inactive. An invalid constructor logs
a safe reason, stays inactive, and allows a later valid attempt. With strict
mode on, it throws instead.

`flush()` and `convergentProcessor.shutdown()` affect only Convergent's destinations.
The application's other exporters remain usable. Processor shutdown is safe to
call again; the application still calls its provider's `shutdown()` when done.
In attached mode, `tracerProvider()` uses OpenTelemetry's global provider and
leaves the application's resource attributes unchanged.

Convergent adds semantic, execution, and session metadata only to its exported
copy. A foreign agent span missing `gen_ai.agent.version` gets the configured
release on that copy. Attributes written by helpers or application code remain
on the original span and are visible to other exporters, including agent options
and content written with `setInput()` or `setOutput()`.

OpenTelemetry JS 2.x has no `addSpanProcessor()`, so the processor goes in the
provider's constructor list. The processor takes no `tracerProvider` option, so
helpers always use the registered global provider. The semantic version and
execution ID go only on spans that have `gen_ai.operation.name` when they start.

Run the [existing OpenTelemetry example](https://github.com/convergent-technologies/convergent-sdk/blob/main/typescript/examples/existing-otel/main.mjs)
after building and setting the same environment variables as the quickstart:

```sh
node examples/existing-otel/main.mjs
```

## Errors and flush

An exception inside a helper sets the span status to error and is rethrown
unchanged. The span records the error's class name and nothing else. No
message, no stack, and nothing from a thrown string. Content you pass to
`setInput`, `setOutput`, and `setAttribute` is sent as given.

```ts
const drained = await flush({ timeoutMs: 5000 }); // { ok, elapsedMs }
```

`flush()` exports the spans in each Convergent destination's batch queue. Then it waits for
exports already in flight, including one the batch timer started before the
call. It uses OpenTelemetry's public `forceFlush()` methods and never throws.
`ok` is true when those methods finished within `timeoutMs`, which defaults to
5000. Every destination is drained even when another one fails. A rejection or a timeout gives `ok: false` and logs a warning the first
time. An invalid `timeoutMs` logs a warning and uses 5000. `elapsedMs` is whole
milliseconds on a monotonic clock. Without a Convergent configuration, for
example after `invalid_config`, `flush()` returns `{ ok: true, elapsedMs: 0 }`
and does nothing.

`ok` is not proof of delivery. With a receiver that answers HTTP 400, a batch
that `flush()` sends gives `ok: false`. A batch the timer had already sent gives
`ok: true`, because the exporter finishes its work and reports the failure only
through a warning. Spans dropped earlier from a full queue are not counted. The
queue of each destination holds 2,048 spans by default and drops new spans when full.
`OTEL_BSP_MAX_QUEUE_SIZE` changes that. The result has no count of pending or
dropped spans, because OpenTelemetry for Node does not expose them.

`flush({ uploads: true })` also waits for the files from `inputArtifact()` and
`outputArtifact()` to upload, for up to `uploadTimeoutMs` after the span drain
(default 60000). `ok` then also requires that no upload is left. The span
timeout is separate because a large file takes longer than a span batch. An
invalid `uploadTimeoutMs` logs a warning and uses 60000.

Some limits apply:

- An open span is not in the queue yet. Finish and await the traced work before
  calling `flush()`. A call inside an active span logs a warning once.
- The timeout limits how long `flush()` waits. It does not cancel a request
  that OpenTelemetry already sent, and Node can stay alive until that request
  ends.
- With an application-owned provider, finish asynchronous resource detection
  before the final flush. The batch timer can take spans from the queue and
  then wait for those resource attributes. Such a batch is neither queued nor
  sent, so `flush()` returns without it. Await
  `resource.waitForAsyncAttributes()` first if your resource has them.

For a script, await `flush()` after the traced work. The SDK also drains once
on `beforeExit` when a script runs out of work, after `init()` or an active
`ConvergentSpanProcessor`. That drain flushes spans and then shuts down
Convergent's processor. File uploads still queued are discarded, not waited
for. Use `flush({ uploads: true })` to wait for them. The
drain does not run after `process.exit()`, a crash, `SIGKILL`, or a platform
freezing the process.

For an HTTP server, handle `SIGTERM` yourself. The SDK installs no signal
handler.

```ts
process.once("SIGTERM", () => {
  server.close(async () => {
    // close() stops intake and calls back after active requests end.
    await flush();
    process.exit(0);
  });
});
```

For a serverless function, flush in an outer handler, after the traced span has
ended. This also runs when the traced work throws.

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

With an application-owned provider, call `provider.shutdown()` when the
application exits. `flush()` never shuts down the application's provider or
its other exporters. `ConvergentSpanProcessor.forceFlush()` and `shutdown()`
reject on a failed export. Repeated `shutdown()` calls share one shutdown.

These recipes ran locally against a test receiver. Hosted checks are still
open: whether AWS Lambda or Vercel lets the `finally` flush finish before it
freezes the instance, what happens to an export in flight at a freeze, and
whether `beforeExit` ever runs there.
