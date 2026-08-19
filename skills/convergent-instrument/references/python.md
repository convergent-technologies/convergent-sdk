# Python instrumentation

Use this reference after the target agent and model client are known.

## Read current APIs

When internet access exists, read the matching public SDK page before editing:

- [Python SDK](https://github.com/convergent-technologies/convergent-sdk/blob/main/python/docs/index.md)
- [Instrumentation](https://github.com/convergent-technologies/convergent-sdk/blob/main/python/docs/instrument.md)
- [Model integrations](https://github.com/convergent-technologies/convergent-sdk/blob/main/python/docs/integrations/index.md)
- [Existing OpenTelemetry](https://github.com/convergent-technologies/convergent-sdk/blob/main/python/docs/opentelemetry.md)
- [Troubleshooting](https://github.com/convergent-technologies/convergent-sdk/blob/main/python/docs/troubleshooting.md)

Read the installed `convergent-sdk` version and package source.
If no version constraint exists, upgrade to the latest release with the project's package manager.
Read the installed instrumentation package metadata and source.
Use the installed API when it conflicts with a newer example.
Report a version mismatch before using a missing API.

## Configure the SDK

Use the project's package manager to add `convergent-sdk`.
Call `convergent.init()` once before any instrumentor starts.
Pass the application's release through `release=` or `CONVERGENT_RELEASE`.

Use `CONVERGENT_API_KEY` to send recordings to Convergent.
Use `CONVERGENT_SPANS_DIR` to write local OTLP/JSONL recordings.
Set `CONVERGENT_STRICT=1` during validation.
Call `convergent.flush()` after a traced call when the runtime can skip normal process exit.

## Preserve existing telemetry

Find the tracer provider that the agent process uses.
Let `convergent.init()` attach to the global provider when one already exists.
Pass an existing provider through `tracer_provider=` when the application does not install it globally.
Pass the same provider to each model instrumentor.
Do not create a second tracer provider.
Do not replace existing span processors, samplers, resources, or exporters.

Use `agents=[...]` when Convergent attaches to an existing provider.
Match each `agents=` name to `gen_ai.agent.name` exactly.
Remember that existing exporters also receive recorded content.

## Filter what is sent

The filters exist since SDK 0.0.5.
If the installed version is older, stop and return `needs user`.
Use `require_span_attributes={...}` to send only spans with allowed values.
Use `reject_span_attributes={...}` to withhold spans with named values.
Set the same filters with `CONVERGENT_REQUIRE_SPAN_ATTRIBUTES` or `CONVERGENT_REJECT_SPAN_ATTRIBUTES`.
Each variable takes a JSON object, for example `{"customer.id": ["acme"]}`.
A keyword argument wins over its variable.
Both filters accept any attribute key from a span attribute, a resource attribute, or a
`context_attributes=` mark.
Mark each request with `context_attributes=` on `span()` before you add a filter.
The filter decides each span alone.
A kept parent does not keep its children.
Matching is exact by type and case.
Copy the key and each value from a recorded span, not from the request text.
The filters run in front of every destination, including a local spans directory.
The filters govern only destinations the SDK set up.
Exporters the application added still receive every span, recorded content included.
When the request is about privacy, report that to the user.
If no source holds the key, `require_span_attributes=` sends nothing.
An unmarked span under `reject_span_attributes=` is sent.

Set the filters once, in `init()`. They judge every span in the process.
Every span inherits its parent span's stamped context attributes, whatever thread or context it starts on.
A span's own `context_attributes=` adds pairs and wins for a key both hold. Its descendants follow the override.
A span with no in-process parent inherits nothing.
When application code starts rootless spans on another thread, open a `span()` with `context_attributes=` there, or use a threading instrumentor.
OpenTelemetry's `ThreadingInstrumentor` is one.
A separate process inherits nothing. Configure the SDK and the filters in each process.

Prove a new or changed filter with one recording.
Exercise one request the filter keeps and one it withholds, into a temporary spans directory.
Require the recording to hold exactly the kept run, with its `convergent.attributes.<key>` stamps.
Require zero spans from the withheld request.
Under `require_span_attributes=`, check the whole spans file, not one agent's subtree.
Every unmarked span in the process is withheld, other agents included.
Read the spans file.
An HTTP status is not proof.
Confirm the printed `check()` report names the filter in its `filters` row (the row exists since SDK 0.0.6).
An empty recording means `context_attributes=` is missing or every request was withheld; check the attribute first.

## Preserve import order

Initialize Convergent before the instrumented model client starts.
Place environment variables before an instrumentor reads them.
Preserve imports that register framework callbacks.
Append to callback registries instead of replacing them.

## Mark the agent and tools

Use `@convergent.agent(name="stable-name")` on one request entry point.
Use `@convergent.tool()` on a directly called tool.
Use one `convergent.span(..., operation="tool_call")` at a shared dispatch site.
Use `convergent.current_span()` inside a decorated function.
Set `gen_ai.conversation.id` on the agent span for multi-turn agents.

Use `set_input()` and `set_output()` for recorded content.
Use `set_tool_call_id()` when the model supplies a tool call identifier.

## Instrument model clients

Choose the package for the client that sends the request.
Choose the framework package only when framework steps must appear.
Install one package that can wrap each request.

| Imported client | Package | Enable |
| --- | --- | --- |
| `openai` | `opentelemetry-instrumentation-openai-v2>=2.4b0` | `OpenAIInstrumentor().instrument()` |
| `anthropic` | `opentelemetry-instrumentation-anthropic>=0.62.1` | `AnthropicInstrumentor().instrument()` |
| `google-genai` | `opentelemetry-instrumentation-google-genai>=1.0b1` | `GoogleGenAiSdkInstrumentor().instrument()` |
| `google-cloud-aiplatform` | `opentelemetry-instrumentation-vertexai>=0.62.1` | `VertexAIInstrumentor().instrument()` |
| `openai-agents` | `opentelemetry-instrumentation-openai-agents-v2>=0.1.0` | `OpenAIAgentsInstrumentor().instrument()` |
| `langchain` | `opentelemetry-instrumentation-langchain>=0.62.1` | `LangchainInstrumentor().instrument()` |

Use litellm's built-in OpenTelemetry callback for litellm.
Append `"otel"` to `litellm.callbacks`.
Set `USE_OTEL_LITELLM_REQUEST_SPAN=true` for that callback.
litellm loads a `.env` file at import, and that file can set
`CONVERGENT_ENDPOINT` or `CONVERGENT_API_KEY`.
After you add litellm, verify the endpoint in the `check()` report.

Use pydantic-ai's `Instrumentation` capability for pydantic-ai.
Pass `convergent.tracer_provider()` to its settings.
Let pydantic-ai open its own agent run.

Set `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT` to the package's span capture value.
Set `OTEL_SEMCONV_STABILITY_OPT_IN=gen_ai_latest_experimental` for OpenAI v2 content.

Read the current model integration page for exact imports and provider behavior.

## Write a model span when no package exists

Open `convergent.span(name=..., operation="model_call")` inside the request function.
Keep the span open until the response or stream completes.
Record the input before the request.
Record the output after the response.
Set the request model.
Set input and output token counts from the response.

## Produce a local recording

Set `CONVERGENT_SPANS_DIR` to a new temporary directory.
Run with `CONVERGENT_STRICT=1`.
Close the traced call before flushing.
Inspect the resulting `spans*.jsonl` file with `convergent-verify`.

## Confirm hosted delivery

Call `convergent.check()` after `convergent.init()` in the same process.
Flush before you check, or the report shows no linked agents yet.
When the report names no agent, wait 30 seconds and check once more.
When the target is a long-lived server, add one temporary route that flushes and then checks.
Delete that route after verification.
Print the report after the traced call and flush.
Remove the temporary check code after verification.
Require `round trip ok` before claiming network delivery.
Require the target agent name before claiming the server received the recording.
Keep local recording verification separate from this check.
