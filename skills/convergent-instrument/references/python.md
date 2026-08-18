# Python instrumentation

Use this reference after the target agent and model client are known.

## Read current APIs

When internet access exists, read the matching public SDK page before editing:

- [Python SDK](https://github.com/convergent-technologies/convergent-sdk/blob/stable/python/docs/index.md)
- [Instrumentation](https://github.com/convergent-technologies/convergent-sdk/blob/stable/python/docs/instrument.md)
- [Model integrations](https://github.com/convergent-technologies/convergent-sdk/blob/stable/python/docs/integrations/index.md)
- [Existing OpenTelemetry](https://github.com/convergent-technologies/convergent-sdk/blob/stable/python/docs/opentelemetry.md)
- [Troubleshooting](https://github.com/convergent-technologies/convergent-sdk/blob/stable/python/docs/troubleshooting.md)

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
If each request carries a span attribute, a resource attribute, or a
`context_attributes=` mark for the key, use `require_span_attributes={...}` to send only spans
with allowed values, or `reject_span_attributes={...}` to withhold spans with named values.
Mark each request with `context_attributes=` on `span()` before you add `require_span_attributes=`.
If no source holds the key, `require_span_attributes=` sends nothing to Convergent or to a
`File` or `Console` destination. An unmarked span under `reject_span_attributes=` is sent.
Match each filter name to `gen_ai.agent.name` exactly.
Remember that existing exporters also receive recorded content.

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
Print the report after the traced call and flush.
Remove temporary check output after verification.
Require `round trip ok` before claiming network delivery.
Require the target agent name before claiming the server received the recording.
Keep local recording verification separate from this check.
