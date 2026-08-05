---
title: pydantic-ai
description: Trace every pydantic-ai agent run, model call, and tool call.
---

pydantic-ai emits OpenTelemetry spans itself, so there is no instrumentation
package to install. Point its instrumentation at the tracer provider `init()`
configured and each `run()` becomes an agent run in your workspace.

## Install

```bash
pip install "pydantic-ai>=1.107,<2"
```

The bound matters: pydantic-ai 2.x renames the run span's token keys to
`gen_ai.aggregated_usage.*`, and this page describes the 1.x line.

## Enable

```python
import os

import convergent
from pydantic_ai import Agent
from pydantic_ai.capabilities.instrumentation import Instrumentation
from pydantic_ai.models.instrumented import InstrumentationSettings

convergent.init(release=os.environ["GIT_SHA"])

agent = Agent(
    "openai:gpt-4.1-mini",
    name="convergent-demo",
    capabilities=[
        Instrumentation(
            settings=InstrumentationSettings(
                tracer_provider=convergent.tracer_provider(),
                include_content=True,
            )
        )
    ],
)

print(agent.run_sync("Is my invoice paid?").output)
```

`include_content=True` is what puts prompts and completions in the trace.
`InstrumentationSettings(version=...)` selects the span format pydantic-ai
emits; the default works with Convergent.

Name every agent. `Agent(name=...)` becomes `gen_ai.agent.name`, which is how the
workspace tells agents apart.

Use one `Instrumentation` capability per agent. Each one records its own copy of
every span, so a second doubles the whole trace. When the agent already has other
capabilities, add to the list rather than replacing it.

pydantic-ai opens the agent run itself, so do not also wrap the same `run()` or
`run_sync()` call in `agent()` under the same name. The run would arrive twice.
Reserve an outer `agent()` for a coordinator of your own, under a name of its
own. Leave `init(agents=...)` out for the same reason: pydantic-ai names the
agent itself.

pydantic-ai writes its own `gen_ai.conversation.id` onto the spans it opens:
the `conversation_id=` you pass to `run()`, else the most recent id in
`message_history`, else a fresh id for the run. Pass `conversation_id=` to
`run()` to group a conversation's turns under your own id.

## What lands in the trace

- one `invoke_agent` span per run, carrying `gen_ai.agent.name` from
  `Agent(name=...)` and the run's aggregated token counts under `gen_ai.usage.*`
- one span per model call, named `chat` followed by the model, carrying
  `gen_ai.request.model`, `gen_ai.response.model`, `gen_ai.provider.name`, and
  both token counts
- `gen_ai.input.messages` and `gen_ai.output.messages` on the model call
- one span per tool call the agent makes
