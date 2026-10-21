# Changelog

Each released version has a `## <version>` section here, and the TypeScript
release workflow publishes that section as the GitHub Release notes. The
newest section may describe a version whose tag does not exist yet.

## 0.1.0

The first public release of the TypeScript SDK on npm. It needs Node.js 22.15
or newer and `@opentelemetry/api` 1.9 installed beside it. Both `import` and
`require` load the same ES module.

### Record work

- `agent()`, `tool()`, `observe()`, and `span()` record agent runs, tool calls,
  model calls, and other operations as nested OpenTelemetry spans. `agent()`
  also takes `model`, `systemInstructions`, and `tools`.
- `currentSpan()` returns a handle for the active span, also for spans that
  other OpenTelemetry instrumentation opened. `setInput()` and `setOutput()`
  record what a span read and returned. `setAttribute()` records any other
  attribute that the SDK does not reserve. `currentTrace()` returns the active
  `{ traceId, spanId }`.
- `inputArtifact()` and `outputArtifact()` record a file that a span read or
  produced, from a path or bytes. The SDK hashes the file, lists it on the span
  by its `sha256:` digest, and uploads it in the background. A file that the
  workspace already holds is not sent again.
- `session(id, callback)` groups separate turns of one conversation under one
  session ID.
- An exception inside a helper sets the span status to error, records only the
  error's class name, and is rethrown unchanged.

### Configure

- `init()` sends spans to Convergent as OTLP over HTTP. It reads
  `CONVERGENT_API_KEY`, `CONVERGENT_ENDPOINT`, `CONVERGENT_RELEASE`,
  `CONVERGENT_STRICT`, and `CONVERGENT_DEBUG` for each option you omit.
- A bad configuration logs one warning and returns `enabled: false` with a
  `reason`. With `strict` on, `init()` throws the problem instead, so a
  deployment can stop at startup.
- `debug` shows the SDK's warnings and OpenTelemetry's export messages when the
  application has no diagnostic logger.
- `version` gives the package version.

### Local destinations

- `File` and `Console` write finished spans to a local file or to stdout or
  stderr, as OTLP/JSON lines. They need no API key and make no network
  request. `CONVERGENT_SPANS_DIR` and `CONVERGENT_TRACES_EXPORTER=console` add
  them from the environment. The file format is the same for every Convergent
  SDK.

### Filters

- `agents`, `requireSpanAttributes`, and `rejectSpanAttributes` choose which
  spans Convergent receives. They apply to every Convergent destination. The
  application's other exporters still get every span.

### Existing OpenTelemetry setups

- `ConvergentSpanProcessor` adds Convergent to a tracer provider that the
  application builds and owns.
- `tracerProvider()` returns the provider for libraries that take one, such as
  Mastra.

### Check and flush

- `check()` reports the local configuration and asks the server once whether
  it accepts the API key. It always resolves.
- `flush()` exports queued spans and never throws. `flush({ uploads: true })`
  also waits for file uploads.
