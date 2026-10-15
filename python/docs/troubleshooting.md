---
title: Troubleshooting
description: Symptoms, causes, and the fastest check for each.
---

`init()` rejects a setup that cannot work. It logs the problem at ERROR and
disables tracing, or stops the process at startup under `CONVERGENT_STRICT=1`.
A setup that passes `init()` but sends nothing looks normal from inside your
application. Start with `check()`, which answers the first two questions on its
own.

```python
import convergent

print(convergent.check())
```

Next, make sure you can see the SDK's warnings. The SDK logs to the
`convergent.sdk` logger at WARNING and ERROR. Delivery lines such as
`Queue full, dropping Span.` come from OpenTelemetry's loggers. One root
configuration catches both.

```python
import logging

logging.basicConfig(level=logging.WARNING)
```

The `convergent.sdk` logger has no level of its own. If your application set
the root logger above WARNING, you see none of these lines. To fix that, set
`CONVERGENT_DEBUG=1` or pass `debug=True` to `init()`. This gives the logger a
level of its own. The SDK writes nothing at DEBUG, so a working setup gets no
noisier. The setting covers only the `convergent.sdk` logger. The OpenTelemetry
lines still need the root logger, or a level set on an `opentelemetry` logger.

## Nothing is arriving

Check these causes in order.

### The require_span_attributes= or reject_span_attributes= filter withheld them

