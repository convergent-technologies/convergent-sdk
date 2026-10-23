---
title: Record agents, models, and tools by hand
description: The rules for agent, model, and tool spans when no integration records them for you.
---

Use these rules when your framework does not record OpenTelemetry spans for
you, or records only some of them. A recording that follows them looks the same
in Python and TypeScript. If an integration or a framework already records a
span, do not wrap the same agent, model, or tool call again.

## The agent span

Wrap the function that handles one request with `agent()`, and each tool with
`tool()`. Use `span()` for a block that is not its own function, such as one
model request. The wrappers do not record arguments or return values. Record
content with `set_input()` and `set_output()` (Python) / `setInput()` and
`setOutput()` (TypeScript).

When one request runs several agents, wrap the whole request in one agent span.
Name it after the class that runs the request, such as
`FinancialResearchManager`. If no class runs it, use the function name. Copy the
spelling from the code. Open [`session()`](../python/docs/reference/api.md#session)
around that span when the request is one turn of a conversation.

An agent that another agent calls as a tool gets a tool span for the call, and
its own agent span inside that tool span.

Give agents stable names. A name that changes between runs splits one agent into
several in Convergent.

## Agent output

Every agent span has an output. The top agent span's output holds what the user
gets: the answer and the deliverable, plus a final status when your application
has one. A deliverable often leaves the agent as a tool call or a saved file,
not as the final text. A memo the agent saves with a `write_memo` tool belongs
in the top agent span's output too.

An agent that ends with a terminating tool call and no final text, such as a
`submit_verdict` tool, records that call's arguments or result as its output.

```python tab="Python"
@convergent.agent(name="verifier")
def verify(draft: str) -> dict:
    verdict = run_verifier(draft)
    convergent.current_span().set_output(
        [{"role": "assistant", "parts": [{"type": "text", "content": json.dumps(verdict)}]}]
    )
    return verdict
```

```ts tab="TypeScript"
const verify = agent({ name: "verifier" }, async (draft: string) => {
  const verdict = await runVerifier(draft);
  currentSpan().setOutput([
    { role: "assistant", parts: [{ type: "text", content: JSON.stringify(verdict) }] },
  ]);
  return verdict;
});
```

This rule is for agent spans only. A tool that returns nothing has a tool span
with no output, and that is correct. Do not add a made-up output to a tool span.

## Model spans

Name each model span `chat <model>`, such as `chat gpt-5.4`, and pass the
request model as `model`. This follows the OpenTelemetry GenAI convention: the
operation, then the model. Keep the span open until the response or the stream
completes.

```python tab="Python"
with convergent.span(name=f"chat {model}", operation="model_call", model=model) as call:
    ...
```

```ts tab="TypeScript"
await span({ name: `chat ${model}`, operation: "model_call", model }, async (call) => {
  // ...
});
```

Set these optional fields with `set_attribute()` / `setAttribute()`:

- When the request offers tools, set `gen_ai.tool.definitions` to a JSON string
  of a list with one object per tool: `type`, `name`, `description`, and
  `parameters`.
- Set `gen_ai.is_streaming` to true only for a streamed call. Leave it out
  otherwise.
- Set `gen_ai.system_instructions` to the instruction text. Do not add it to the
  input messages as a system message.

Find where your framework keeps the system prompt and the tool list for each
request. Some frameworks keep them in the messages, such as a leading `system`
message. Set `gen_ai.system_instructions` and `gen_ai.tool.definitions` from
there, and leave system messages out of the input messages.

On an agent span, pass `system_instructions=` and `tools=` (Python) /
`systemInstructions` and `tools` (TypeScript) instead. Pass tools only when the
agent has tools. Never pass an empty list.

## Messages

On agent and model spans, pass `set_input()` and `set_output()` a list of
messages in the OpenTelemetry GenAI format. Each message has a `role` of `user`,
`assistant`, or `tool`, and a list of `parts`. Convert each provider item to one
part:

| Item | Part |
| --- | --- |
| Text | `{"type": "text", "content": "..."}` |
| Tool call, in an `assistant` message | `{"type": "tool_call", "id": "<call id>", "name": "<tool>", "arguments": {...}}` |
| Tool result, in a `tool` message | `{"type": "tool_call_response", "id": "<call id>", "response": ...}` |
| Reasoning text | `{"type": "reasoning", "content": "..."}` |

Add `finish_reason` to an output message when the provider returns one. A value
that is not a nonempty list of objects that each have `role` becomes one text
message that holds the whole value. The [attribute reference](attributes.md#accepted-values)
lists every accepted value.

Do not put file bytes in a message. Replace a file block, such as a PDF or an
image, with a short text part, such as `[application/pdf, 6521 bytes]`, and
record the file itself with [attach files](attach-files.md).

## Token usage

Set each count the provider returned on the model span, as an integer:

| Count | Attribute | OpenAI Responses API field |
| --- | --- | --- |
| Input | `gen_ai.usage.input_tokens` | `input_tokens` |
| Output | `gen_ai.usage.output_tokens` | `output_tokens` |
| Cache read | `gen_ai.usage.cache_read.input_tokens` | `input_tokens_details.cached_tokens` |
| Cache creation | `gen_ai.usage.cache_creation.input_tokens` | `input_tokens_details.cache_write_tokens` |
| Reasoning | `gen_ai.usage.reasoning_tokens` | `output_tokens_details.reasoning_tokens` |
| Total | `gen_ai.usage.total_tokens` | `total_tokens` |

Copy the provider's raw usage. A framework's normalized usage object can turn a
missing field into 0. Read the raw response JSON, because a client's types can
leave out a field that the API returns. For another provider, find the field for
each count in its raw usage. Leave out a count the provider did not return.

## Tools the provider runs

A tool that the provider runs inside the model request, such as hosted web
search, gets one tool span per call. Open it inside the model span after the
response arrives, so it sits under the span that made the call:

```python tab="Python"
with convergent.span(name=tool_name, operation="tool_call",
                     attributes={"gen_ai.tool.type": "extension"}) as hosted:
    hosted.set_tool_call_id(provider_call_id)
```

```ts tab="TypeScript"
span(
  { name: toolName, operation: "tool_call", attributes: { "gen_ai.tool.type": "extension" } },
  (hosted) => hosted.setToolCallId(providerCallId),
);
```

Set its input to the request part of the provider's call item, such as the web
search `action` without `sources`. Set its output to the call's status and
results, such as `{ status, sources }`. Use null for a result the provider did
not return. Its duration is about 0 ms, because the provider does not report
the real timing.

## Frameworks with their own tracing

Some frameworks, such as the OpenAI Agents SDK, have their own tracing that is
not OpenTelemetry. When that tracing cannot feed OpenTelemetry, wrap the
framework's model-provider interface, so each model request gets one model
span. For the OpenAI Agents SDK, pass a wrapping `ModelProvider` in
`RunConfig(model_provider=...)` (Python), or install one with
`setDefaultModelProvider()` (TypeScript). Forward every other method and
property to the wrapped object. Wrap each agent run with an agent span.

Some OpenTelemetry instrumentation libraries record prompts as log records, not
as span attributes. The SDK exports spans only, so those prompts never reach
Convergent. Check that model spans carry `gen_ai.input.messages`. If they do
not, use a library that writes prompts on the span, or record them with
`set_input()` / `setInput()` on a model span.

## Language notes

In Python, decorate an `async def` function with `@convergent.tool()`, not a
callable object. The decorator checks `inspect.iscoroutinefunction()`, which is
false for an object whose `__call__` returns a coroutine, so the span ends
before the tool runs. Wrap such an object in an `async def` function that
awaits it.

In TypeScript, a span ends when its callback returns or its Promise settles.
Consume a stream inside the callback and await it. A returned iterator ends the
span early.
