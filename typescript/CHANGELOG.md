What changed in each version of the TypeScript `convergent-sdk`, newest first.
The top section can describe a version that is not on npm yet.

## 0.1.1

- Breaking: `setInput()`, `setOutput()`, and `tools` drop a value that is not portable JSON, and the first one logs a warning. Before, `JSON.stringify` changed it silently: `NaN` became `null`, a `Map` became `{}`, and a value it could not encode became its `String()` text. Portable JSON is `null`, booleans, finite numbers, strings, and arrays and plain objects of these. A property set to `undefined` is still left out, but an array holding `undefined` or a hole, which became `null` before, is now dropped. So is an object with a function as a property, such as a tool definition that carries its `execute` function, which `JSON.stringify` used to write without the function; pass the definition without its functions. Convert a `Date` or class instance first, such as `date.toISOString()`. Strings are recorded as before.
- Breaking: an empty or blank `model` is no longer recorded as `gen_ai.request.model`, and the first one logs a warning.
- Breaking: a span whose callback throws or rejects, aborts included, no longer carries the output `setOutput()` recorded on it, so a failed call never ships a partial answer as its result. The input and the span's status are recorded as before. If you read `gen_ai.output.messages` or `gen_ai.tool.call.result` from a failed span, record it under a key of your own with `setAttribute()`, for example `app.partial_output`. Convergent does not show that key as the span's output. On a span another library opened, `setOutput()` still writes at once.
- Span exports open a fresh connection after 55 seconds idle, before managed ingest closes idle connections at 60 seconds. Exports closer together keep reusing their connection.
- Every request to Convergent, span exports and `check()` included, starts its `User-Agent` with `convergent-sdk-typescript/<version>`, not only file uploads.
- `FlushResult` has a new `dropped` field: how many spans were dropped because the send queue was full since the last `flush()`.
- A span name outside 1 to 128 characters, or an operation the SDK does not know, logs a warning once. The span is still recorded, as before.
- Aborted calls no longer show up as errors. A call that rejects with an `AbortError` now leaves its span status unset. No action needed.
- Spans the SDK sends, to Convergent and to the local `File` and `Console` destinations alike, no longer carry `convergent.semantic.version` or `convergent.execution.id`. Convergent read neither one, and exporters you add to your own OpenTelemetry provider never received them. If you read either label from `File` or `Console` output, use the span's trace id, which held the same value.
- Every span the SDK sends to Convergent, and to the local `File` and `Console` destinations, carries `convergent.sdk.version` set to `typescript/<version>`, so Convergent can tell which SDK release sent it. Exporters you add to your own OpenTelemetry provider never receive it.

## 0.1.0

- First public release on npm. Needs Node.js 22.15 or newer and `@opentelemetry/api` 1.9.
- Record agent runs, tool calls, and model calls with `agent()`, `tool()`, `observe()`, and `span()`.
- Record what a span read and returned with `setInput()` and `setOutput()`.
- Attach files to a span with `inputArtifact()` and `outputArtifact()`.
- Group the turns of one conversation with `session(id, callback)`.
- Configure with `init()` options or `CONVERGENT_*` environment variables.
- Write spans to a local file or the console with `File` and `Console`.
- Choose which spans are sent with `agents`, `requireSpanAttributes`, and `rejectSpanAttributes`.
- Add Convergent to your own OpenTelemetry provider with `ConvergentSpanProcessor`.
- Check your setup with `check()`, and send pending spans with `flush()`.
