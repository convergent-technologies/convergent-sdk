---
title: LiteLLM
description: Trace every model call LiteLLM makes, whichever provider it routes to.
---

LiteLLM has its own OpenTelemetry callback, so you install no instrumentation
package. Turn the callback on, and each model call becomes a span inside your
agent run, with its cost.

## Install

```bash
pip install "litellm>=1.95" "pydantic-settings>=2.14"
```

Install `pydantic-settings` yourself. Without it, LiteLLM records nothing.

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

Set this variable. Without it, the trace has no model calls.

Append to `litellm.callbacks`. Do not assign to it. An assignment removes every
callback that was set earlier.

Convergent also reads spans from LiteLLM's newer `otel_v2` callback. If you use
it, set `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=span_and_event` to
record prompts and completions.
