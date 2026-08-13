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

# Optional
export GIT_SHA=$(git rev-parse --short HEAD)
```

`CONVERGENT_API_KEY` is required. The export works for local development. In a deployment, you'll want the key wherever you store secrets or your environment variables.

`GIT_SHA` is an example environment variable name you can use to describe a release when initializing convergent with: `init(release=...)`. Note that describing a release is optional.

## Instrument with a coding agent

If you're instrumenting Convergent for the first time, the fastest way to do so is give your coding agent this prompt.

```text
Install the convergent-instrument and convergent-verify skills from the github repository convergent-technologies/convergent-sdk. 

convergent-instrument is a skill to instrument your agent, and convergent-verify is a skill to inspect each recording. You will be given the agent file or directory to instrument, as well as instructions / a command that runs the agent to inspect the representative run. Use both skills to instrument and verify your agent. Continue using both skills until the record has no unresolved instrumentation issue.

<Insert agent file or directory to instrument>
<Insert instructions or command to run and inspect the agent>
```

If you've already instrumented with Convergent, and are just looking to verify your setup, give your coding agent this prompt:

```text
Install the convergent-instrument and convergent-verify skills from the github repository convergent-technologies/convergent-sdk.

convergent-instrument is a skill to instrument your agent, and convergent-verify is a skill to inspect each recording. You will be given instructions on the agents/directory that has already been instrumented, as well as instructions / a command that runs the agent(s) to inspect the representative run(s). Use both skills to verify the existing instrumentation, and if necessary re-instrument and fix an existing setup.

<Insert agent file or directory that has been instrumented and should be verified>
<Insert instructions or command to run and inspect the agent>
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

## Full Documentation
The full documentation is under `python/docs/`: confirming a run arrived with `check()`,
instrumenting your agent by hand, attaching to an existing OpenTelemetry setup, the
integration packages, configuration, the API reference, the attribute spellings Convergent
reads, and troubleshooting.

`skills/convergent-instrument/` and `skills/convergent-verify/` are the skills from the
coding-agent section above.

`python/examples/` holds runnable examples: the run above as a self-contained file that
needs no OpenAI key, and one trace recorded across a dispatcher and three worker
processes.
