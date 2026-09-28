---
title: Get started
description: Install the SDK, set your key, and instrument your agent with the coding-agent skills.
---

## Install

```bash
pip install convergent-sdk
```

[Stability](stability.md) states the version policy.

## Set your key

1. Mint an ingestion key at
   [app.convergent.dev/workspace/settings](https://app.convergent.dev/workspace/settings).
2. Export it:

```bash
export CONVERGENT_API_KEY="cvk_xxxxxxxxxxxxxxxx"

# Optional
export GIT_SHA=$(git rev-parse --short HEAD)
```

Both are environment variables, and they must be set in the environment that
runs your agent. The export works for local development. In a deployment, put
them where you store your other secrets or environment variables.

`init()` needs `CONVERGENT_API_KEY`, or a `File()` destination when you record
locally instead. `GIT_SHA` is an example environment variable name you can use
to describe a release with `init(release=...)`. Describing a release is
optional.

## Instrument with a coding agent

Two portable skills do the work: `convergent-instrument` adds tracing, and
`convergent-verify` inspects the recording. Your coding agent installs both from
the SDK repository. This works with Claude Code and Codex.

From the root of your repository, give your coding agent this prompt. Fill in
the agent to instrument and one representative run:

```text
Install the convergent-instrument and convergent-verify skills from the github repository convergent-technologies/convergent-sdk.

convergent-instrument is a skill to instrument your agent, and convergent-verify is a skill to inspect each recording. You will be given the agent file or directory to instrument, as well as the instructions or command that runs the agent for the representative run. Use both skills to instrument and verify your agent. Continue using both skills until the recording has no unresolved instrumentation issue.

Agent to instrument: <file or directory>
Representative run: <command or instructions that run the agent>
```

The agent instruments your code, runs the command, and inspects the recording.
It repeats until the recording has no unresolved instrumentation issue.

To check an existing setup, ask the agent to use `convergent-verify` on a
recording. It reports findings and changes no code. This prompt starts that
check directly:

```text
Install the convergent-instrument and convergent-verify skills from the github repository convergent-technologies/convergent-sdk.

convergent-instrument is a skill to instrument your agent, and convergent-verify is a skill to inspect each recording. You will be given the agent file or directory that is already instrumented, as well as the instructions or command that runs the agent for the representative run. Use both skills to verify the existing instrumentation, and fix each evidence-backed instrumentation issue. Rerun and verify until the recording has no unresolved instrumentation issue.

Agent to verify: <file or directory that is already instrumented>
Representative run: <command or instructions that run the agent>
```

[Instrument with a coding agent](agent-skill.md) describes what each skill does.

## Example: Trace a run

Call `init()` once at startup, and put `agent()` on the function that handles one request.
The model call inside it is the one your app already makes, and this one is the OpenAI
client. Save it as `app.py`.

`release` names the deployed version, so recordings group by release. Pass any
string, or set `CONVERGENT_RELEASE` and drop the argument.

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

- [Instrument with a coding agent](agent-skill.md): what the skills do.
- [Already using OpenTelemetry](opentelemetry.md): attach to an existing OpenTelemetry setup and filter spans.
- [Integrations](integrations/index.md): the integration packages.
- [Instrument](instrument.md): confirm a run arrived with `check()`, and instrument by hand.
- [Configuration](configuration.md): every argument and environment variable.
- [API reference](reference/api.md): every public function.
- [Attribute support](reference/attributes.md): the attribute spellings Convergent reads.
- [Troubleshooting](troubleshooting.md): what to do when a run does not arrive.

The [SDK repository](https://github.com/convergent-technologies/convergent-sdk)
holds the source and runnable examples: the run above as a self-contained file
that needs no OpenAI key, and one trace recorded across a dispatcher and three
worker processes.
