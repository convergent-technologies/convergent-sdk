What changed in each version of the TypeScript `convergent-sdk`, newest first.
The top section can describe a version that is not on npm yet.

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
