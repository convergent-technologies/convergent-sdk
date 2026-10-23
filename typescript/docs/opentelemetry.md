---
title: OpenTelemetry
description: Add Convergent to an app that already sets up OpenTelemetry.
---

`init()` creates its own tracer provider and registers it for the process. If
your app already registers a provider, `init()` cannot register a second one
and returns `reason: "existing_provider"`. Add `ConvergentSpanProcessor` to
your provider instead, as shown below.

Your other processors and exporters do not change. They also receive the
prompts and answers that the SDK records.

Set `gen_ai.operation.name` in the call that starts a span, for example
`tracer.startActiveSpan("answer", { attributes: { "gen_ai.operation.name": "chat" } }, fn)`.
Convergent treats a span as agent work only when the span has
`gen_ai.operation.name` at the moment it starts.

## No init() call at all

Add `ConvergentSpanProcessor` next to your existing processor when you build
the provider. Then register the provider.

```ts
import { NodeTracerProvider } from "@opentelemetry/sdk-trace-node";
import { ConvergentSpanProcessor, flush } from "convergent-sdk";

const convergentProcessor = new ConvergentSpanProcessor({ release: "2026.10.06.1" });
const provider = new NodeTracerProvider({
  spanProcessors: [existingProcessor, convergentProcessor],
});
provider.register();

// Record application work with Convergent helpers or OpenTelemetry.
await flush();
await provider.shutdown();
```

Follow these rules:

- Build the processor before the provider. OpenTelemetry for JavaScript 2.x
  has no `addSpanProcessor()`, so the processor goes in the provider's
  `spanProcessors` list.
- Register the provider globally before you call `agent()`, `tool()`,
  `observe()`, or `span()`. These helpers always use the global provider. The
  processor takes no `tracerProvider` option, so a private provider that is
  never registered does not work with the helpers.
- The processor needs an API key. It takes the same options and environment
  variables as `init()`, except `debug`, `destinations`, and their variables.
  It sends only to Convergent, because your app owns its other exporters.
- Your app owns the provider's context manager, propagator, resources, and
  shutdown. Your context manager must carry context across `await`, or request
  marks and sessions are lost between steps.

If the registered provider does not carry the processor, the helpers record
nothing and the SDK logs one warning. Spans your own tracers record on the
provider that does carry the processor still reach Convergent.

Building the processor makes no network request and registers nothing
globally.

### Only one setup per process

The first successful setup wins, whether it is `init()` or a processor.

- A valid processor claims the process even if you never add it to a
  provider. The claim is never released, so a later `init()` or processor stays
  inactive.
- After a processor is set up, `init()` returns `enabled: true`,
  `mode: "attached"`, and `reason: "already_configured"`, with the processor's
  release, destinations, and filters. After a successful `init()`, the mode is
  `"owned"`.
- A later processor logs one warning and stays inactive.
- A processor with an invalid configuration logs the reason, stays inactive,
  and lets a later valid attempt succeed. With strict mode on, it throws
  instead.

### What your other exporters see

The SDK adds its own metadata, the session ID, and request marks only to the
copy it sends to Convergent. If an agent span from another library has no
`gen_ai.agent.version`, the copy gets your release there.

Attributes that the helpers or your code write stay on the original span, and
your other exporters see them. That includes the agent options and the content
from `setInput()` and `setOutput()`.

### Flush and shut down

`flush()` and `convergentProcessor.shutdown()` affect only Convergent. Your
other exporters keep working. Calling the processor's `shutdown()` again is
safe. When your app exits, still call `provider.shutdown()`. `flush()` never
shuts down your provider.

`ConvergentSpanProcessor.forceFlush()` and `shutdown()` reject when an export
fails. Repeated `shutdown()` calls share one shutdown.

If your resource has asynchronous attributes, await
`resource.waitForAsyncAttributes()` before the final flush. Otherwise a batch
can wait on those attributes, and `flush()` returns without it.

Run the
[existing OpenTelemetry example](https://github.com/convergent-technologies/convergent-sdk/blob/main/typescript/examples/existing-otel/main.mjs)
after you build the package and set the same variables as the quickstart:

```sh
node examples/existing-otel/main.mjs
```

## Filtering what is sent

By default, Convergent receives every span the process records. The `agents`,
`requireSpanAttributes`, and `rejectSpanAttributes` options choose which spans
Convergent gets. They apply only to Convergent, not to your other exporters.
[Span filters](../../docs/span-filters.md) explains how to use them.

## Pass the provider yourself

Some libraries take a tracer provider as an argument. Pass them
`tracerProvider()`:

```ts
import { init, tracerProvider } from "convergent-sdk";

init();
const provider = tracerProvider();
```

It returns the standard OpenTelemetry `TracerProvider` interface. After
`init()`, it is the provider `init()` created. With a `ConvergentSpanProcessor`,
it is OpenTelemetry's global provider, and the SDK leaves your resource
attributes unchanged. Before setup succeeds, it returns a provider that sends
nothing.

To call provider-wide methods such as `forceFlush()` and `shutdown()`, keep a
reference to your own provider.

[Mastra](https://github.com/convergent-technologies/convergent-sdk/blob/main/typescript/examples/mastra/README.md)
takes a provider this way. Its bridge adds the agent name only when a span
ends, so leave the `agents` filter unset when you use the bridge. Mastra's
native OTLP exporter is the other path. It sends to Convergent directly and
skips the SDK, so SDK filters, sessions, and `CONVERGENT_SPANS_DIR` do not
apply and no local file is written. With the Mastra versions in the examples,
model spans carry no `gen_ai.input.messages` on either path.

## Traces that cross processes

To keep one trace across two services, pass the OpenTelemetry trace context
with the work. `init()` registers OpenTelemetry's standard W3C trace context
propagator. Over HTTP, install an instrumentation package on both ends. Over a
queue, use `propagation.inject()` in the producer and `propagation.extract()`
in the consumer.

```ts
import { context, propagation } from "@opentelemetry/api";

// Producer
const carrier: Record<string, string> = {};
propagation.inject(context.active(), carrier);
queue.send({ body: payload, headers: carrier });

// Consumer
await context.with(propagation.extract(context.active(), message.headers), () =>
  handle(message.body),
);
```

Set up the SDK in each process. Filters, sessions, and request marks do not
travel between processes.
