---
title: Get started
description: Install the SDK, set your key, and confirm a run reaches your workspace.
---

This page covers installing the SDK, setting your key, recording one agent run,
and confirming that the run reached your workspace.

### 1. Install

```bash
pip install convergent-sdk
```

With uv, `uv add convergent-sdk`. With poetry, `poetry add convergent-sdk`.

The package brings the OpenTelemetry pieces it needs.
[What gets installed](configuration.md#what-gets-installed) lists them.

### 2. Set your key

Mint an ingestion key at
[app.convergent.dev/workspace/settings](https://app.convergent.dev/workspace/settings).

```bash
export CONVERGENT_API_KEY="cvk_xxxxxxxxxxxxxxxx"
export GIT_SHA=$(git rev-parse --short HEAD)
```

The export is the local form. In a deployment the key belongs in your secrets
manager or your platform's environment configuration.

`GIT_SHA` is the release. It links a trace to the version of your code that
produced it, and it is how you compare two versions later. A git sha is the usual
choice, and any string that names a version works: a build id, an image tag, a
date.

### 3. Trace a run

Call `init()` once at startup, and put `agent()` on the function that handles one
request. The model call inside it is the one your app already makes, and this one
is the OpenAI client. Save it as `app.py`.

```python
import os

from openai import OpenAI

import convergent

convergent.init(release=os.environ["GIT_SHA"])

client = OpenAI()
MODEL = "gpt-5.5"


@convergent.agent(name="convergent-demo")
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
python app.py
```

That records one agent run with one model call inside it, carrying the prompt,
the answer, the model, and the token counts. The counts come off the response, so
the span has to be open around the call that returns it. Any other model client
goes in the same place, and the trace keeps the same shape.

### 4. Confirm it arrived

`init()` rejects a setup that cannot work: a missing key, a missing release, a
bad value. By default the problem is logged at ERROR and tracing is disabled;
set `CONVERGENT_STRICT=1` to make it raise and stop the process at startup
instead. A setup it accepts can still fail to deliver
quietly, and `check()` reports what
this process configured and then asks the server what it can see.

Call it after `init()` has run. It reads the configuration of the process it is
called from, so a separate script or a fresh shell reports `disabled` however
well your app is set up.

```python
print(convergent.check())
```

```
convergent: enabled
  release     f114ac54b
  mode        a tracer provider we created
  sending to  convergent

  round trip  ok (146ms)
  key         org_30f981e207bf3b38
  agents      convergent-demo

  no notes
```

- `round trip ok` means the server answered this process.
- `key` shows the organization the key belongs to.
- `agents` lists the agent names the server has seen from you, so your own name
  appearing there is the proof that the trace arrived.
- `no notes` means nothing needs your attention.

The `release` line echoes the version you set in step 2.

Anything else, and [Troubleshooting](troubleshooting.md) reads the report for you.

### 5. Look at it

Open [app.convergent.dev/workspace](https://app.convergent.dev/workspace).

Click the run to see the agent span with the model call nested under it, and the
model call carrying the prompt, the answer, and its token counts.

## What to read next

- [Instrument your agent](instrument.md): tools, sub-steps, and recording what went in and out.
- [Instrument with a coding agent](agent-skill.md): the skill that tells a coding agent how to add the tracing.
- [Already using OpenTelemetry](opentelemetry.md): what `init()` does when a tracer provider already exists, and the agent filter.
- [Configuration](configuration.md): environment variables, destinations, and runtime behavior.
- [Troubleshooting](troubleshooting.md): nothing arriving, split traces, missing agents.
