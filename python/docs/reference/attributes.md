---
title: Attribute support
description: Attribute read order, accepted values, span kinds, and custom fields.
---

Convergent reads the attributes on each span and its OpenTelemetry resource.
The tables below list each field and its read order. An attribute name in
these tables is a key your instrumentation can send. It is not a Python SDK
argument. The SDK's recording methods are in the [API reference](api.md).

## Attribute precedence

Read the numbered entries for each field from top to bottom. Convergent selects the first present key,
then checks its value. A present key with an invalid value blocks every later
key for that field. A missing key allows the next key to be tried.

For example, a span with `convergent.input_tokens="bad"` and
`gen_ai.usage.input_tokens=12` has no input token count. It does not use `12`.
With the first key absent, its input token count is `12`.

For direct attributes, the same rule applies to zero, false, null, and empty values. Presence decides
which value to check; the [value rules](#accepted-values) decide whether it is
usable. For example, `convergent.stream=False` wins over
`gen_ai.is_streaming=True` and records `False`.

```python
for key in keys_in_read_order:
    if key in attributes:
        value = attributes[key]
        return normalize(value) if valid(value) else None
return None
```

All keys below are span attributes unless marked **resource**. **Agent only**
means the key applies only to an agent run. `pydantic-ai` means the additional
tracer rules in [Events and framework attributes](#events-and-framework-attributes)
apply. An instrumentation scope identifies the tracer that recorded a span.
Direct aliases such as `operation.cost`, `session.id`, and `llm.model_name`
work regardless of the tracer that recorded the span.

### Operation and identity

| Field | Order | Attribute or event |
| --- | --- | --- |
| Operation | 1 | `convergent.operation` |
|  | 2 | `gen_ai.operation.name` |
| Requested model | 1 | `convergent.agent.model` (agent only) |
|  | 2 | `convergent.request_model` |
|  | 3 | `gen_ai.request.model` |
|  | 4 | `model_name` (agent only, `pydantic-ai`) |
|  | 5 | `llm.model_name` |
| Response model | 1 | `convergent.response_model` |
|  | 2 | `gen_ai.response.model` |
| Provider name | 1 | `convergent.provider_name` |
|  | 2 | `gen_ai.provider.name` |
|  | 3 | `gen_ai.system` |
| Agent name | 1 | `convergent.agent_name` |
|  | 2 | `convergent.agent.name` (resource) |
|  | 3 | `gen_ai.agent.name` |
|  | 4 | `agent_name` |
| Reported agent version | 1 | `convergent.agent_version` |
|  | 2 | `gen_ai.agent.version` |
| Session ID | 1 | `convergent.session.id` |
|  | 2 | `gen_ai.conversation.id` |
|  | 3 | `session.id` |
| Parent trajectory ID | 1 | `convergent.parent_trajectory_id` |
| Release | 1 | `convergent.release` |
|  | 2 | `service.version` (resource) |

The reported agent version is producer metadata. Read the release from
`convergent.release` or the resource's `service.version`.

For example, a resource with `convergent.agent.name="support-agent"` wins over
a span with `gen_ai.agent.name="billing-agent"`. A span's
`convergent.agent_name="refund-agent"` wins over both.

### Messages and response details

| Field | Order | Attribute or event |
| --- | --- | --- |
| Input messages | 1 | `convergent.input_messages` |
|  | 2 | `gen_ai.input.messages` |
|  | 3 | `gen_ai.user.message` event content (`pydantic-ai`) |
| Output messages | 1 | `convergent.output_messages` |
|  | 2 | `gen_ai.output.messages` |
|  | 3 | `gen_ai.choice` event completion (`pydantic-ai`) |
| System instructions | 1 | `convergent.agent.system_instructions` (agent only) |
|  | 2 | `convergent.system_instructions` |
|  | 3 | `gen_ai.system_instructions` |
|  | 4 | `gen_ai.system.message` event content (`pydantic-ai`) |
| Finish reasons | 1 | `convergent.finish_reasons` |
|  | 2 | `gen_ai.response.finish_reasons` |
|  | 3 | `gen_ai.response.finish_reason` |
|  | 4 | `gen_ai.choice` event finish reason (`pydantic-ai`) |
| Response ID | 1 | `convergent.response_id` |
|  | 2 | `gen_ai.response.id` |
| Streaming | 1 | `convergent.stream` |
|  | 2 | `gen_ai.is_streaming` |
| Tool definitions | 1 | `convergent.agent.tools` (agent only) |
|  | 2 | `convergent.tool_definitions` |
|  | 3 | `gen_ai.tool.definitions` |
| Time to first chunk (seconds) | 1 | `convergent.time_to_first_chunk` |
|  | 2 | `gen_ai.server.time_to_first_token` |
| Error type | 1 | `convergent.error_type` |
|  | 2 | `error.type` |
| General input | 1 | `convergent.input` |
| General output | 1 | `convergent.output` |
|  | 2 | `final_result` (`pydantic-ai`) |

### Tool call arguments and results

| Field | Order | Attribute or event |
| --- | --- | --- |
| Tool name | 1 | `convergent.tool_name` |
|  | 2 | `gen_ai.tool.name` |
| Tool call ID | 1 | `convergent.tool_call_id` |
|  | 2 | `gen_ai.tool.call.id` |
|  | 3 | `gen_ai.tool.message` event ID (`pydantic-ai`) |
| Tool type | 1 | `convergent.tool_type` |
|  | 2 | `gen_ai.tool.type` |
| Tool description | 1 | `convergent.tool_description` |
|  | 2 | `gen_ai.tool.description` |
| Tool arguments | 1 | `convergent.tool_arguments` |
|  | 2 | `gen_ai.tool.call.arguments` |
|  | 3 | `tool_arguments` |
|  | 4 | `input` |
| Tool result | 1 | `convergent.tool_result` |
|  | 2 | `gen_ai.tool.call.result` |
|  | 3 | `tool_response` |
|  | 4 | `output` |
|  | 5 | `gen_ai.tool.message` event content (`pydantic-ai`) |

### Usage and cost

| Field | Order | Attribute or event |
| --- | --- | --- |
| Input tokens | 1 | `convergent.input_tokens` |
|  | 2 | `gen_ai.usage.input_tokens` |
|  | 3 | `gen_ai.usage.prompt_tokens` |
| Output tokens | 1 | `convergent.output_tokens` |
|  | 2 | `gen_ai.usage.output_tokens` |
|  | 3 | `gen_ai.usage.completion_tokens` |
| Total tokens | 1 | `convergent.total_tokens` |
|  | 2 | `gen_ai.usage.total_tokens` |
| Reasoning output tokens | 1 | `convergent.reasoning_output_tokens` |
|  | 2 | `gen_ai.usage.reasoning_tokens` |
|  | 3 | `gen_ai.usage.details.reasoning_tokens` |
| Cache read input tokens | 1 | `convergent.cache_read_input_tokens` |
|  | 2 | `gen_ai.usage.cache_read.input_tokens` |
|  | 3 | `gen_ai.usage.cache_read_input_tokens` |
| Cache creation input tokens | 1 | `convergent.cache_creation_input_tokens` |
|  | 2 | `gen_ai.usage.cache_creation.input_tokens` |
|  | 3 | `gen_ai.usage.cache_creation_input_tokens` |
| Cost in US dollars | 1 | `convergent.cost_usd` |
|  | 2 | `gen_ai.usage.cost` |
|  | 3 | `operation.cost` |
|  | 4 | `gen_ai.cost.total_cost` |
|  | 5 | `litellm.cost.total` |

## Accepted values

These rules apply after reading the first present key. They describe received telemetry.
The SDK's own methods may accept a narrower input type or serialize a value
before sending it. A rejected value leaves its field empty. A rejected total
token count may still be calculated from input and output counts. A rejected
value may remain in custom attributes if it is valid JSON.

| Fields | Accepted value | Example and result |
| --- | --- | --- |
| Operation, model names, provider, agent name/version, IDs, release, tool name/type/description, error type | A nonblank string. Whitespace in a valid string stays unchanged. | `" gpt-4o "` stays `" gpt-4o "`; `"   "`, `7`, and `False` are rejected. |
| Session ID | The string rule above, at most 128 characters. | A 129-character string is rejected, not shortened. |
| Release and parent trajectory ID | The string rule above, at most 256 characters. | A 257-character release is rejected. |
| All six token counts | An integer from `0` through `2**63 - 1`, excluding booleans. | `12` and `0` work; `12.0`, `"12"`, `True`, `-1`, and `2**63` do not. |
| Cost and time to first chunk | A finite, nonnegative integer or float, excluding booleans. | `0.25` and `0` work; `"0.25"`, `True`, `-1`, infinity, and NaN do not. |
| Streaming | A boolean. | `False` works; `0` and `"false"` do not. |
| Input and output messages | A list of objects, or a JSON string that decodes to one. | `'[{"role":"user","content":"Hello"}]'` becomes that list; `"Hello"`, `{}`, and `[1]` are rejected. |
| Finish reasons | A string or a list of strings. | `"stop"` becomes `["stop"]`; `[]` stays `[]`; `[1]` is rejected. |
| Tool arguments/result and general input/output | A valid JSON value. Strings stay strings. | `'{}'` stays the string `'{}'`; `{"id": 7}` stays an object; null stays null. |

Time to first chunk uses seconds: `0.25` means 250 milliseconds. Convergent
keeps the supplied number without converting units. This follows the
[OpenTelemetry time-to-first-token unit](https://github.com/open-telemetry/semantic-conventions-genai/blob/main/docs/gen-ai/gen-ai-metrics.md).

### Total token count

A valid supplied total token count wins even if it differs from the sum.
Without one, Convergent adds the valid input and output counts. One known count
is enough; with neither, the total is absent. A sum above `2**63 - 1` is absent.

For example, input `12`, output `3`, and total `99` produce total `99`.
Changing total to `"bad"` produces total `15`. This calculation uses the input
and output fields; it does not try a later total-token alias.

### System instructions

System instructions accept a string or a list. Plain text becomes one system
message. A list of text parts becomes one system message containing those
parts. A list of message objects with string `role` values keeps its structure.
JSON strings containing these lists follow the same rules.

| Input | Recorded messages |
| --- | --- |
| `"Be brief."` | One system message whose text is `Be brief.` |
| `[]` or `"[]"` | No messages. |
| `"[broken"` | One system message whose text is `[broken`. |
| `[1,2]` | One system message whose text is `[1,2]`. |
| `7`, `False`, or an object | Rejected. |

For example, these input text parts:

```json
[
  {"type": "text", "content": "A"},
  {"type": "text", "content": "B"}
]
```

become these recorded messages:

```json
[
  {
    "role": "system",
    "parts": [
      {"type": "text", "content": "A"},
      {"type": "text", "content": "B"}
    ]
  }
]
```

A list that matches neither message objects nor text parts becomes literal
text. A native list is serialized to compact JSON first. An input string
retains its original text.

### Tool definitions

Tool definitions accept a list or a JSON string that decodes to a list.
Convergent sorts the list by each item's top-level string `name`, then by JSON text with sorted object keys. Items without such a name use an empty name
for sorting. Received lists can contain JSON values other than objects; the
SDK's typed `tools=` argument uses mappings.

For example, `[{"name":"z"},{"name":"a"}]` becomes
`[{"name":"a"},{"name":"z"}]`. `[]` stays an empty list. A missing value,
an object, or `"broken"` provides no tools.

### JSON limits

JSON values must contain only null, booleans, finite numbers, strings, lists,
and objects with string keys. Values are limited to 16 MiB of compact UTF-8
JSON, 64 nesting levels, and 65,536 combined list items and object entries.
Integers must fit within 16,384 bits and serialize successfully. Invalid
Unicode strings are rejected.

The Python value `{"enabled": False, "limits": [0, 2]}` can be serialized to
JSON. A set, an object with an integer key, or a list containing NaN is invalid. These limits also apply
to custom values and decoded messages and tools.

## Operation names

The operation determines which kind of span Convergent records.

| Operation | Span kind |
| --- | --- |
| `invoke_agent` | Agent run |
| `chat`, `text_completion`, `generate_content` | Model call |
| `execute_tool` | Tool call |
| Any other nonblank string | Generic span |

The SDK translates `operation="agent_run"` to `invoke_agent`, `model_call` to
`chat`, and `tool_call` to `execute_tool` before export. See the full
[SDK operation list](api.md#operations). Writing `toolcall` as a raw operation
keeps a generic span with operation `toolcall`.

Without a valid operation, Convergent checks for a tool name, then an agent
name, then a requested model or output messages. These select tool, agent, and
model spans respectively. With none, it records a generic span. The `pydantic-ai`
agent-name rule below also applies before these checks.

### `litellm` operation values

For tracers named `litellm` or beginning `litellm.`, Convergent also reads these
operation values case-insensitively. This conversion applies only to
`gen_ai.operation.name`.

| What the producer writes | Operation |
| --- | --- |
| `completion`, `acompletion`, `completion_with_retries`, `responses`, `aresponses` | `chat` |
| `atext_completion` | `text_completion` |
| `embedding`, `aembedding` | `embeddings` |

The resulting values `chat`, `text_completion`, `embeddings`, and
`execute_tool` are also accepted case-insensitively for these tracers. Other
values stay unchanged. `call_mcp_tool` therefore remains generic.

For example, `completion` from `litellm.proxy` becomes a model call. The same
operation from `my_app` or `litellm_proxy` remains generic. A direct cost alias
such as `litellm.cost.total=0.25` still provides cost for all three tracers.

## Fields kept for each span kind

Every span can carry agent name/version, session, release, parent trajectory
ID, token counts, cost, and error type. Its remaining fields depend
on its kind.

| Kind | Fields |
| --- | --- |
| Agent run | Input messages, output messages, system instructions, and general output. A named agent also keeps its model and tool definitions. |
| Model call | Requested and response models, provider, input/output messages, system instructions, finish reasons, response ID, streaming, tool definitions, time to first chunk, request parameters, and general input/output. |
| Tool call | Tool name, call ID, type, description, arguments, and result. |
| Generic span | Operation and general input/output. |

For example, `gen_ai.response.model="gpt-4o-mini"` is a response model on a model
call. On an agent run, that selected value remains a custom attribute under
its original key. A model call's `gen_ai.request.temperature=0.2` becomes
request parameter `temperature=0.2`. Request parameters are all
`gen_ai.request.*` keys except the model, and accept JSON values.

Numeric `gen_ai.usage.details.*` values use the cost value rules and remain
usage details on every kind. For example, `gen_ai.usage.details.requests=2`
records detail `requests=2`. Details do not create input or output token
counts. The reasoning-token alias in the table can also supply that count.

## Events and framework attributes

These rules apply only to tracers named `pydantic_ai`, `pydantic-ai`, or a
dotted child such as `pydantic_ai.agent`. They apply at their positions in the
read-order tables, after earlier direct attributes.

| Field | Event or attribute used |
| --- | --- |
| Input messages | First `gen_ai.user.message` event containing `content`; wrap it as one user text message. |
| System instructions | First `gen_ai.system.message` event containing `content`; apply the system-instruction rules. |
| Output messages | First `gen_ai.choice` event containing `gen_ai.completion`; wrap it as one assistant text message. |
| Finish reasons | Last `gen_ai.choice` event containing `finish_reason`; wrap it in a list. |
| Tool result | First `gen_ai.tool.message` event containing `content`. |
| Tool call ID | First `gen_ai.tool.message` event containing both `content` and `id`. |
| Requested model | The span's `model_name`, on an agent run only. |
| General output | The span's `final_result`. |

For example, two user events with contents `"First"` and `"Second"` produce
one user message containing `"First"`. Choice events with finish reasons
`"length"` then `"stop"` produce `["stop"]`. This is field extraction, not a
concatenation of all events.

With no valid operation, an OpenTelemetry INTERNAL span named `agent support`
with `agent.name="support"` is an agent run for these tracers. This rule decides
the kind; `agent.name` itself is not an agent-name alias in the table.

Outside these tracers, events and `model_name` do not supply these fields.
For example, `model_name="gpt-4o"` on a named agent run from `my_app` stays
custom; `llm.model_name="gpt-4o-mini"` supplies its model. With a matching tracer,
a non-null invalid `model_name` blocks `llm.model_name`. A missing or null
`model_name` supplies no value, so `llm.model_name` is still tried. These
framework extraction rules only supply a value when their result is non-null.

## Custom attributes

Unread attributes with valid JSON values are kept in `custom`, except for
recognized aliases and bookkeeping keys listed below. Their values keep their
JSON types. JSON-looking strings are not decoded.
The receiver strips `convergent.attributes.` and `convergent.custom.` from
custom keys. Other keys keep their spelling, including other `convergent.`
keys. The SDK's `attributes=` and `set_attribute()` methods separately reject
keys that start with `convergent.`.

Convergent reads resource attributes first, then span attributes. Within either set of
attributes, a prefixed key wins over a bare key. If both prefixes occur in that
same set, `convergent.custom.` wins over `convergent.attributes.`.

| Input | Custom result |
| --- | --- |
| Resource `team="billing"`; span `team="support"` | `team="support"` |
| Span `team="billing"` and `convergent.attributes.team="support"` | `team="support"` |
| Resource `convergent.custom.team="billing"`; span `team="support"` | `team="support"` |
| Span `convergent.attributes.team="billing"` and `convergent.custom.team="support"` | `team="support"` |
| Span `acme.config='{"enabled":true}'` | `acme.config` remains a string. |

A selected invalid field can remain custom when its value is valid JSON.
For example, `convergent.input_tokens="bad"` records no input token count but
keeps the string under that custom key. NaN cannot remain custom because it
is not valid JSON. A selected field that does not belong to the span's kind
can also remain custom, as in the response-model example above.

Recognized aliases that lose to an earlier key are not copied to custom.
For example, valid `convergent.input_tokens=12` and
`gen_ai.usage.input_tokens=99` record count `12` without a custom copy of `99`.
SDK bookkeeping attributes `convergent.semantic.version` and
`convergent.content.source` are omitted. `convergent.execution.id` is omitted
when it equals the span's trace ID.

### What is not renamed

A key absent from the read-order tables does not become a field
unless one of the event or prefix rules above applies. For example,
`llm.token_count.prompt=12` and `traceloop.entity.name="support-agent"` remain
custom attributes; they do not supply input tokens or an agent name.

## Which spelling of each fact we read

See [Attribute precedence](#attribute-precedence) for the ordered keys.
