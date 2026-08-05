---
title: Troubleshooting
description: Symptoms, and the checks that find each cause fastest.
---

`init()` rejects a setup that cannot work: the problem is logged at ERROR and
tracing is disabled, or the process stops at startup under `CONVERGENT_STRICT=1`.
A setup that passes `init()` and still sends
nothing looks like a working one from inside your application. Start with `check()`, which answers the first two
questions on its own.

```python
import convergent

print(convergent.check())
```

Then make sure you can see the SDK's own warnings. It logs to the
`convergent.sdk` logger at WARNING and ERROR, and delivery lines such as
`Queue full, dropping Span.` come from OpenTelemetry's loggers. One root
configuration catches both.

```python
import logging

logging.basicConfig(level=logging.WARNING)
```

If your application raised the root logger above WARNING, none of it reaches
you, because the `convergent.sdk` logger has no level of its own. Set
`CONVERGENT_DEBUG=1`, or pass `debug=True` to `init()`, to give it one. The SDK
writes nothing at DEBUG level, so that cannot make a working setup noisier. It
covers the `convergent.sdk` logger only, so the OpenTelemetry lines still need the
root logger or a level of their own on an `opentelemetry` logger.

## Nothing is arriving

Work down this list.

### Nothing was configured

`init()` rejects a configuration with no key and no destination, or with no
release: the problem is logged at ERROR, tracing is disabled, and `check()`
prints `reason invalid_config`. Under `CONVERGENT_STRICT=1` the same problem
raises at startup instead.
`check()` prints `reason missing_config` when neither `init()` nor `install()`
has run in the process, and also after a strict-mode raise, because a call that
raised configured nothing and recorded nothing.

### The traced code never ran

An `agent()` function that was never called produces nothing. Put a print next
to it.

### The process exited before the spans shipped

Export happens on a background thread every five seconds. A normal interpreter
exit still ships the queue, because `init()` registers an exit hook that drains
it. A serverless freeze, `os._exit()`, or `SIGKILL` skips that hook, so call
`flush()` before you exit there.

```python
result = convergent.flush()
print(result.ok, result.pending, result.dropped)
```

> **Note:** `flush()` does not include a span that has not ended yet. A `flush()` written at the
> end of an `agent()` body misses that run's own span, because the decorator ends the span after
> the body returns. Flush after the function returns. One warning per process says so when it
> happens.

### The key was rejected

A bad key logs `deployment registration failed` first. That line names no status
code, so it reads like a version problem. `check()` tells the two apart in one
call, because it prints a failed round trip with the status code.

```
convergent: enabled
  release     x
  mode        a tracer provider we created
  sending to  convergent

  round trip  failed (http_401)   endpoint https://ingest.convergent.dev
```

The clearer line, `disabled because the collector rejected its credentials`,
comes from the span exporter, so it appears only after the process tries to send
a batch. A process that fails registration and then records nothing never prints
it.

Sending stays off for the life of the process. A corrected key in the
environment does not re-arm it, and neither does a second `init()`. Restart the
process after fixing the credential.

A 401 with a key you know is good is usually `OTEL_EXPORTER_OTLP_HEADERS`. Its
headers are added to every request on top of the key the SDK sets, so an
`authorization` entry there replaces the credentials and every export comes back
401. The SDK logs a warning naming that variable when it sees one, so leave it
unset. `OTEL_EXPORTER_OTLP_TRACES_HEADERS` does the same and takes precedence over
both, and the SDK does not warn about that one, so check for it too.

### You are looking at the wrong workspace

The API key decides which workspace receives the trace. Nothing errors if you
are looking at another one. `check()` prints the organization the key belongs to
on its `key` line.

## One run arrives as many traces

Thirty seven model calls arrive as thirty eight traces, one for the agent and
one for each call.

A span joins a trace by inheriting the active OpenTelemetry context. With no
active span when a call starts, that call becomes the root of a new trace. There
are three ways to get there.

### The model call is outside every span

`init()` on its own records nothing. It configures where spans go, and something
still has to open the span the model calls nest inside.

```python
# Every call is its own trace.
convergent.init(release=os.environ["GIT_SHA"])

for question in questions:
    litellm.completion(model="gpt-4.1-mini", messages=[{"role": "user", "content": question}])
```

Wrap the run.

```python
@convergent.agent(name="convergent-demo")
def answer_all(questions: list[str]) -> None:
    for question in questions:
        litellm.completion(
            model="gpt-4.1-mini", messages=[{"role": "user", "content": question}]
        )
```

