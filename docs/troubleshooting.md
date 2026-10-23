---
title: Troubleshooting
description: What to check when traces are missing, split, or incomplete.
---

Start with `check()`. It shows whether tracing is on, where spans go, and
whether the server accepts your key.

```python tab="Python"
import convergent

print(convergent.check())
```

```ts tab="TypeScript"
import { check } from "convergent-sdk";

console.log(String(await check()));
```

A bad setup does not crash your app. `init()` logs the problem and turns
tracing off. Set `CONVERGENT_STRICT=1` to stop at startup instead.

Next, make sure you can see the SDK's warnings.

```python tab="Python"
import logging

logging.basicConfig(level=logging.WARNING)
```

```ts tab="TypeScript"
import { diag, DiagConsoleLogger, DiagLogLevel } from "@opentelemetry/api";

diag.setLogger(new DiagConsoleLogger(), DiagLogLevel.WARN);
```

If your app hides warnings and you cannot change that, set
`CONVERGENT_DEBUG=1`. It shows the SDK's own warnings. In TypeScript, it also
shows OpenTelemetry's export errors, unless your app already set a logger.

## Nothing is arriving

Check these causes in order.

### Setup did not run or was rejected

When tracing is off, `check()` prints a `reason` line.

- `missing_config`: setup never ran in this process, or a strict mode call
  threw before it configured anything.
- `invalid_config`: the configuration cannot work. For example, it has no key
  and no local destination, or no release. The logged warning names the
  problem.

### Another OpenTelemetry provider is registered (TypeScript)

`reason existing_provider` means OpenTelemetry refused the SDK's provider.
Either another library registered one first, or two copies of
`@opentelemetry/api` are loaded. Run `npm ls @opentelemetry/api` to find a
second copy. A bundled copy does not show up there. To keep your own provider,
add `ConvergentSpanProcessor` to it. See
[OpenTelemetry](../typescript/docs/opentelemetry.md).

### Your provider does not carry the processor (TypeScript)

If you add `ConvergentSpanProcessor` to your own provider, register that
provider globally with `provider.register()` before you call the helpers.
Otherwise `agent()`, `tool()`, `span()`, and `observe()` record nothing and log
one warning.

### A filter withheld them

