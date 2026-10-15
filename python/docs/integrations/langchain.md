---
title: LangChain
description: Trace every chain, model call, and tool call LangChain runs.
---

Instrument LangChain to see each chain and its steps in the trace. If you only
need the model calls, instrument the model client instead, as
[Integrations](index.md) describes. Do not use both.

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

Each chain shows as its own agent run, named after the chain. A chain built
with `|` is named `RunnableSequence` unless you give it a name. Name each chain
you want to tell apart.
