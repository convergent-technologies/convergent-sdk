---
title: Integrations
description: Trace the model calls your app already makes by installing one instrumentation package.
full: true
---

An instrumentation package records each call to a model client. You install the
package and add one line, and you write no span code.

## Enable a package

Install the package. Then call `init()` before the instrumentor, and both before
the first model call.

```bash
pip install "opentelemetry-instrumentation-openai-v2>=2.4b0" "openai>=2.52"
```

```python
import os

import convergent
from openai import OpenAI
from opentelemetry.instrumentation.openai_v2 import OpenAIInstrumentor

convergent.init(release=os.environ["GIT_SHA"])
OpenAIInstrumentor().instrument()

client = OpenAI()


@convergent.agent(name="convergent-demo")
def answer(question: str) -> str:
    reply = client.chat.completions.create(
        model="gpt-4.1-mini",
        messages=[{"role": "user", "content": question}],
    )
    return reply.choices[0].message.content or ""
```

Every package below works the same way. Only the install line, the import, and
the instrumentor change.

## Model clients

| The app imports | Package | Version | Instrumentor |
| --- | --- | --- | --- |
| `openai` | `opentelemetry-instrumentation-openai-v2` | 2.4b0 | `opentelemetry.instrumentation.openai_v2.OpenAIInstrumentor` |
| `anthropic` | `opentelemetry-instrumentation-anthropic` | 0.62.1 | `opentelemetry.instrumentation.anthropic.AnthropicInstrumentor` |
| `google-genai` | `opentelemetry-instrumentation-google-genai` | 1.0b1 | `opentelemetry.instrumentation.google_genai.GoogleGenAiSdkInstrumentor` |
| `google-cloud-aiplatform` | `opentelemetry-instrumentation-vertexai` | 0.62.1 | `opentelemetry.instrumentation.vertexai.VertexAIInstrumentor` |
| `openai-agents` | `opentelemetry-instrumentation-openai-agents-v2` | 0.1.0 | `opentelemetry.instrumentation.openai_agents.OpenAIAgentsInstrumentor` |

Pin the version you install. These packages change between releases.

The `openai` package does not record calls through `client.responses`.

## Frameworks

These libraries have their own page.

| The app imports | Page |
| --- | --- |
| `langchain` | [LangChain](langchain.md) |
| `litellm` | [LiteLLM](litellm.md) |
| `pydantic-ai` | [Pydantic AI](pydantic-ai.md) |

## Temporal

Install the optional `temporal` extra before importing the Temporal integration:

```bash
pip install "convergent-sdk[temporal]"
```

The extra needs `temporalio>=1.34.0,<2`.

Pass `convergent.temporal.TracingInterceptor()` to the client:

```python
from temporalio.client import Client

import convergent.temporal


async def connect() -> Client:
    return await Client.connect(
        "localhost:7233",
        interceptors=[convergent.temporal.TracingInterceptor()],
    )
```

Use this interceptor instead of temporalio's own `TracingInterceptor`, not
alongside it. pydantic-ai's `LogfirePlugin` for Temporal adds temporalio's
`TracingInterceptor`, so leave that plugin out of a client that uses this one.
It is temporalio's interceptor with one change: an agent that runs inside a
workflow starts its own trajectory instead of joining the caller's, in every
run, including a cron run, a child workflow run and a run that continues as
new. An agent in one of the workflow's activities nests under the workflow's
agent that handed off to it, as a child run of that trajectory. A `Worker` built
from this client picks the interceptor up by itself, so do not list it again in
the worker's `interceptors`; that would record every worker span twice.

The session id is off by default, because it may name a person. Opt in with
`TracingInterceptor(send_session=True)` only when your Temporal cluster stays
inside your own services: the session id then goes in Temporal's request
headers, which Temporal stores in the workflow's history. Set it on both
interceptors, the one on the client that starts the workflow and the one on
the worker's client, or the session stops before the workflow.

An agent span stays open until the agent run returns, and a span is exported
only once it ends. Do not keep an agent run open across a durable wait, such as
`workflow.wait_condition`, a timer, or waiting for a signal: the trace would show
nothing until the wait is over. Run each step of the agent in its own activity,
so each step ends before the workflow waits and the next one starts a new
trajectory:

```python
from datetime import timedelta

from temporalio import activity, workflow


@activity.defn
async def draft_answer(ticket: str) -> str:
    # Run your agent here. Its run ends when the activity returns.
    return f"Draft answer for {ticket}"


@activity.defn
async def answer_follow_up(reply: str) -> str:
    # A second agent run, after the workflow's wait.
    return f"Answer to {reply}"


@workflow.defn
class SupportWorkflow:
    def __init__(self) -> None:
        self.reply: str | None = None

    @workflow.signal
    def customer_replied(self, reply: str) -> None:
        self.reply = reply

    @workflow.run
    async def run(self, ticket: str) -> str:
        timeout = timedelta(minutes=5)
        # The agent run inside draft_answer ends before the wait.
        await workflow.execute_activity(draft_answer, ticket, start_to_close_timeout=timeout)
        await workflow.wait_condition(lambda: self.reply is not None)
        assert self.reply is not None
        # A new agent run, and so a new trajectory, after the wait.
        return await workflow.execute_activity(
            answer_follow_up, self.reply, start_to_close_timeout=timeout
        )
```

Every worker that runs workflows with instrumented agents must pass
`convergent` through Temporal's workflow sandbox. Without it, the span of an
agent whose run starts in workflow code, such as a pydantic-ai agent run through
its Temporal integration, never reaches Convergent, and nothing logs an error:

```python
from temporalio.worker import Worker
from temporalio.worker.workflow_sandbox import (
    SandboxedWorkflowRunner,
    SandboxRestrictions,
)

# connect, SupportWorkflow, draft_answer and answer_follow_up are the examples
# above; put your own client, task queue, workflows and activities here.
async def run_worker() -> None:
    worker = Worker(
        await connect(),
        task_queue="support",
        workflows=[SupportWorkflow],
        activities=[draft_answer, answer_follow_up],
        workflow_runner=SandboxedWorkflowRunner(
            restrictions=SandboxRestrictions.default.with_passthrough_modules("convergent")
        ),
    )
    await worker.run()
```

## Prompts and completions

The `openai` and `google-genai` packages record prompts and completions only
after you set these variables. The other packages record them by default.

```bash
# openai
export OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=span_only
export OTEL_SEMCONV_STABILITY_OPT_IN=gen_ai_latest_experimental

# google-genai
export OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=SPAN_ONLY
```

## One package per library

Use one package for each model call. If two packages wrap the same call, every
call is recorded twice, and token counts double. For example, an app on
`langchain-openai` is covered by both the `openai` and the `langchain` packages.
Pick one.

## Other libraries

Convergent also reads spans from the
[OpenLLMetry](https://github.com/traceloop/openllmetry/tree/main/packages) and
[OpenInference](https://github.com/Arize-ai/openinference/tree/main/python/instrumentation)
packages. They cover libraries such as Amazon Bedrock, CrewAI, LlamaIndex, and
MCP.

If no package covers your library, record the calls with
[`span()`](../reference/api.md#span).
