---
title: litellm
description: Trace every model call litellm makes, whichever provider it routes to.
---

litellm ships its own OpenTelemetry callback, so there is no instrumentation
package to install. Turn the callback on and every model call becomes a span
inside your agent run, with the per call cost on it.

The setup on this page is for litellm's default `otel` callback. The example
uses the chat completions entry points, `litellm.completion()` and its async
form `litellm.acompletion()`. The OpenAI Responses API entry points,
`litellm.responses()` and `litellm.aresponses()`, are read the same way.

litellm also ships a newer `otel_v2` callback. Convergent reads its spans, and
[The newer callback](#the-newer-callback) says what arrives from it. Its setup
is litellm's to document, so this page does not state it.

## Install

```bash
pip install "litellm>=1.95" "pydantic-settings>=2.14"
```

The callback sends through the OTLP exporter the SDK install already brings.
`pydantic-settings` is the part you have to add: the callback imports it and
litellm does not depend on it, so without it litellm logs
`Error initializing custom logger` at startup, records nothing, and keeps
answering requests.

## Enable

```python
import os

import convergent
import litellm

convergent.init(release=os.environ["GIT_SHA"])
litellm.callbacks.append("otel")


@convergent.agent(name="convergent-demo")
def answer(question: str) -> str:
    reply = litellm.completion(
        model="gpt-4.1-mini",
        messages=[{"role": "user", "content": question}],
    )
    return reply.choices[0].message.content or ""
```

```bash
export USE_OTEL_LITELLM_REQUEST_SPAN=true
```

That variable is required for the `otel` callback above. Without it litellm
writes the model attributes onto your agent span instead of opening its own
span, so the trace arrives with no model call in it, and nothing raises.

**Warning:** Append to `litellm.callbacks`. Do not assign to it.

```python
litellm.callbacks.append("otel")  # correct
litellm.callbacks = ["otel"]  # wrong: silently drops every callback set earlier
```

Assignment replaces the whole list, so any callback another vendor registered
first stops firing, and nothing raises or logs. Appending is safe in any order,
and litellm's own callback manager drops duplicate appends.

## What lands in the trace

One `litellm_request` span per model call, under the `invoke_agent` span that
`agent()` opened, carrying:

- `gen_ai.request.model` and `gen_ai.response.model`
- `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, and
  `gen_ai.usage.total_tokens`
- `gen_ai.cost.total_cost`, litellm's own price for that call
- `gen_ai.response.finish_reasons` and `gen_ai.response.id`
- `gen_ai.input.messages` and `gen_ai.output.messages`

A small `raw_gen_ai_request` child holds the raw exchange with the provider: the
request litellm sent and the reply that came back, both under the provider's own
field names. It opens and closes with its parent, and Convergent reads nothing
on it. One call should be one step, so the child gets no row of its own on the
timeline: it renders through the model call above it, and the model call names
the span id it took in. The span itself is stored whole, so every attribute it
arrived with is still there for anything reading the raw spans of the trace.

The span says what kind of call it was in `gen_ai.operation.name`, and this
callback puts litellm's own name for the call type there. `completion` is the
one name it rewrites, to `chat`. Every other entry point arrives under its own
name, so `acompletion()` arrives as `acompletion`, a call to the Responses API
as `responses`, and an embedding as `embedding`. Convergent recognizes litellm's
spans by their tracer name and reads litellm's names for its call types, so each
call renders as the kind of step it is, with the model, the tokens, the
messages, and the cost on it. See
[operation names](../reference/attributes.md#litellm-values-for-gen_aioperationname)
for the whole list.

## The newer callback

litellm also has an `otel_v2` callback, and Convergent reads its spans too. It
names each span after the kind of call it made, such as `chat gpt-4.1-mini`
rather than `litellm_request`, and it puts the conventions' own word in
`gen_ai.operation.name`, so a Responses API call says `chat` there where the
default callback says `responses`. It spells the price `litellm.cost.total`
rather than `gen_ai.cost.total_cost`, and it opens no `raw_gen_ai_request`
child. Everything else is the same fields under the same names.

The messages are the one thing you have to ask for. `otel_v2` captures no
message content until you turn it on, so out of the box its spans carry no
`gen_ai.input.messages` and no `gen_ai.output.messages`, and every model call in
the trace shows with nothing said in it. The default callback captures both
without being asked. To get the messages from `otel_v2`, set its capture mode:

```bash
export OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=span_and_event
```

Your model calls are read whichever litellm you run and whichever of the two
callbacks you turn on. A recent litellm labels a call the way the conventions
expect when you use `otel_v2`, and litellm's own labels are read as well, so the
model, the tokens, and the cost land in the same places either way.

A `completion()` call outside every span starts a trace of its own. See
[one run arrives as many traces](../troubleshooting.md#one-run-arrives-as-many-traces).
