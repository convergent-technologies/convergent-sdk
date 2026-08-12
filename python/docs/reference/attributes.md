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
still live traffic. OpenLLMetry's `semconv_ai` package emits them today, and so
does litellm: its default OpenTelemetry callback writes `gen_ai.system`, and its
`otel_v2` callback ships a compatibility mapper, on unless you turn it off, that
writes both earlier names beside the current ones.

`gen_ai.usage.cost` sits outside the conventions. It is the one cost spelling a
producer and a consumer already agree on: OpenLIT's SDK writes it and Langfuse
reads it. litellm's two OpenTelemetry callbacks spell one call's cost two ways,
so both rows are here. Its default callback writes `gen_ai.cost.total_cost`. Its
newer `otel_v2` callback writes `litellm.cost.total`, on its model spans and on
the tool spans it prices alike, such as a call to an MCP tool.

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

This section is about the one attribute `gen_ai.operation.name`: which producer
values become which, and what happens to a value nothing here lists.

The tables below rename a producer's own span kind onto
`gen_ai.operation.name`. That target is a closed set, so a source value with no
row below is dropped rather than copied across.

A span that already states `gen_ai.operation.name` keeps the value it states,
unless the producer that wrote it has a table below of its own words for that
key. Today litellm is the only one. Each table belongs to the producer it is
listed under and is applied to that producer's spans, which is what lets one
word mean different things in different tables.

An operation name no table covers reaches storage as the word the producer
wrote. It renders as a plain step on the timeline, with the content on it, and
without the model, the token counts, or the cost being read as a model call's.

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

### litellm values for gen_ai.operation.name

litellm writes `gen_ai.operation.name` itself rather than renaming another key
onto it, and the word it writes there is the name of the call type the caller
made. Its default OpenTelemetry callback puts that name in the attribute as it
stands, spelling only `completion` as `chat`, so a call to the OpenAI Responses
API arrives as `responses` and an embedding arrives as `embedding`. The rows
below are litellm's own statement of what each of its call types means, taken
from `_OPERATION_BY_CALL_TYPE` in `litellm/integrations/otel/model/semconv.py`,
less two of that table's rows.

| What the producer writes | What it becomes |
| --- | --- |
| `completion` | `chat` |
| `acompletion` | `chat` |
| `completion_with_retries` | `chat` |
| `responses` | `chat` |
| `aresponses` | `chat` |
| `atext_completion` | `text_completion` |
| `embedding` | `embeddings` |
| `aembedding` | `embeddings` |

Five of the eight land on `chat` because litellm's chat completions and its
Responses API calls are both chat completions in the conventions. One entry
point takes the OpenAI Responses request shape and the other the chat
completions shape, and both return an assistant turn from a chat model.

Your model calls are read whichever litellm you run. Recent versions ship a
second callback, `otel_v2`, which runs the call type through the same table
before it writes the span, so its spans already say `chat` where the table above
says `chat`, and every row here leaves them as they are. The default callback
lands on the same word for all eight rows when you set
`OTEL_SEMCONV_STABILITY_OPT_IN=gen_ai_latest_experimental`, by a different
route: it matches on part of the call type rather than reading the table, and
the only words it can write are `chat`, `text_completion`, and `embeddings`.

Two of litellm's rows are left out of the table above. The first is
`text_completion`, which litellm maps to `text_completion`. That is already the
conventions' own word, so the row would change nothing, and a table whose value
is also one of its keys folds a second time when a trace is read again. The
second is `call_mcp_tool`, which litellm maps to `execute_tool`. Only the
default callback writes that call type, and it writes none of
`gen_ai.tool.name`, `gen_ai.tool.call.arguments`, or `gen_ai.tool.call.result`
beside it. The tool's name, arguments, and result arrive inside a
`metadata.mcp_tool_call_metadata` value the span carries whole. Reading the word
as a tool call would give you a tool call with no name and no arguments in place
of a step that shows that value. The `otel_v2` callback writes `execute_tool`
itself, so its MCP spans are read as tool calls with or without a row here.

A call type litellm's own table does not list, such as `image_generation`, keeps
the word litellm wrote and renders as a plain step. That holds for the default
callback with no opt-in set. The other two paths write `chat` for a call type
they do not list, so the same call arrives as a model call.

These rows are read on spans whose tracer is named `litellm`, or a dotted child
of it, and on no others. The words are litellm's own, and they are not all its
own alone: `completion` is a text completion in the OpenLLMetry table above and
a chat completion here, because that is what each producer means by it.

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
