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
