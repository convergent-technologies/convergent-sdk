# convergent-sdk

Records the runs of your Python AI agents as OpenTelemetry spans and sends them to your
Convergent workspace.

## Install

```bash
pip install convergent-sdk
```

## Set your key

Mint an ingestion key at
[app.convergent.dev/workspace/settings](https://app.convergent.dev/workspace/settings).

```bash
export CONVERGENT_API_KEY="cvk_xxxxxxxxxxxxxxxx"
export GIT_SHA=$(git rev-parse --short HEAD)
```

The export is the local form. In a deployment the key belongs in your secrets manager or
your platform's environment configuration.

`GIT_SHA` is the release. It ties every trace to the version of your code that produced it.
A git sha is the usual choice, and any string that names a version works: a build id, an
image tag, a date.

## Trace a run

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

```bash
pip install openai
export OPENAI_API_KEY=sk-xxxxxxxxxxxxxxxx
GIT_SHA=$(git rev-parse --short HEAD) python app.py
```

That records one agent run with one model call inside it, carrying the prompt, the answer,
the model, and the token counts. Any other model client goes in the same place, and the
trace keeps the same shape.

## Confirm it arrived

`init()` rejects a setup that cannot work, and a setup it accepts can still fail to deliver
quietly. `check()` reports what this process configured, then asks the server what it can
see for the same key and release.

```python
print(convergent.check())
```

```
convergent: enabled
  release     9f2c1d4
  mode        a tracer provider we created
  sending to  convergent

  round trip  ok (146ms)
  key         org_30f981e207bf3b38
  agents      support-agent

  no notes
```

`round trip ok` means the server answered this process, and your agent's name on the
`agents` line is the proof that a trace arrived.

## What lands in the trace

Each run arrives as one trace. The agent run is the root, the model calls and tool calls sit
inside it, and each of those carries the prompts, answers, token counts, and finish reasons it
recorded. Every trace names the release that produced it, so you can compare one version of
your agent against another. Traces that share a conversation id read together as one thread.

## Instrument with a coding agent

If you use Claude Code, Cursor, or another coding agent, hand it the skill next to this
README. Copy `skills/instrument/` into your own project, then ask the agent to add Convergent
tracing to your agent.

```bash
cp -r skills/instrument /path/to/your/project/.claude/skills/instrument
```

The skill has the agent list what one run touches, confirm that list with you, wrap each
part, and then read the recorded spans back against the list.
[Instrument with a coding agent](docs/agent-skill.md) walks through what it does.

## Documentation

- [Get started](docs/index.md) installs the SDK, sets your key, and confirms a run reaches
  your workspace.
- [Instrument your agent](docs/instrument.md) marks the agent run, the tool calls, the steps
  in between, and the turns of a conversation.
- [Instrument with a coding agent](docs/agent-skill.md) installs the skill above and says
  what the agent does with it.
- [Already using OpenTelemetry](docs/opentelemetry.md) is what `init()` does when a tracer
  provider already exists, the agent filter, and traces that cross processes.
- [Integrations](docs/integrations/index.md) is which instrumentation package to install for
  the library your agent already uses.
- [Configuration](docs/configuration.md) is the environment variables, the destinations,
  strict startup, and what leaves your process.
- [API reference](docs/reference/api.md) is every call in the public surface.
- [Attribute support](docs/reference/attributes.md) is which spelling of each fact Convergent
  reads.
- [Troubleshooting](docs/troubleshooting.md) reads the `check()` report for you and covers
  nothing arriving, split traces, and missing agents.

## Examples

`examples/quickstart/main.py` is the run above as a file, with the model call answered from
a script in the same file so it runs with no OpenAI key and no network.

`examples/parallel-workers/` records one trace across a dispatcher and three queue worker
processes. It needs no credentials and checks its own output.

