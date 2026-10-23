---
title: Python SDK
description: Set your key, instrument your agent with a coding agent, or install the SDK by hand.
---

The SDK records agent runs, model calls, and tool calls in Python as
OpenTelemetry spans and sends them to your Convergent workspace.

## Set your key

1. Mint an ingestion key at
   [app.convergent.dev/workspace/settings](https://app.convergent.dev/workspace/settings).
2. Export it:

```bash
export CONVERGENT_API_KEY="<your ingestion key>"
```

Set it in the environment that runs your agent. In a deployment, store it with
your other secrets.

## Instrument with a coding agent

From the root of your repository, give Claude Code, Codex, or another coding
agent this prompt. Fill in the last two lines with the agent to instrument and a
command that runs it.

```text
Install the convergent-instrument and convergent-verify skills from the github repository convergent-technologies/convergent-sdk.

convergent-instrument is a skill to instrument your agent, and convergent-verify is a skill to inspect each recording. You will be given the agent file or directory to instrument, as well as the instructions or command that runs the agent for the representative run. Use both skills to instrument and verify your agent. Continue using both skills until the recording has no unresolved instrumentation issue.

Agent to instrument: <file or directory>
Representative run: <command or instructions that run the agent>
```

The coding agent installs the SDK with your package manager, adds the tracing,
runs your command, and checks the recording. It repeats until the recording has
no open issue. With your key set, the run sends a trace to your workspace.

[Instrument with a coding agent](../../docs/coding-agent.md) says what each
skill does, and gives a prompt to check an agent that is already instrumented.

## Install by hand

Skip this section if a coding agent instrumented your agent.

```bash
pip install convergent-sdk
```

Use your own package manager's command, such as `uv add` or `poetry add`.
[Versioning](stability.md) states the version policy.

`init()` also needs a release, which names the deployed version of your code.
Convergent groups recordings by release. Use a Git SHA, a build ID, or an image
tag. Without a release, `init()` turns tracing off.

```bash
export CONVERGENT_RELEASE=$(git rev-parse --short HEAD)
```

You can also pass it as `init(release="...")`.

### Example: Trace a run

Call `init()` once at startup. Put `agent()` on the function that handles one
request. Inside it, keep the model call your app already makes. This example
uses the OpenAI client. Save it as `app.py`.

```python
from openai import OpenAI

import convergent

convergent.init()

client = OpenAI()
MODEL = "gpt-5.5"


@convergent.agent(name="support-agent")
def answer(question: str) -> str:
    with convergent.span(name=MODEL, operation="model_call") as call:
        call.set_input({"question": question})
        completion = client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "user", "content": question}],
        )
        reply = completion.choices[0].message.content
        call.set_output({"answer": reply})
        call.set_attribute("gen_ai.request.model", MODEL)
        call.set_attribute("gen_ai.usage.input_tokens", completion.usage.prompt_tokens)
        call.set_attribute("gen_ai.usage.output_tokens", completion.usage.completion_tokens)
    return reply


print(answer("Where is my invoice?"))
convergent.flush()
```

Run it:

```bash
pip install openai
export OPENAI_API_KEY="<your OpenAI key>"
python app.py
```

This records one agent run with one model call inside it. The model call holds
the prompt, the answer, the model, and the token counts. Another model client
goes in the same place and gives the same trace shape.

## Next steps

- [Instrument with a coding agent](../../docs/coding-agent.md): what the skills do.
- [OpenTelemetry](opentelemetry.md): attach to an existing OpenTelemetry setup and filter spans.
- [Attach files to spans](../../docs/attach-files.md): record the files a span read or wrote.
- [Integrations](integrations/index.md): the integration packages.
- [Configuration](configuration.md): every argument and environment variable.
- [API reference](reference/api.md): every public function.
- [Attributes](../../docs/attributes.md): the attribute spellings Convergent reads.
- [Troubleshooting](../../docs/troubleshooting.md): what to do when a run does not arrive.

The [SDK repository](https://github.com/convergent-technologies/convergent-sdk)
has the source and runnable examples. One is the run above as a single file that
needs no OpenAI key. Another records one trace across a dispatcher and three
worker processes.