If `init(require_span_attributes=...)` is set, confirm that the running
requests are marked with `context_attributes=`. `require_span_attributes={"k": []}`
sends nothing at all. If `init(reject_span_attributes=...)` is set, confirm
that the running requests do not carry a rejected pair. See
[Spans are missing under require_span_attributes= or reject_span_attributes=](#spans-are-missing-under-require_span_attributes-or-reject_span_attributes).

### Nothing was configured

`init()` rejects a configuration with no key and no destination, or with no
release. It logs the problem at ERROR, disables tracing, and `check()` prints
`reason invalid_config`. Under `CONVERGENT_STRICT=1`, the same problem raises
at startup instead.

`check()` prints `reason missing_config` in two cases:

- Neither `init()` nor `install()` has run in the process.
- A strict-mode call raised. A call that raised configured nothing and
  recorded nothing.

### The traced code never ran

An `agent()` function that is never called produces nothing. Add a print next
to it to confirm it runs.

### The process exited before the spans shipped

The SDK exports on a background thread every five seconds. `init()` registers
an exit hook that drains the queue, so a normal interpreter exit still ships
it. A serverless freeze, `os._exit()`, or `SIGKILL` skips that hook. In those
cases, call `flush()` before the process exits.

```python
result = convergent.flush()
print(result.ok, result.pending, result.dropped)
```

> **Note:** `flush()` skips a span that has not ended yet. The decorator ends
> the span after the body returns, so a `flush()` at the end of an `agent()`
> body misses that run's own span. Flush after the function returns. The SDK
> logs one warning per process when this happens.

### The key was rejected

A bad key first logs `deployment registration failed`. That line names no
status code, so it looks like a version problem. Run `check()` to tell the two
apart. It prints a failed round trip with the status code.

```
convergent: enabled
  release     x
  mode        a tracer provider we created
  sending to  convergent

  round trip  failed (http_401)   endpoint https://ingest.convergent.dev
```

The clearer line, `disabled because the collector rejected its credentials`,
comes from the span exporter. It appears only after the process tries to send
a batch. A process that fails registration and then records nothing never
prints it.

Sending stays off for the life of the process. Neither a corrected key in the
environment nor a second `init()` turns it back on. Fix the credential, then
restart the process.

If you get a 401 with a key you know is good, check `OTEL_EXPORTER_OTLP_HEADERS`.
Its headers are added to every request on top of the key the SDK sets. An
`authorization` entry there replaces the credentials, and every export returns
401. The SDK logs a warning naming that variable when it sees one, so leave it
unset. `OTEL_EXPORTER_OTLP_TRACES_HEADERS` has the same effect and takes
precedence over both. The SDK does not warn about it, so check for it too.

### You are looking at the wrong workspace

The API key decides which workspace receives the trace. Nothing errors if you
look at another workspace. The `key` line that `check()` prints names the
organization the key belongs to.

## One run arrives as many traces

For example, a run with thirty seven model calls arrives as thirty eight traces,
one for the agent and one for each call.

A span joins a trace by inheriting the active OpenTelemetry context. If no span
is active when a call starts, that call becomes the root of a new trace. Three
causes lead there.

### The model call is outside every span

`init()` alone records nothing. It configures where spans go. Your code still
has to open the span that the model calls nest inside.

```python
# Every call is its own trace.
convergent.init(release=os.environ["GIT_SHA"])

for question in questions:
    litellm.completion(model="gpt-4.1-mini", messages=[{"role": "user", "content": question}])
```

Fix: wrap the run.

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
`asyncio.gather` over async calls stays in one trace. Threads do not inherit
it. A call handed to `ThreadPoolExecutor` starts with an empty context and
roots a new trace. Fix: carry the context across the boundary yourself.

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

To share one trace across two processes, use the standard `traceparent`
header. Send it from the caller and extract it in the callee with
OpenTelemetry's `TraceContextTextMapPropagator`. Nothing about this is specific
to Convergent.

To confirm which trace a span landed in, print `convergent.current_trace()` at
the top of the agent and again next to the model call. Two different ids mean
the run split.

### A library opened a span that nothing records

Some libraries open an OpenTelemetry span around each unit of work but have
nothing configured to record it, e.g. `pydantic-evals` around every case. That
span carries no valid id. The next span under it cannot inherit one, so it
becomes the root of a new trace. The span you opened around the whole run is
left alone in its own trace. The SDK detects this and reports it once per
process, when one of its spans starts a new trace while another of its spans
is still open.

```
Convergent started a span as the root of a new trace while another Convergent
span was still open, so this run's spans are splitting into separate traces
instead of joining one.
```

Fix: open the Convergent span outside that library's scope, or drop the
wrapper span the library opens. A parent dropped by a sampler is a different
case. It still carries a valid id, so the child stays in the trace.

## Some agents are missing

If you passed `agents=[...]`, the SDK drops any agent you did not name inside
your process, before anything is sent. The `agents` line in `check()` is the
server's list of agents it has recorded, not your local list. Compare the list
in your `init()` call against the `gen_ai.agent.name` on each span.

The SDK also drops an agent run that never names itself, because it keeps a
span with no parent only by name.

## Spans are missing under require_span_attributes= or reject_span_attributes=

The filters withhold spans inside your process and log nothing per span. Eight
causes look the same from outside.

The value does not match exactly. Comparison is by type and case. `1` matches
neither `"1"` nor `True`, and `"Acme"` does not match `"acme"`. A float in
`require_span_attributes=` or `reject_span_attributes=` logs a startup warning
with `may never match`, because an exact match against a recorded float rarely
holds.

The span's attribute holds a list or an enum member. Such a value never
matches, so `reject_span_attributes=` cannot exclude the span and
`require_span_attributes=` withholds it. Record the plain scalar instead, e.g.
`.value` for an enum member.

No source held a required key when the span ended. The filter reads the
stamped `convergent.attributes.<key>` mark first, then the finished span's own
attributes, then the resource attributes. Fix it in one of three ways:

- Mark the request with `context_attributes=` on the `span()` call that wraps it.
- Write the attribute onto the span.
- Set the attribute on the resource.

The span was started from an explicitly passed `Context`, so it missed the
mark. The SDK reads the mark from that context, not the ambient one, and the
mark ends with the marked block. A resource or span attribute can still supply
the key.

The request crossed to a second process without its mark. The
`context_attributes=` pairs stay in the process that set them. The second
service withholds its spans until it marks its own requests and sets its own
filters.

A span carried a rejected pair. `reject_span_attributes=` withholds a span on
any one matching key. It wins over `require_span_attributes=` for a pair named
in both. The SDK logs a contradicting pair at ERROR at startup with
`reject_span_attributes= wins`.

The span exceeded its attribute limit. The default limit is 128. Past it,
OpenTelemetry evicts the span's oldest attribute. The stamped key is among the
oldest, so the span loses it and `require_span_attributes=` withholds it. The
span's `dropped_attributes` count is then above zero. Raise
`OTEL_ATTRIBUTE_COUNT_LIMIT`, or record fewer attributes on one span.

The filter decision failed. An exception while deciding withholds the span and
logs `Convergent could not decide whether a span may be sent` once per process.

## Too many agents are listed

A string that varies per request reached an agent name. Keep names stable, like
a class name, and put the varying part in an attribute.

The SDK logs a bad value once per reason. A loop passing `f"agent-{uuid4()}"`
produces one log line, not one per span, so only the first bad value reaches
your logs.

## Traces have no version

Deployment registration runs in the background after the first span, so a
deployment can take a moment to appear. If it never appears, registration
failed and logged `deployment registration failed`. Traces still arrive and
still carry your `release`. A missing `release` is not the cause, because
`init()` rejects a configuration without one, and raises under strict mode.

## Prompts are missing from the spans

A framework's own instrumentation has its own switch for recording content.
Each framework's integration page names it.

## An attribute did not appear

`set_attribute()` drops any key that starts with `convergent.`, and the eight
keys the SDK owns. It logs one line naming the dropped key. The eight keys are
`gen_ai.operation.name`, `gen_ai.agent.name`, `gen_ai.agent.version`,
`gen_ai.tool.name`, `gen_ai.input.messages`, `gen_ai.output.messages`,
`gen_ai.tool.call.arguments`, and `gen_ai.tool.call.result`.

## The run has no model call in it

An instrumentation package that writes onto an ended span loses everything it
wrote. It logs `Setting attribute on ended span.` for each attribute. On
litellm, `USE_OTEL_LITELLM_REQUEST_SPAN` prevents this. The litellm integration
page covers it.

## Spans were recorded but never delivered

A network export retries, then can discard the batch with an error log. Your
application keeps running either way. A discard turns `FlushResult.ok` off and
adds the batch's spans to `FlushResult.dropped`. A false `ok` or a `dropped`
above zero means spans were lost. A true `ok` means that flush delivered
everything it drained. The exporter's error log names the failed batch.

`FlushResult.dropped` counts spans thrown away since the last call. It includes
spans OpenTelemetry dropped because a queue was full, and spans an export
failed to deliver. The queue holds 2048 spans. Raise it with
`OTEL_BSP_MAX_QUEUE_SIZE`.

Rejected credentials count the same way. Once the collector answers 401 or 403,
the SDK stops sending. The refused batch and every later batch turn `ok` off
and count in `dropped`. The `disabled because the collector rejected its
credentials` line names the cause, and `check()` prints the status code.

## Seeing what you are sending

```bash
export CONVERGENT_TRACES_EXPORTER=console
```

This adds a console destination and keeps the others, so you keep sending to
Convergent while you read the spans. `convergent.File("/data/traces")` does the
same but writes the spans to disk.
