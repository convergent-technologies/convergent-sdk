---
title: TypeScript SDK
description: Set your key, instrument your agent with a coding agent, or install the SDK by hand.
---

The SDK records agent runs, model calls, and tool calls in Node.js as
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
npm install convergent-sdk @opentelemetry/api
```

Use your own package manager's command, such as `pnpm add` or `yarn add`. The
SDK needs Node.js 22.15 or newer. It is an ES module, and both `import` and
`require` load it.

`@opentelemetry/api` is a peer dependency, version 1.9. Install it next to the
SDK so your application and the SDK share one copy.

`init()` also needs a release, which names the deployed version of your code.
Convergent groups recordings by release. Use a Git SHA, a build ID, or an image
tag. Without a release, `init()` turns tracing off.

```bash
export CONVERGENT_RELEASE=$(git rev-parse --short HEAD)
```

You can also pass it as `init({ release: "..." })`.

### Example: Trace a run

Call `init()` once at startup. Wrap the function that handles one request in
`agent()`. Inside it, keep the model call your app already makes. This example
uses the OpenAI client. Save it as `app.mjs`.

```js
import OpenAI from "openai";
import { agent, flush, init, span } from "convergent-sdk";

init();

const client = new OpenAI();
const MODEL = "gpt-5.5";

const answer = agent({ name: "support-agent" }, async (question) => {
  return span({ name: MODEL, operation: "model_call", model: MODEL }, async (call) => {
    call.setInput(question);
    const completion = await client.chat.completions.create({
      model: MODEL,
      messages: [{ role: "user", content: question }],
    });
    const reply = completion.choices[0].message.content;
    call.setOutput(reply);
    call.setAttribute("gen_ai.usage.input_tokens", completion.usage.prompt_tokens);
    call.setAttribute("gen_ai.usage.output_tokens", completion.usage.completion_tokens);
    return reply;
  });
});

console.log(await answer("Where is my invoice?"));
await flush();
```

Run it:

```bash
npm install openai
export OPENAI_API_KEY="<your OpenAI key>"
node app.mjs
```

This records one agent run with one model call inside it. The model call holds
the prompt, the answer, the model, and the token counts. Another model client
goes in the same place and gives the same trace shape.

The `await flush()` at the end sends the spans before the script exits.

## Next steps

- [Instrument with a coding agent](../../docs/coding-agent.md): let a coding agent add and check the tracing.
- [OpenTelemetry](opentelemetry.md): add Convergent to an OpenTelemetry setup you already have.
- [Attach files to spans](../../docs/attach-files.md): record the files a span read or wrote.
- [Span filters](../../docs/span-filters.md): choose which spans are sent.
- [Local files](../../docs/local-files.md): write spans to a file or the console.
- [Configuration](configuration.md): every option and environment variable.
- [API reference](reference/api.md): every public call.
- [Attributes](../../docs/attributes.md): the attribute spellings Convergent reads.
- [Troubleshooting](../../docs/troubleshooting.md): what to do when a run does not arrive.
- [Changelog](https://github.com/convergent-technologies/convergent-sdk/blob/main/typescript/CHANGELOG.md): what changed in each version.

The [SDK repository](https://github.com/convergent-technologies/convergent-sdk)
has the source and runnable examples. The
[quickstart](https://github.com/convergent-technologies/convergent-sdk/blob/main/typescript/examples/quickstart/main.mjs)
records one agent run with a tool call and a scripted model, and needs no
model provider key.
