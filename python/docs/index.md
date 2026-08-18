---
title: Get started
description: Install the SDK, set your key, and instrument your agent with the coding-agent skills.
---

The SDK's source, skills, and examples live at
[github.com/convergent-technologies/convergent-sdk](https://github.com/convergent-technologies/convergent-sdk).
If a coding agent is doing the instrumentation, point it at that repository
first; the prompts below do exactly that.

## Install

```bash
pip install convergent-sdk
```

In a project that pins its dependencies, use `convergent-sdk>=0.0.4,<0.1`.
While the SDK is on 0.x, a minor release may change the public API, and the
upper bound keeps such a release out of a routine dependency update.
[Stability](stability.md) states the full policy.

## Set your key

Mint an ingestion key at
[app.convergent.dev/workspace/settings](https://app.convergent.dev/workspace/settings).

```bash
export CONVERGENT_API_KEY="cvk_xxxxxxxxxxxxxxxx"

# Optional
export GIT_SHA=$(git rev-parse --short HEAD)
```

`CONVERGENT_API_KEY` is required. The export works for local development. In a deployment, you'll want the key wherever you store secrets or your environment variables.

`GIT_SHA` is an example environment variable name you can use to describe a release when initializing convergent with: `init(release=...)`. Note that describing a release is optional.

## Instrument with a coding agent

If you're instrumenting Convergent for the first time, the fastest way to do so is give your coding agent this prompt.

```text
Install the convergent-instrument and convergent-verify skills from the github repository convergent-technologies/convergent-sdk.

convergent-instrument is a skill to instrument your agent, and convergent-verify is a skill to inspect each recording. You will be given the agent file or directory to instrument, as well as the instructions or command that runs the agent for the representative run. Use both skills to instrument and verify your agent. Continue using both skills until the recording has no unresolved instrumentation issue.

Agent to instrument: <file or directory>
Representative run: <command or instructions that run the agent>
```

If you've already instrumented with Convergent, and are just looking to verify your setup, give your coding agent this prompt:

```text
Install the convergent-instrument and convergent-verify skills from the github repository convergent-technologies/convergent-sdk.

convergent-instrument is a skill to instrument your agent, and convergent-verify is a skill to inspect each recording. You will be given the agent file or directory that is already instrumented, as well as the instructions or command that runs the agent for the representative run. Use both skills to verify the existing instrumentation, and fix each evidence-backed instrumentation issue. Rerun and verify until the recording has no unresolved instrumentation issue.

Agent to verify: <file or directory that is already instrumented>
Representative run: <command or instructions that run the agent>
```

Alternatively, you can also install the skills directly in your coding agent using:

```bash
npx skills add convergent-technologies/convergent-sdk
```

You'll have to restart your coding agent session to use the skills directly, but can do so with slash commands:

```
/convergent-instrument
/convergent-verify
```

[Instrument with a coding agent](agent-skill.md) describes what each skill does.

## Example: Trace a run

Call `init()` once at startup, and put `agent()` on the function that handles one request.
The model call inside it is the one your app already makes, and this one is the OpenAI
client. Save it as `app.py`.

```python
import os

from openai import OpenAI

import convergent

convergent.init(release=os.environ["GIT_SHA"])

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

From your command line, run:

```bash
pip install openai
export OPENAI_API_KEY=sk-xxxxxxxxxxxxxxxx
export GIT_SHA=$(git rev-parse --short HEAD)
python app.py
```

That records one agent run with one model call inside it, carrying the prompt, the answer,
the model, and the token counts. Any other model client goes in the same place, and the
trace keeps the same shape.

## The rest of this documentation

[Instrument](instrument.md) covers confirming a run arrived with `check()` and
instrumenting your agent by hand. [OpenTelemetry](opentelemetry.md) covers
attaching to an existing OpenTelemetry setup and the `agents`, `require_span_attributes`, and
`reject_span_attributes` filters, and [Integrations](integrations/index.md)
the integration packages. The reference section holds
[configuration](configuration.md), the [API reference](reference/api.md), the
[attribute spellings Convergent reads](reference/attributes.md), and
[troubleshooting](troubleshooting.md).

The [GitHub repository](https://github.com/convergent-technologies/convergent-sdk)
holds the skills from the coding-agent section above and runnable examples: the
run above as a self-contained file that needs no OpenAI key, and one trace
recorded across a dispatcher and three worker processes.
