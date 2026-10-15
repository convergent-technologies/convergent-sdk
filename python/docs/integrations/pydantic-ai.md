---
title: Pydantic AI
description: Trace every Pydantic AI agent run, model call, and tool call.
---

Pydantic AI records its own spans, so you install no instrumentation package.
Point it at the tracer provider that `init()` set up. Each `run()` then becomes
an agent run in Convergent.

## Install

```bash
pip install "pydantic-ai>=1.107,<2"
```

Keep the `<2` bound. Pydantic AI 2.x changes its token attributes.

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
            )
        )
    ],
)

print(agent.run_sync("Is my invoice paid?").output)
```

- **Name every agent.** Use a name that stays the same across runs.
- **Add one `Instrumentation` capability per agent.** A second one records
  every span twice.
- **Do not wrap `run()` in `agent()`.** Pydantic AI opens the agent run itself.
- **Pass `conversation_id=` to `run()`** to group a conversation's turns under
  your own ID.
