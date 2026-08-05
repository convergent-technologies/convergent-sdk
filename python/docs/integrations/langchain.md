---
title: LangChain
description: Trace every chain, model call, and tool call LangChain runs.
---

Every runnable LangChain invokes becomes a span inside your agent run, with no
span code of your own around it. Instrument LangChain when you want the chains
and the steps inside them in the trace. When the model calls are all you want,
instrument the model client instead, which is what
[Integrations](index.md) covers.

## Install

```bash
pip install "opentelemetry-instrumentation-langchain>=0.62.1" "langchain-core>=1.5" "langchain-openai>=1.4"
```

## Enable

```python
import os

import convergent
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI
from opentelemetry.instrumentation.langchain import LangchainInstrumentor

convergent.init(release=os.environ["GIT_SHA"])
LangchainInstrumentor().instrument()

chain = ChatPromptTemplate.from_template("Answer: {question}") | ChatOpenAI(
    model="gpt-4.1-mini"
)


@convergent.agent(name="convergent-demo")
def answer(question: str) -> str:
    return str(chain.invoke({"question": question}).content)
```

## What lands in the trace

Under the `invoke_agent` span that `agent()` opened, the chain above arrives as
three spans:

- one span per chain, named after the runnable and suffixed `.workflow`
  (`RunnableSequence.workflow` here), carrying `gen_ai.agent.name` set to the
  runnable's name
- one span per step inside it, named `execute_task` followed by the step's name,
  carrying `gen_ai.task.name` and the step's inputs and outputs
- one span per model call, named after the model class and suffixed `.chat`
  (`ChatOpenAI.chat` here), carrying `gen_ai.request.model`,
  `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`,
  `gen_ai.response.finish_reasons`, and the messages

A chain arrives as an agent run of its own, named after the runnable. A chain
built with `|` is called `RunnableSequence` unless you name it, so name the ones
you want to tell apart in the workspace.
