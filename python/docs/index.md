---
title: Python SDK
description: Install the SDK, set your key, and trace your first agent run.
---

## Install

```bash
pip install convergent-sdk
```

[Versioning](stability.md) states the version policy.

## Set your key

1. Mint an ingestion key at
   [app.convergent.dev/workspace/settings](https://app.convergent.dev/workspace/settings).
2. Export it:

```bash
export CONVERGENT_API_KEY="cvk_xxxxxxxxxxxxxxxx"

# Optional
export GIT_SHA=$(git rev-parse --short HEAD)
```

Set both in the environment that runs your agent. In a deployment, store them
with your other secrets.

`init()` needs `CONVERGENT_API_KEY`, or a `File()` destination to record
locally. `GIT_SHA` is only an example name. Use it to pass a release to
`init(release=...)`.

## Instrument with a coding agent

Two skills do the work. `convergent-instrument` adds tracing, and
`convergent-verify` checks the recording. Your coding agent installs both from
the SDK repository. They work with Claude Code and Codex.

From the root of your repository, give your coding agent this prompt. Fill in
the last two lines:

```text
Install the convergent-instrument and convergent-verify skills from the github repository convergent-technologies/convergent-sdk.

convergent-instrument is a skill to instrument your agent, and convergent-verify is a skill to inspect each recording. You will be given the agent file or directory to instrument, as well as the instructions or command that runs the agent for the representative run. Use both skills to instrument and verify your agent. Continue using both skills until the recording has no unresolved instrumentation issue.

Agent to instrument: <file or directory>
Representative run: <command or instructions that run the agent>
```

The agent instruments your code, runs the command, and checks the recording.
It repeats until the recording has no open issue.

To check an agent that is already instrumented, use this prompt instead:

```text
Install the convergent-instrument and convergent-verify skills from the github repository convergent-technologies/convergent-sdk.

convergent-instrument is a skill to instrument your agent, and convergent-verify is a skill to inspect each recording. You will be given the agent file or directory that is already instrumented, as well as the instructions or command that runs the agent for the representative run. Use both skills to verify the existing instrumentation, and fix each evidence-backed instrumentation issue. Rerun and verify until the recording has no unresolved instrumentation issue.

Agent to verify: <file or directory that is already instrumented>
Representative run: <command or instructions that run the agent>
```

[Instrument with a coding agent](agent-skill.md) describes what each skill does.

## Example: Trace a run

Call `init()` once at startup. Put `agent()` on the function that handles one
request. Inside it, keep the model call your app already makes. This example
uses the OpenAI client. Save it as `app.py`.

`release` names the deployed version, so Convergent groups recordings by
release. Pass any string, or set `CONVERGENT_RELEASE` and leave out the argument.

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

Run it:

```bash
pip install openai
export OPENAI_API_KEY=sk-xxxxxxxxxxxxxxxx
export GIT_SHA=$(git rev-parse --short HEAD)
python app.py
```

This records one agent run with one model call inside it. The model call holds
the prompt, the answer, the model, and the token counts. Another model client
goes in the same place and gives the same trace shape.

## Next steps

- [Instrument with a coding agent](agent-skill.md): what the skills do.
- [OpenTelemetry](opentelemetry.md): attach to an existing OpenTelemetry setup and filter spans.
- [Attach files to spans](attach-files.md): record the files a span read or wrote.
- [Integrations](integrations/index.md): the integration packages.
- [Configuration](configuration.md): every argument and environment variable.
- [API reference](reference/api.md): every public function.
- [Attributes](reference/attributes.md): the attribute spellings Convergent reads.
- [Troubleshooting](troubleshooting.md): what to do when a run does not arrive.

The [SDK repository](https://github.com/convergent-technologies/convergent-sdk)
has the source and runnable examples. One is the run above as a single file that
needs no OpenAI key. Another records one trace across a dispatcher and three
worker processes.
