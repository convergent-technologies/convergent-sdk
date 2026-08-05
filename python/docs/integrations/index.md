---
title: Integrations
description: Trace the model calls your app already makes by installing one instrumentation package.
full: true
---

An instrumentation package wraps a library's client and records every call it
makes as an OpenTelemetry span. `init()` configures where the spans of this
process go, so once a package is instrumented the model calls arrive in your
workspace with no span code of your own around them.

Convergent reads OpenTelemetry spans, so any library that emits GenAI spans
arrives this way, whether or not it is named on this page.

## Enable a package

Install the package, call `init()`, then instrument, all before the first model
call. A model call that runs before `init()` is recorded nowhere, and `check()`
afterwards still reports a healthy setup, because the setup is healthy from that
point on. A warm-up call or a model call at module import is where that happens.

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

Every package in the table below works this way. Only the install line, the
import, and the instrumentor class change.

## Model clients

| The app imports | Package | Version | Instrumentor |
| --- | --- | --- | --- |
| `openai` | `opentelemetry-instrumentation-openai-v2` | 2.4b0 | `opentelemetry.instrumentation.openai_v2.OpenAIInstrumentor` |
| `anthropic` | `opentelemetry-instrumentation-anthropic` | 0.62.1 | `opentelemetry.instrumentation.anthropic.AnthropicInstrumentor` |
| `google-genai` | `opentelemetry-instrumentation-google-genai` | 1.0b1 | `opentelemetry.instrumentation.google_genai.GoogleGenAiSdkInstrumentor` |
| `google-cloud-aiplatform` | `opentelemetry-instrumentation-vertexai` | 0.62.1 | `opentelemetry.instrumentation.vertexai.VertexAIInstrumentor` |
| `openai-agents` | `opentelemetry-instrumentation-openai-agents-v2` | 0.1.0 | `opentelemetry.instrumentation.openai_agents.OpenAIAgentsInstrumentor` |

Pin the version you install. These packages rename attributes and change which
environment variables they read between releases.

A few of them cover less, or more, than the name suggests:

- The `openai` package wraps `chat.completions` and `embeddings`. A call through
  `client.responses` is not recorded.
- The `google-genai` package also covers a client built with
  `genai.Client(vertexai=True)`, so it is the one to reach for on Vertex AI. The
  `vertexai` package covers `vertexai.generative_models`, the older SDK Google
  has deprecated.
- The `openai-agents` package records through the Agents SDK's own tracing
  pipeline, so a process that calls `set_tracing_disabled(True)` records nothing.

## Libraries with a page of their own

Three libraries need more than an instrumentor line, and each has a page for
what is different about it.

| The app imports | Instrumentation | Version | Page |
| --- | --- | --- | --- |
| `langchain` | `opentelemetry-instrumentation-langchain` | 0.62.1 | [LangChain](langchain.md) |
| `litellm` | built into litellm | litellm 1.95.0 | [litellm](litellm.md) |
| `pydantic-ai` | built into pydantic-ai | pydantic-ai 1.107 or later, below 2 | [pydantic-ai](pydantic-ai.md) |

## What lands in the trace

One span per model call, under the `invoke_agent` span that `agent()` opened.
The span name comes from the package: the `openai` package names it `chat`
followed by the model, and the `anthropic` package names it `anthropic.chat`.
The attributes are the same facts whichever package wrote them:

- `gen_ai.request.model` and `gen_ai.response.model`
- `gen_ai.usage.input_tokens` and `gen_ai.usage.output_tokens`
- `gen_ai.response.finish_reasons` and `gen_ai.response.id`
- `gen_ai.input.messages` and `gen_ai.output.messages`

Some packages add to that. The `anthropic` package records
`gen_ai.usage.cache_read.input_tokens` and
`gen_ai.usage.cache_creation.input_tokens`. The `google-genai` package records
`gen_ai.provider.name`, reading `gemini` or `vertex_ai` depending on how the
client was built. The streaming and async paths of a client are recorded the
same way as a plain call.

## Prompts and completions

The `anthropic`, `vertexai`, `langchain`, and `openai-agents` packages record
prompts and completions with nothing set. The `openai` and `google-genai`
packages record them once their capture variable is set.

```bash
export OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=span_only
```

The `openai` package reads `span_only` and the `google-genai` package reads
`SPAN_ONLY`. A value neither one recognizes, such as `true`, leaves the span with
no prompts and no completions on it, and raises nothing.

The `openai` package needs
`OTEL_SEMCONV_STABILITY_OPT_IN=gen_ai_latest_experimental` as well, because its
default conventions put content on log records, which this SDK does not export. Any
capture value that routes content to log records, such as `EVENT_ONLY` or
`SPAN_AND_EVENT`, is lost the same way, so prefer the span-only setting each package
documents.

## One package per library

Two packages that can wrap the same call record every model call twice, with the
token counts on both copies, so anything computed from those counts is twice
what it should be, and nothing errors.

Two packages cover the same call more often than it looks. An app on
`langchain-openai` is covered by the `openai` package and by the `langchain`
package. Pick the package that matches the model client the app imports rather
than the framework wrapped around it. The client sits lower, so one package
covers every framework in the process that talks to it. Instrument the framework
instead when you want its chains and steps in the trace too, and then leave the
client's package out.

