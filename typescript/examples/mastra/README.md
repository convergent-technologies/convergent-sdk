# Mastra examples

Run the same agent through either Mastra's native OpenTelemetry exporter or its
OpenTelemetry bridge. Both use the pinned Mastra packages in `package.json`.
They call a scripted local model, execute `lookup_order`, and print the answer and
trace ID. They make no model provider request.

The scripted model uses Mastra's testing helper, `MastraLanguageModelV2Mock`.
Replace it with your model configuration in an application.

These commands run from this example's directory in the source checkout. Use
Node 24; these Mastra packages require Node >=22.13.0. Build the SDK first, then
install this example's separate dependencies:

```sh
cd ../..
pnpm --ignore-workspace install
pnpm build
cd examples/mastra
pnpm --ignore-workspace install --ignore-scripts
```

Dependency install scripts are unnecessary for these examples. `--ignore-scripts`
skips protobufjs's optional version-warning script.

Set `CONVERGENT_API_KEY` in your environment and set `CONVERGENT_ENDPOINT` to the
receiver's base URL if needed. Set `CONVERGENT_RELEASE` to your application's git
revision or build number. Run each example in its own process:

```sh
pnpm native
pnpm bridge
```

## Native exporter

[`native.mjs`](./native.mjs) configures Mastra's `OtelExporter` with the receiver's
`/v1/traces` URL, Bearer authentication, and `service.version`. It sends Mastra's
spans directly. It bypasses the Convergent SDK's automatic export attributes.

The native example enables debug logging so failed uploads are visible. Mastra's
shutdown waits for exporters but does not report upload failures to the caller.
A printed trace ID and exit code 0 do not confirm that Convergent received the trace.

## Bridge

[`bridge.mjs`](./bridge.mjs) calls `init()` before creating Mastra. It passes
`tracerProvider()` to `OtelBridge`, so Mastra's spans use the Convergent export path.
It waits for Mastra's pending work before calling the SDK's `flush()`.

Mastra adds the agent name when a span ends. The SDK's `agents` filter decides
when a span starts, so it cannot use that name. Leave `agents` unset with the
bridge. The native exporter sends without the SDK, so the SDK's filters and
session metadata do not apply to it.

With these pinned packages, model spans omit `gen_ai.input.messages`. Stored
model input messages are therefore empty in both paths. Model output, token
usage, and tool arguments and results are recorded. The SDK quickstart separately
shows explicit prompt capture.

Both paths use Mastra's content and exception capture settings. The Convergent
helper policy that omits exception messages and stacks applies to spans created
by Convergent helpers. Review Mastra's capture and sensitive-data filtering
settings before using real inputs.

See [Mastra's OpenTelemetry integration documentation](https://mastra.ai/integrations/observability/opentelemetry)
for its exporter and bridge configuration.
