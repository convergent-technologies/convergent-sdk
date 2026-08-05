---
title: Attribute support
description: Which spelling of each fact we read, across the producer conventions.
---

Convergent follows the industry's attribute naming conventions, and it reads the
four in common use: OpenTelemetry GenAI, OpenLLMetry and Traceloop, litellm, and
OpenInference. They spell the same fact differently, so Convergent normalizes
every spelling to the OpenTelemetry GenAI name before reading it. The tables
below are the full mapping, on both the live receiver and the file import path.

- Your span keeps its original attributes.
- The standard key is added next to them, so a reader that wants the producer's
  own spelling still has it.
- A standard key already on the span keeps the value it arrived with.
- The conventions are applied in the column order below, so the first one with a
  value wins.

A key in no table below stays on the stored span and is readable there, and
nothing computed reads it: it becomes no token count, no cost, no other
first-class field. To make such a fact first-class, record it under the key in
the first column: on a span your own code opens,
`call.set_attribute("gen_ai.usage.cost", 0.0042)` writes it. A span an
instrumentation package writes carries what the package put there; where that
package's spelling has no row here, file it at
[github.com/convergent-technologies/convergent-sdk/issues](https://github.com/convergent-technologies/convergent-sdk/issues).

## Which spelling of each fact we read

A dash means that convention's table has no row for the fact. Either the
producer already writes the key in the first column, or it writes a spelling an
earlier column already covers, or it does not record the fact at all.

"Built from" means the fact is spread across many keys and Convergent assembles
the value out of them. The target holds a message array, and no single producer
key carries one.

| What Convergent reads | OpenTelemetry GenAI | OpenLLMetry and Traceloop | litellm | OpenInference |
| --- | --- | --- | --- | --- |
| `gen_ai.operation.name` | — | `llm.request.type`, `traceloop.span.kind` | — | `openinference.span.kind` |
| `gen_ai.request.model` | — | `llm.request.model` | — | `llm.model_name`, `embedding.model_name`, `reranker.model_name` |
| `gen_ai.response.model` | — | `llm.response.model` | — | — |
| `gen_ai.provider.name` | `gen_ai.system` | — | — | `llm.provider`, `llm.system` |
| `gen_ai.usage.input_tokens` | `gen_ai.usage.prompt_tokens` | `llm.usage.prompt_tokens` | — | `llm.token_count.prompt` |
| `gen_ai.usage.output_tokens` | `gen_ai.usage.completion_tokens` | `llm.usage.completion_tokens` | — | `llm.token_count.completion` |
| `gen_ai.usage.total_tokens` | — | `llm.usage.total_tokens` | — | `llm.token_count.total` |
| `gen_ai.usage.reasoning_tokens` | `gen_ai.usage.details.reasoning_tokens` | `llm.usage.reasoning_tokens` | — | — |
| `gen_ai.usage.cost` | `operation.cost` | — | `gen_ai.cost.total_cost`, `litellm.cost.total` | `llm.cost.total` |
| `gen_ai.response.finish_reasons` | `gen_ai.response.finish_reason` | `llm.response.finish_reason`, `llm.response.stop_reason`, built from `gen_ai.completion.{i}.finish_reason` | — | `llm.finish_reason` |
| `gen_ai.agent.name` | — | `traceloop.entity.name` | — | — |
| `gen_ai.tool.name` | — | — | — | `tool.name` |
| `gen_ai.tool.description` | — | — | — | `tool.description` |
| `gen_ai.conversation.id` | — | — | — | `session.id` |
| `convergent.session.id` | `gen_ai.conversation.id`, `session.id` | — | — | — |
| `gen_ai.input.messages` | — | built from `gen_ai.prompt.{i}.*` | — | built from `llm.input_messages.{i}.message.*`, or `input.value` |
| `gen_ai.output.messages` | — | built from `gen_ai.completion.{i}.*` | — | built from `llm.output_messages.{i}.message.*`, or `output.value` |
| `gen_ai.system_instructions` | — | built from the system and developer role `gen_ai.prompt.{i}.*` | — | built from the system and developer role `llm.input_messages.{i}.message.*` |

The two spellings in the OpenTelemetry column are the conventions' own earlier
names, renamed by the conventions themselves in v1.27.0 and v1.37.0. They are
still live traffic, because they are what OpenLLMetry's `semconv_ai` package
emits today and what litellm's default OpenTelemetry callback goes through.

`gen_ai.usage.cost` sits outside the conventions. It is the one cost spelling a
producer and a consumer already agree on: OpenLIT's SDK writes it and Langfuse
reads it. litellm's two integrations spell their own cost differently and cannot
both be on one span, so both rows are here.

`gen_ai.conversation.id` and `session.id` are two names for the same id. The
OpenTelemetry GenAI conventions use the first and OpenInference uses the
second. Convergent reads both and adds its own `convergent.session.id` next to
whichever one arrives. Setting either name through the SDK writes
`gen_ai.conversation.id` and `convergent.session.id`, and the SDK never writes
the bare `session.id` key.

A message reconstruction falls back to the whole payload for OpenInference,
because that producer labels it: `input.value` beside `input.mime_type` says
whether the value is a turn or a request envelope, and only a turn is taken.

## Tool call arguments and results

A tool call's arguments and its result are the two facts the table above cannot
state, because they are read where the producer wrote them and no rename
happens. Convergent takes the first key below that has a value, in the order
listed, so a producer that has adopted the OpenTelemetry GenAI names is believed
over its own older spelling.

| What Convergent reads | The other spellings accepted, in the order they are tried |
| --- | --- |
| `gen_ai.tool.call.arguments` | `tool_arguments`, `input`, `input.value` |
| `gen_ai.tool.call.result` | `tool_response`, `output`, `output.value` |

The arguments have to be an object, such as `{"city": "Paris"}`, or a JSON string
that decodes to one. Both read as the same arguments. A bare string, a number, or
a JSON array is stored and then shows as "Invalid arguments" wherever the call is
rendered, so a producer with a single value to pass should send it as a named
field. The result is kept in the shape it arrived in and carries no such
requirement.

A tool span whose arguments and result are both absent is logged together with
the attribute keys it did carry. A spelling none of the rows above covers and
content capture being switched off produce the same empty span, and the keys are
what tell the two apart.

## Operation names

The tables below rename a producer's own span kind onto
`gen_ai.operation.name`. That target is a closed set, so a source value with no
row below is dropped rather than copied across.

A span that already states `gen_ai.operation.name` keeps the value it states.
Nothing here rewrites it, and nothing folds one operation name onto another, so
an operation your own code wrote reaches storage as the word you wrote.

### OpenLLMetry and Traceloop values for gen_ai.operation.name

| What the producer writes | What it becomes |
| --- | --- |
| `workflow` | `invoke_workflow` |
| `task` | `invoke_agent` |
| `agent` | `invoke_agent` |
| `tool` | `execute_tool` |
| `completion` | `text_completion` |
| `chat` | `chat` |
| `rerank` | `retrieval` |
| `embedding` | `embeddings` |

### OpenInference values for gen_ai.operation.name

| What the producer writes | What it becomes |
| --- | --- |
| `llm` | `chat` |
| `embedding` | `embeddings` |
| `chain` | `invoke_agent` |
| `retriever` | `retrieval` |
| `reranker` | `retrieval` |
| `tool` | `execute_tool` |
| `agent` | `invoke_agent` |
| `prompt` | `text_completion` |

## What is not renamed

Request parameters other than the model, e.g. `llm.request.temperature` and
`llm.request.top_p`, are left alone. Nothing on either path reads a
`gen_ai.request.*` key other than the model, and the values already ride along
in the span's own attributes. The per-component costs beside each total, e.g.
litellm's input and output cost, are left alone for the same reason: a model call
carries one cost.

`traceloop.entity.input` and `traceloop.entity.output` are left alone. The
workflow and task decorators write the decorated function's own arguments as a
JSON object, and the message keys hold an array, so the value would be rejected
and the content lost either way. OpenInference's `input.value` is the same fact
and is read, because that producer labels it.

`agent.name` is left alone. A producer that writes it also names the agent
somewhere Convergent already reads.