A framework that opens the agent run itself, such as the OpenAI Agents SDK or
pydantic-ai, needs no `agent()` around the same call. Wrapping it under the same
name records the run twice. Keep an outer `agent()` for a coordinator of your
own, under a name of its own.

## The provider your spans go to

`instrument()` with no arguments sends spans to the global tracer provider,
which is the provider `init()` configures, so call `init()` first and instrument
after it. When you hand `init()` a `tracer_provider` of your own it leaves the
global provider alone, and each package then needs that same provider.

```python
OpenAIInstrumentor().instrument(tracer_provider=convergent.tracer_provider())
```

## Spans keep their own attributes

A package writes the attribute names its own convention uses, and those stay on
the span. Convergent renames the facts it reads onto one vocabulary and adds
them next to the originals, so both spellings are there to read. Some packages
spread prompts and completions across indexed keys such as
`gen_ai.prompt.0.content`, which Convergent rebuilds into message arrays on the
way in. [Attribute support](../reference/attributes.md) is the table of which
spelling of each fact Convergent reads.

## Other libraries

Convergent reads the OpenLLMetry and OpenInference attribute conventions, so the
spans either registry's packages write arrive with no span code of your own
around them. OpenLLMetry, from Traceloop, publishes
`opentelemetry-instrumentation-*` packages. OpenInference, from Arize, publishes
`openinference-instrumentation-*` packages. Where both cover a library, run one.
The registries are
[OpenLLMetry](https://github.com/traceloop/openllmetry/tree/main/packages) and
[OpenInference](https://github.com/Arize-ai/openinference/tree/main/python/instrumentation).

| Library | OpenLLMetry | OpenInference |
| --- | --- | --- |
| Agno | `opentelemetry-instrumentation-agno` | `openinference-instrumentation-agno` |
| AgentSpec | | `openinference-instrumentation-agentspec` |
| Aleph Alpha | `opentelemetry-instrumentation-alephalpha` | |
| Amazon Bedrock | `opentelemetry-instrumentation-bedrock` | `openinference-instrumentation-bedrock` |
| Amazon SageMaker | `opentelemetry-instrumentation-sagemaker` | |
| AutoGen | | `openinference-instrumentation-autogen` |
| AutoGen AgentChat | | `openinference-instrumentation-autogen-agentchat` |
| BeeAI | | `openinference-instrumentation-beeai` |
| Chroma | `opentelemetry-instrumentation-chromadb` | |
| claude-agent-sdk | | `openinference-instrumentation-claude-agent-sdk` |
| Cohere | `opentelemetry-instrumentation-cohere` | |
| CrewAI | `opentelemetry-instrumentation-crewai` | `openinference-instrumentation-crewai` |
| DSPy | | `openinference-instrumentation-dspy` |
| Google ADK | | `openinference-instrumentation-google-adk` |
| google-generativeai | `opentelemetry-instrumentation-google-generativeai` | |
| Groq | `opentelemetry-instrumentation-groq` | `openinference-instrumentation-groq` |
| Guardrails | | `openinference-instrumentation-guardrails` |
| Haystack | `opentelemetry-instrumentation-haystack` | `openinference-instrumentation-haystack` |
| Instructor | | `openinference-instrumentation-instructor` |
| LanceDB | `opentelemetry-instrumentation-lancedb` | |
| LlamaIndex | `opentelemetry-instrumentation-llamaindex` | `openinference-instrumentation-llama-index` |
| Marqo | `opentelemetry-instrumentation-marqo` | |
| MCP | `opentelemetry-instrumentation-mcp` | `openinference-instrumentation-mcp` |
| Microsoft Agent Framework | | `openinference-instrumentation-agent-framework` |
| Milvus | `opentelemetry-instrumentation-milvus` | |
| Mistral AI | `opentelemetry-instrumentation-mistralai` | `openinference-instrumentation-mistralai` |
| Ollama | `opentelemetry-instrumentation-ollama` | |
| Pinecone | `opentelemetry-instrumentation-pinecone` | |
| Pipecat | | `openinference-instrumentation-pipecat` |
| Portkey | | `openinference-instrumentation-portkey` |
| Promptflow | | `openinference-instrumentation-promptflow` |
| Qdrant | `opentelemetry-instrumentation-qdrant` | |
| Replicate | `opentelemetry-instrumentation-replicate` | |
| smolagents | | `openinference-instrumentation-smolagents` |
| Strands Agents | | `openinference-instrumentation-strands-agents` |
| Together AI | `opentelemetry-instrumentation-together` | |
| Transformers | `opentelemetry-instrumentation-transformers` | |
| Voyage AI | `opentelemetry-instrumentation-voyageai` | |
| watsonx | `opentelemetry-instrumentation-watsonx` | |
| Weaviate | `opentelemetry-instrumentation-weaviate` | |
| Writer | `opentelemetry-instrumentation-writer` | |

## Nothing for your library

Some model calls have no package at all, such as a provider you reach over plain
HTTP. Write those as spans of your own with [`span()`](../instrument.md), which is what
[Get started](../index.md) does.

For an app that already emits OpenTelemetry from somewhere else, see
[Already using OpenTelemetry](../opentelemetry.md).