### The call runs on another thread

The OpenTelemetry context is a context variable. `asyncio` tasks inherit it, so
`asyncio.gather` over async calls stays in one trace. A thread does not inherit
it, so a call handed to `ThreadPoolExecutor` starts empty and roots a new trace.
Carry the context across the boundary yourself.

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

Two processes that should share one trace use the standard `traceparent` header.
Send it from the caller and extract it in the callee with OpenTelemetry's
`TraceContextTextMapPropagator`. There is nothing Convergent specific about it.

To confirm which trace a span landed in, print `convergent.current_trace()` at
the top of the agent and again next to the model call. Two different ids means
the run split.

### A library opened a span that nothing records

Some libraries open an OpenTelemetry span around each unit of work and have
nothing configured to record it. `pydantic-evals` does this around every case.
A span like that carries no valid id, so the next span under it cannot inherit
one and becomes the root of a new trace. The span you opened around the whole run
is then left alone in a trace of its own. The SDK reports this once per process,
because it can see it happen: a span of ours started a new trace while another
span of ours was still open.

```
Convergent started a span as the root of a new trace while another Convergent
span was still open, so this run's spans are splitting into separate traces
instead of joining one.
```

Open the Convergent span outside that library's scope, or drop the wrapper span
the library opens. A parent that a sampler dropped is not this problem, because
it still carries a valid id and the child keeps the trace.

## Some agents are missing

If you passed `agents=[...]`, an agent you did not name is dropped inside your
process before anything is sent. The `agents` line `check()` prints is the
server's list of linked agents, not your local declaration, so compare the list
in the `init()` call against the `gen_ai.agent.name` each span carries.

An agent run that never names itself is also dropped, because a span with no
parent is kept only by name.

## Too many agents are listed

A per request string reached an agent name. Names must stay stable, like a class
name. Put the varying part in an attribute.

A mistake in a value you passed is logged once per reason, so a loop passing
`f"agent-{uuid4()}"` produces one line rather than one per span, and only the first
offending value reaches your logs.

## Traces have no version

Deployment registration runs in the background after the first span, so a
deployment can take a moment to appear. If it never appears, registration
failed, which logs `deployment registration failed`. Traces still arrive, and
they still carry your `release`. A missing `release` cannot be the cause, because
`init()` rejects a configuration without one, and raises under strict mode.

## Prompts are missing from the spans

Content written by a framework's own instrumentation has its own switch. Each
framework's integration page names it.

## An attribute did not appear

`set_attribute()` drops any key starting with `convergent.` and the eight keys
the SDK owns, and logs one line naming the key. The eight are
`gen_ai.operation.name`, `gen_ai.agent.name`, `gen_ai.agent.version`,
`gen_ai.tool.name`, `gen_ai.input.messages`, `gen_ai.output.messages`,
`gen_ai.tool.call.arguments`, and `gen_ai.tool.call.result`.

## The run has no model call in it

An instrumentation package that writes onto a span that already ended loses
everything it wrote, and logs `Setting attribute on ended span.` for each
attribute. On litellm this is what `USE_OTEL_LITELLM_REQUEST_SPAN` prevents, and
the litellm integration page covers it.

## Spans were recorded but never delivered

A network export retries and can then discard the batch with an error log, and
your application keeps running either way. That discard turns `FlushResult.ok`
off and adds the batch's spans to `FlushResult.dropped`. A false `ok` or a
`dropped` above zero means spans were lost, and a true `ok` means everything that
flush drained was delivered. The exporter's error log says which batch failed.

`FlushResult.dropped` counts spans thrown away since the last call, both the ones
OpenTelemetry threw away because a queue was full and the ones an export failed to
deliver. The queue is bounded at 2048 and `OTEL_BSP_MAX_QUEUE_SIZE` raises it.

Rejected credentials are the one loss these numbers do not carry. Once the
collector has answered 401 or 403 the SDK stops sending, and the batches it holds
back after that are not counted. The `disabled because the collector rejected its
credentials` line is what reports it, and `check()` prints the status code.

## Seeing what you are sending

```bash
export CONVERGENT_TRACES_EXPORTER=console
```

That adds a console destination rather than replacing the others, so you keep
sending to Convergent while you read the spans. `convergent.File("/data/traces")`
writes them to disk for the same reason.