An empty `agents` list sends nothing, and so does a require rule with an empty
list of values. For anything else, see
[Some agents are missing](#some-agents-are-missing) and
[Spans are missing under the attribute filters](#spans-are-missing-under-the-attribute-filters).

### The traced code never ran

An agent function that is never called sends nothing. Add a print next to it to
confirm it runs.

### The process exited before the spans shipped

The SDK sends spans in the background, and a normal exit sends what is left. A
crash, a forced exit, `SIGKILL`, or a serverless platform freezing the process
skips that step. Call `flush()` yourself in those cases.

Call it after the traced function returns. A span that has not ended is not
sent, so a `flush()` inside an `agent()` body misses that run. The SDK warns
once when this happens. In a serverless function, flush in the outer handler.

```python tab="Python"
def handler(event, context):
    try:
        return answer(event)  # answer is wrapped with @convergent.agent
    finally:
        convergent.flush()
```

```ts tab="TypeScript"
export async function handler(event) {
  try {
    return await answer(event); // answer is wrapped with agent()
  } finally {
    await flush();
  }
}
```

If you record files, pass `uploads=True` (Python) / `{ uploads: true }`
(TypeScript) so the flush also waits for them.

### The server stops on SIGTERM (TypeScript)

The SDK does not handle `SIGTERM`. In an HTTP server, stop taking requests,
flush, then exit.

```ts
process.once("SIGTERM", () => {
  server.close(async () => {
    await flush();
    process.exit(0);
  });
});
```

### The key was rejected

`check()` prints the status code, for example
`round trip  failed (http_401)`. After the server answers 401 or 403, the SDK
stops sending for the life of the process. Fix the key, then restart.

In Python, the first log line for a bad key is often
`deployment registration failed`. It looks like a version problem. Trust
`check()` instead.

### An OTLP headers variable replaced the key (Python)

If a good key gets a 401, check `OTEL_EXPORTER_OTLP_HEADERS` and
`OTEL_EXPORTER_OTLP_TRACES_HEADERS`. An `authorization` entry in either one
replaces the key. Leave both unset.

### You are looking at the wrong workspace

The API key decides which workspace gets the trace. The `key` line in `check()`
names the organization the key belongs to.

## One run arrives as many traces

For example, a run with thirty seven model calls arrives as thirty eight
traces. A call joins a trace only if a span is active when it starts.

To confirm, print `current_trace()` (Python) / `currentTrace()` (TypeScript) at
the top of the agent and next to the model call. Two different trace ids mean
the run split.

### The model call is outside every span

`init()` only sets where spans go. Your code still has to open the span that
the model calls sit inside. Wrap the run.

```python tab="Python"
@convergent.agent(name="convergent-demo")
def answer_all(questions: list[str]) -> None:
    for question in questions:
        litellm.completion(
            model="gpt-4.1-mini", messages=[{"role": "user", "content": question}]
        )
```

```ts tab="TypeScript"
const answerAll = agent({ name: "convergent-demo" }, async (questions: string[]) => {
  for (const question of questions) await model(question);
});
```

### The call runs on another thread (Python)

`asyncio` tasks keep the active span. Threads do not, so a call handed to
`ThreadPoolExecutor` starts a new trace. Carry the context across yourself.

```python
from concurrent.futures import ThreadPoolExecutor

from opentelemetry import context


@convergent.agent(name="convergent-demo")
def answer_all(questions: list[str]) -> list[str]:
    parent = context.get_current()

    def ask(question: str) -> str:
        token = context.attach(parent)
        try:
            return answer(question)
        finally:
            context.detach(token)

    with ThreadPoolExecutor() as pool:
        return list(pool.map(ask, questions))
```

### Your provider loses the span across await (TypeScript)

`init()` sets this up for you. If you own the provider, its context manager
must carry the active span across `await`. `NodeTracerProvider` sets one up
when you call `provider.register()`.

### The run crosses into another process

Send the standard `traceparent` header from the caller and read it in the
callee with OpenTelemetry's propagation API. See OpenTelemetry for
[Python](../python/docs/opentelemetry.md#traces-that-cross-processes) or
[TypeScript](../typescript/docs/opentelemetry.md#traces-that-cross-processes).

### A library opened a span that nothing records (Python)

Some libraries open a span around each unit of work with nothing set up to
record it, for example `pydantic-evals` around every case. Spans under it start
new traces. The SDK warns once that `this run's spans are splitting into
separate traces`. Open the Convergent span outside that library's scope, or
turn off the library's span.

## Some agents are missing

If you set `agents`, the SDK drops every agent you did not name, and every top
level span with no agent name. Compare your list with the `gen_ai.agent.name`
on each span. The `agents` line in `check()` is the server's list, not yours.

An agent that runs in two processes needs `agents` set in both. With Mastra in
TypeScript, leave `agents` unset, because Mastra names the agent too late.

## Spans are missing under the attribute filters

`require_span_attributes=` (Python) / `requireSpanAttributes` and
`reject_span_attributes=` (Python) / `rejectSpanAttributes` withhold spans
quietly. These causes look the same from outside. See
[Span filters](span-filters.md) for how the rules work.

**The value does not match exactly.** Type and case both count. `"Acme"` does
not match `"acme"`, and `true` does not match `1`.

**The attribute holds a list.** A list never matches, so a reject rule cannot
exclude the span and a require rule withholds it. In Python, an enum member
never matches either. Record its `.value`.

**No source held the key when the span ended.** The filter looks at the
request mark, then the span's attributes, then the resource attributes. Mark
the request with `context_attributes=` (Python) / `contextAttributes`, write
the attribute on the span, or set it on the resource. An attribute on one span
does not reach its children, so use a request mark for a value like a customer
id.

**The request crossed into another process.** Request marks stay in the
process that set them. Each service marks its own requests and sets its own
filters.

**The span carried a rejected pair.** One matching pair withholds the span. A
pair in both rules counts as rejected, and the SDK warns at startup.

**The marks could not be read (TypeScript).** A resolver that throws, an async
resolver, or a value that is not a plain object withholds that span and its
children. Look the value up inside the function and call
`setContextAttributes()` instead.

**The span started from a context you passed in (Python).** The SDK reads the
mark from that context, so the span misses it. A span or resource attribute can
still supply the key.

**The span went over its attribute limit (Python).** Past 128 attributes,
OpenTelemetry drops the oldest one, which can be the mark. Raise
`OTEL_ATTRIBUTE_COUNT_LIMIT`, or record fewer attributes.

**The filter failed.** The SDK withholds the span and logs
`Convergent could not decide whether a span may be sent` once.

## Too many agents are listed

A value that changes per request reached an agent name. Keep names stable, like
a class name, and put the changing part in an attribute.

## Traces have no version (Python)

The SDK registers the deployment in the background after the first span, so it
can take a moment to appear. If it never does, look for
`deployment registration failed` in your logs. Traces still arrive with your
release.

## Prompts are missing from the spans

A framework's own instrumentation has its own switch for recording prompts.
Each framework's integration page names it.

## An attribute did not appear

`set_attribute()` (Python) / `setAttribute()` (TypeScript) ignores keys that
start with `convergent.` and the `gen_ai.*` keys the SDK writes itself, and
logs a warning. See [Attributes](attributes.md).

## The run has no model call in it (Python)

An instrumentation package that writes onto a span after it ended loses what it
wrote, and logs `Setting attribute on ended span.` On litellm, set
`USE_OTEL_LITELLM_REQUEST_SPAN`. The litellm integration page covers it.

## Spans were recorded but never delivered

A failed export is retried, then the batch can be dropped with a warning. Spans
are also dropped when the queue is full. It holds 2,048 spans. Raise it with
`OTEL_BSP_MAX_QUEUE_SIZE`. After a rejected key, every later batch is dropped.

```python tab="Python"
result = convergent.flush()
print(result.ok, result.dropped)
```

```ts tab="TypeScript"
const { ok } = await flush();
```

In Python, a false `ok` or a `dropped` above zero means spans were lost since
the last call.

In TypeScript, a false `ok` means the flush failed or timed out. A true `ok`
does not prove delivery, and there is no dropped count. Read the warnings.

### The server refused a batch as too large (TypeScript)

The SDK warns once when the server answers 413. That batch is lost and later
ones still go. Lower `OTEL_BSP_MAX_EXPORT_BATCH_SIZE` to send fewer spans per
request. A single span that is too large needs smaller content.

## Seeing what you are sending

```bash
export CONVERGENT_TRACES_EXPORTER=console
```

This prints each span and keeps sending to Convergent.
`CONVERGENT_SPANS_DIR=<dir>` writes them to `spans.jsonl` in that folder
instead. Both write every recorded prompt and output. See
[Local files](local-files.md).
