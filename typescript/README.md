# Convergent TypeScript SDK

Records agent runs, model calls, and tool calls from a Node.js application as
OpenTelemetry spans and sends them to Convergent. Requires Node.js 22.15 or newer.
Both `import` and `require` load the same ES module.

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
`setOutput`, `setAttribute`, `inputArtifact`, `outputArtifact`, `traceId`, and
`spanId`. When tracing is off, the handle does nothing and its IDs are
`undefined`.

What the SDK writes on the span:

| You write | The span carries |
| --- | --- |
| `operation: "agent_run"` | name `invoke_agent <name>`, `gen_ai.operation.name=invoke_agent`, `gen_ai.agent.name`, `gen_ai.agent.version=<release>` |
| `operation: "tool_call"` | name `execute_tool <name>`, `gen_ai.operation.name=execute_tool`, `gen_ai.tool.name`, `gen_ai.tool.type=function` |
| `operation: "model_call"` | `gen_ai.operation.name=chat` |
| any other operation | `gen_ai.operation.name` set to that string |
| `setInput(x)` on a tool span | `gen_ai.tool.call.arguments`: `x` as is if a string, else compact JSON |
| `setInput(x)` elsewhere | `gen_ai.input.messages`: a JSON message list; a bare value becomes one user text message |
| `setOutput(x)` | the same, as `gen_ai.tool.call.result` or `gen_ai.output.messages` with role `assistant` |
| `setAttribute(key, value)` | the attribute, unless the key is reserved by the SDK or starts with `convergent.` |
| `inputArtifact(file)` | `convergent.artifacts.input`: a JSON list with one `{digest, size_bytes, media_type, filename, origin}` entry per file |
| `outputArtifact(file)` | the same, as `convergent.artifacts.output` |

`agent()` also accepts `model`, `systemInstructions`, and `tools` to describe the
configured agent.

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
compressed image or archive, with the filename so the server
can identify the file's type. It first asks whether your
workspace already holds the file, and sends no bytes when it does. A file the
server stored or refused (409, 413, or 422) is not sent again by this process.
A file whose upload ran out of retries or hit an error is sent again when a
later span lists it. If the upload route answers 404, file uploads are off for
your organization. The SDK then stops uploading and listing files for the rest
of the process, and does the same after a 401.

A span lists at most 100 files, and spans that have not ended hold at most
128 MiB of copies across the process. A file past either limit, one larger
than 16 MiB, one that cannot be read, one given as an `http://` or `https://`
URL, or one whose `mediaType` is not printable ASCII of at most 255 characters
is logged once and counted in
`convergent.artifacts.dropped`. At most 64 MiB of files wait for upload. A
file that arrives when that queue is full is logged and not sent.

Pending uploads do not keep the process alive. Call `flush({ uploads: true })`
before a short-lived process exits.

## Configure

`init({ apiKey?, endpoint?, release? })` reads an environment variable for each
option you omit.

| Option | Environment variable | Default |
| --- | --- | --- |
| `apiKey` | `CONVERGENT_API_KEY` | required |
| `endpoint` | `CONVERGENT_ENDPOINT` | `https://ingest.convergent.dev` |
| `release` | `CONVERGENT_RELEASE` | required |

Spans go to `<endpoint>/v1/traces` as OTLP protobuf, gzipped, with the key as a
Bearer header. The release becomes `service.version` on every span and
`gen_ai.agent.version` on agent runs.

`init()` returns `{ enabled, release, mode, reason, destinations }`. It never
throws. With a bad configuration it logs once and returns `enabled: false` with
a `reason`. A second call keeps the first configuration.

`init()` registers an OpenTelemetry tracer provider for the process. If the
registration is refused, the result is `reason: "existing_provider"`. Two
things cause that: another library already registered a provider, or two
different `@opentelemetry/api` versions are loaded. `npm ls @opentelemetry/api`
is the first check; a bundled copy does not appear there.

`tracerProvider()` returns the configured provider, for libraries that take one
explicitly, such as [Mastra](https://github.com/convergent-technologies/convergent-sdk/blob/main/typescript/examples/mastra/README.md). Before `init()` succeeds it returns a provider that records nothing.

Warnings go through OpenTelemetry's diagnostics. Set a logger before `init()`:

```ts
import { diag, DiagConsoleLogger, DiagLogLevel } from "@opentelemetry/api";

diag.setLogger(new DiagConsoleLogger(), DiagLogLevel.WARN);
```

OpenTelemetry's own settings apply unchanged, including `OTEL_BSP_*` for
batching and `OTEL_EXPORTER_OTLP_HEADERS`. Remove a header that carries another
collector's credentials before configuring Convergent.

## Errors and flush

An exception inside a helper sets the span status to error and is rethrown
unchanged. The span records the error's class name and nothing else. No
message, no stack, and nothing from a thrown string. Content you pass to
`setInput`, `setOutput`, and `setAttribute` is sent as given.

```ts
const drained = await flush({ timeoutMs: 5000 }); // { ok, elapsedMs }
```

`flush()` drains the batch queue and then waits for the exporter, through
OpenTelemetry's public flush methods. Call it after the traced work has
finished, and before a process that may be frozen or killed exits. `ok` means
the drain completed within the timeout. It does not mean the server stored
every span. OpenTelemetry's queue holds 2,048 spans by default and drops the
oldest when full; `OTEL_BSP_MAX_QUEUE_SIZE` changes that.

`flush({ uploads: true })` also waits for the files from `inputArtifact()` and
`outputArtifact()` to upload, for up to `uploadTimeoutMs` after the span drain
(default 60000). `ok` then also requires that no upload is left. The span
timeout is separate because a large file takes longer than a span batch.
