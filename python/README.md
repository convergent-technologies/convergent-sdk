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

`CONVERGENT_API_KEY` is the only environment variable the SDK requires. The export is the
local form. In a deployment the key belongs in your secrets manager or your platform's
environment configuration.

`GIT_SHA` is this page's way of naming a release: `init(release=...)` below takes it, and
it ties every trace to the version of your code that produced it. Any string that names a
version works: a git sha, a build id, an image tag, a date.

## Instrument with a coding agent

If you use Claude Code, Codex, Cursor, or another coding agent, install the two skills and
ask the agent to add Convergent tracing:

```bash
npx skills add convergent-technologies/convergent-sdk
```

Install `convergent-instrument` and `convergent-verify` when the installer asks. The first
skill instruments one representative path. The second skill reads the resulting recording
without changing code. The section below is the same instrumentation done by hand.

For a new setup, give your coding agent this prompt:

```text
Install convergent-instrument and convergent-verify from convergent-technologies/convergent-sdk.
Use convergent-instrument to instrument the agent in <agent file or directory>.
Use <command that runs the agent> as the representative run.
Use convergent-verify to inspect each recording.
Continue until the recording has no unresolved instrumentation issue.
```

For an existing setup, give your coding agent this prompt:

```text
Install convergent-instrument and convergent-verify from convergent-technologies/convergent-sdk.
Use convergent-instrument and convergent-verify to review the existing Convergent
instrumentation for the agent in <agent file or directory>.
Use <command that runs the agent> as the representative run.
Fix each evidence-backed instrumentation issue.
Rerun and verify until the expected recording is complete.
```

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

## Documentation

This README is the companion document to
[github.com/convergent-technologies/convergent-sdk](https://github.com/convergent-technologies/convergent-sdk),
and everything past this point lives there.

```bash
git clone https://github.com/convergent-technologies/convergent-sdk
```

The full documentation is under `python/docs/`: confirming a run arrived with `check()`,
instrumenting your agent by hand, attaching to an existing OpenTelemetry setup, the
integration packages, configuration, the API reference, the attribute spellings Convergent
reads, and troubleshooting.

`skills/convergent-instrument/` and `skills/convergent-verify/` are the skills from the
coding-agent section above.

`python/examples/` holds runnable examples: the run above as a self-contained file that
needs no OpenAI key, and one trace recorded across a dispatcher and three worker
processes.
