---
title: litellm
description: Trace every model call litellm makes, whichever provider it routes to.
---

litellm ships its own OpenTelemetry callback, so there is no instrumentation
package to install. Turn the callback on and every `completion()` becomes a span
inside your agent run, with the per call cost on it.

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

That variable is required. Without it litellm writes the model attributes onto
your agent span instead of opening its own span, so the trace arrives with no
model call in it, and nothing raises.

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

A small `raw_gen_ai_request` child holds the unparsed provider response.

A `completion()` call outside every span starts a trace of its own. See
[one run arrives as many traces](../troubleshooting.md#one-run-arrives-as-many-traces).
