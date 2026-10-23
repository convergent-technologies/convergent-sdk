# convergent-sdk

Records the runs of your TypeScript and JavaScript AI agents on Node.js as
OpenTelemetry spans and sends them to your Convergent workspace. It records
agent runs, model calls, and tool calls.

## Install

```sh
npm install convergent-sdk @opentelemetry/api
```

The SDK needs Node.js 22.15 or newer. It is an ES module, and both `import`
and `require` load it.

`@opentelemetry/api` is a peer dependency. Install it next to the SDK so your
application and the SDK share one copy.

## Set your key and release

Mint an ingestion key at
[app.convergent.dev/workspace/settings](https://app.convergent.dev/workspace/settings).

```sh
export CONVERGENT_API_KEY="<your ingestion key>"
export CONVERGENT_RELEASE=$(git rev-parse --short HEAD)
```

`init()` reads both. `CONVERGENT_API_KEY` is required to send spans to
Convergent. A local `File` destination needs no key. Store ingestion keys with
your deployment's other secrets.

A release is required. Use a Git SHA, a build ID, or an image tag. You can also
pass it as `init({ release: "..." })`.

## Example: Trace a run

Call `init()` once at startup. Wrap the function that handles one request in
`agent()`, and put each model or tool call inside `span()`.

```ts
import { agent, flush, init, span } from "convergent-sdk";

init();

const answer = agent({ name: "order_support" }, async (question: string) => {
  return await span({ name: "gpt-5.5", operation: "model_call", model: "gpt-5.5" }, async (call) => {
    call.setInput(question);
    const text = await model(question);
    call.setOutput(text);
    return text;
  });
});

await answer("Where is order 1042?");
await flush();
```

This records one agent run with one model call inside it, carrying the prompt
and the answer. `model()` stands for the model call your app already makes.
The `await flush()` sends the spans before the script exits.

## Build from source

The package is built from the `typescript/` directory of the SDK repository.
It has its own dependencies and lockfile.

```sh
pnpm --ignore-workspace install
pnpm build
```

`pnpm pack` builds the package before creating the tarball. `pnpm test` installs
that tarball into a temporary application and checks that `import` and `require`
share one configuration and record one connected trace.

Set `CONVERGENT_API_KEY` and `CONVERGENT_RELEASE`, then run the quickstart:

```sh
node examples/quickstart/main.mjs
```

The [quickstart](https://github.com/convergent-technologies/convergent-sdk/blob/main/typescript/examples/quickstart/main.mjs)
records one agent run that calls a tool and a scripted model, prints the trace
ID, and flushes. It makes no model provider request.

## Documentation

- [Get started](https://github.com/convergent-technologies/convergent-sdk/blob/main/typescript/docs/index.md): install, set your key, and trace your first run.
- [Instrument with a coding agent](https://github.com/convergent-technologies/convergent-sdk/blob/main/docs/coding-agent.md): let a coding agent add and check the tracing.
- [OpenTelemetry](https://github.com/convergent-technologies/convergent-sdk/blob/main/typescript/docs/opentelemetry.md): add Convergent to an OpenTelemetry setup you already have.
- [Attach files to spans](https://github.com/convergent-technologies/convergent-sdk/blob/main/docs/attach-files.md): record the files a span read or wrote.
- [Span filters](https://github.com/convergent-technologies/convergent-sdk/blob/main/docs/span-filters.md): choose which spans are sent.
- [Local files](https://github.com/convergent-technologies/convergent-sdk/blob/main/docs/local-files.md): write spans to a file or the console.
- [Configuration](https://github.com/convergent-technologies/convergent-sdk/blob/main/typescript/docs/configuration.md): every option and environment variable.
- [API reference](https://github.com/convergent-technologies/convergent-sdk/blob/main/typescript/docs/reference/api.md): every public call.
- [Attributes](https://github.com/convergent-technologies/convergent-sdk/blob/main/docs/attributes.md): the attribute spellings Convergent reads.
- [Troubleshooting](https://github.com/convergent-technologies/convergent-sdk/blob/main/docs/troubleshooting.md): what to do when a run does not arrive.
- [Changelog](https://github.com/convergent-technologies/convergent-sdk/blob/main/typescript/CHANGELOG.md): what changed in each version.

The repository's `typescript/examples/` directory has runnable examples: the
quickstart, [sessions](https://github.com/convergent-technologies/convergent-sdk/blob/main/typescript/examples/sessions/main.mjs),
an [existing OpenTelemetry setup](https://github.com/convergent-technologies/convergent-sdk/blob/main/typescript/examples/existing-otel/main.mjs),
and [Mastra](https://github.com/convergent-technologies/convergent-sdk/blob/main/typescript/examples/mastra/README.md).
