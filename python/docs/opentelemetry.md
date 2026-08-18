---
title: Already using OpenTelemetry
description: Add Convergent to an app that already produces OpenTelemetry spans.
---

If your agent already produces OpenTelemetry spans, `init()` attaches Convergent
to the tracer provider you already have.

```python
import os

import convergent

convergent.init(
    release=os.environ["GIT_SHA"],
    agents=["convergent-demo", "billing-agent"],
)
```

When a tracer provider already exists, `init()` adds its own span processors to
it and leaves everything else alone. Your resource attributes, your samplers,
and your exporters are untouched, so your own backend keeps receiving what it
received before.

Your own exporters also receive the SDK's spans, including the content
`set_input()` and `set_output()` wrote. Adding a span processor to a provider
leaves every processor already on it receiving everything, which is
OpenTelemetry's own contract, so the prompts and answers you record reach your
collector as well as ours.

`Status.mode` reads `attached` then. A provider's resource attributes are already
fixed by the time the SDK attaches, so the deployment identity cannot go there.
The SDK puts it on each span instead, and a trace from an attached process links
to its deployment either way.

## Filtering what is sent

By default we receive every span the process records. That is usually right when
the process has no other OpenTelemetry setup.

To narrow it, pass `agents=[...]` with your agent names. It is the list of agent
names Convergent is allowed to see, and it is worth passing when `init()` attaches
to a provider you already had, or your web server and database spans reach us
along with your agents.

With the filter on, a span is sent when it names one of those agents, or when it
runs inside a span that named one. That covers the model calls, tool calls, and
database queries a run touches. Everything else stays in your process, including
a span naming an agent you did not list.

A span with no parent at all is sent only when it names a declared agent, so a
model call started outside every agent run stays in your process.

A span whose parent started in another process has no local parent to check, so
it is sent when it carries any `gen_ai.*` attribute, or when its tracer is named
`convergent.sdk`, `pydantic-ai`, `openinference`, or `litellm`, or is a dotted
child of one of those such as `pydantic-ai.models`. That is how the second half of
a cross-service agent is kept.

`agents=[]` declares no agents, so nothing is sent. The cross-process rule above
applies only to a list with names in it.

An agent whose work crosses two processes needs `agents` in both. Nothing about
the declaration travels between them.

To narrow by request instead of by agent, pass `require_span_attributes={...}` with the
attribute values a span must hold, or `reject_span_attributes={...}` with the values that
keep a span home. Mark each request with `context_attributes=` on the span
that wraps it.

```python
with convergent.span(
    name="support-agent",
    operation="agent_run",
    context_attributes={"customer.id": "acme"},
):
    run_support_request(request)
```

Two span processors do the work. A stamper copies every `context_attributes=`
pair onto each span started inside the block, library spans included, as
`convergent.attributes.<key>`, so the mark collides with no attribute of your
own. The pairs live in the OpenTelemetry context for the block's lifetime.
They stay in your process: nothing writes them to outbound requests. A filter
processor then decides each span when it ends. It reads a key from the stamped
mark first, then from the span's own attributes, then from the resource
attributes. `reject_span_attributes` decides first: one matching key withholds
the span. `require_span_attributes` decides next: the span is forwarded only
when every named key holds an allowed value.
`require_span_attributes={"customer.id": "acme"}` sends a span only when its
customer key is `acme`, so an excluded customer's traffic stays in your
process. `reject_span_attributes={"customer.id": "internal-test"}` withholds
that customer's spans and sends everything else, unmarked spans included. An
unmarked span never passes `require_span_attributes`. Comparison is exact, by
type and case. A list-valued or enum-valued span attribute never matches, so
`reject_span_attributes` cannot exclude it and `require_span_attributes`
withholds it. The filters and `agents` combine: a span is sent only when every
configured filter keeps it.

Each service marks its own requests and sets its own filters. Nothing about
the mark or the filters travels between processes. A service without filters
sends everything it records.

The filters sit in front of every destination the SDK sets up, including a
`convergent.File` or `convergent.Console` you passed in `destinations`.
Processors you added to the provider yourself are the ones that keep receiving
everything they received before, and an exporter you point at Convergent's
ingest endpoint yourself bypasses the filters.

## Pass the provider yourself

To be explicit instead of relying on the global lookup, hand the provider over.

```python
import os

import convergent
from opentelemetry.sdk.trace import TracerProvider

provider = TracerProvider()
convergent.init(release=os.environ["GIT_SHA"], tracer_provider=provider)
```

**Warning:** If you also use auto-instrumentation libraries, pass them the same
provider.

```python
import os

import convergent
from opentelemetry.instrumentation.openai_v2 import OpenAIInstrumentor
from opentelemetry.sdk.trace import TracerProvider

provider = TracerProvider()
convergent.init(release=os.environ["GIT_SHA"], tracer_provider=provider)
OpenAIInstrumentor().instrument(tracer_provider=provider)
```

## No init() call at all

If you build the provider and want to place Convergent in your pipeline by hand,
add the span processor.

```python
import os

from opentelemetry.sdk.trace import TracerProvider

import convergent

provider = TracerProvider()
convergent.otel.install(provider, release=os.environ["GIT_SHA"])
```

Handing the provider to `install()` is what makes `convergent.span()` and
`convergent.observe()` work. A span processor is never told which provider holds
it, so one added with `add_span_processor()` alone can only find a provider you
installed globally.

`install()` takes the provider first, then `api_key`, `endpoint`, `release`,
`agents`, `require_span_attributes`, and `reject_span_attributes`, which mean what they mean on `init()`. It has no
`destinations`, `tracer_provider`, or `debug` argument, because the provider and
everything on it are yours. It needs an API key. Construction makes no network
call, and the first span starts deployment registration on a thread of its own.

## Which attributes we read

Convergent reads the OpenTelemetry GenAI conventions, OpenLLMetry and Traceloop,
OpenInference, and litellm's cost keys. The attributes reference page lists
which spelling of each fact Convergent understands, and it is generated from the
code that does the reading.

Your spans keep their original attributes. A rename adds the standard key next
to the producer's own.

Pass `gen_ai.operation.name` into the call that starts the span, as in
`tracer.start_as_current_span("answer", attributes={"gen_ai.operation.name": "chat"})`.
It is read when the span starts, so a span that gains it on a later line is not
recognized as agent work: it gets none of the attributes below, and `agents=[...]`
drops it.

Three attributes ride along that you do not write. `convergent.semantic.version`
tells ingest how to read the span, `convergent.execution.id` holds the span's own
trace id and is what groups one run's spans together, and the deployment identity
links the span to the release you deployed. The SDK adds all three to every span
carrying a `gen_ai.operation.name`, including one a framework created.

Every span the SDK starts comes from a tracer named `convergent.sdk` whose scope
carries the schema url `https://opentelemetry.io/schemas/1.40.0`, which is the
version of the GenAI conventions these attribute names come from. On
OpenTelemetry below 1.31.0 the url stays in the process: the OTLP encoder of
those releases put the resource's schema url on the scope instead of the scope's
own, so the exported scope reaches the receiver without one.

Convergent builds a structured trace record out of a span when it recognizes the
instrumentation scope and finds the `gen_ai.*` attributes on it. `observe()` and
`span()` set those for you. On a span of your own, or one from another framework,
set them as the GenAI conventions describe, or the raw span is kept and a warning
says the trace may not be fully structured.

## Configure tracing once

A second `init()` with different settings keeps the first configuration and logs
one warning naming what the later call lost. A processor added to a process that
`init()` already configured does the same: it shuts its own exporter down and
sends nothing.

## Traces that cross processes

One trace across two processes needs the trace context to travel with the work.
That context is OpenTelemetry's and moving it is OpenTelemetry's job, so the SDK
ships no `inject` and no `extract` of its own.

Over HTTP an instrumentation package does it, and it has to be installed on both
ends. `opentelemetry-instrumentation-requests` on the caller and
`opentelemetry-instrumentation-fastapi` on the server write and read the
`traceparent` header with no code from you. Either end missing its package breaks
the chain quietly: an uninstrumented caller sends no header, an uninstrumented
server ignores the one it receives, and the second service starts a trace of its
own.

Over a queue message or a job row you carry it yourself. The producer writes the
context into a carrier beside the payload.

```python
from opentelemetry.propagate import inject

carrier: dict[str, str] = {}
inject(carrier)
queue.put({"body": {"invoice_id": "inv_88213"}, "message_attributes": carrier})
```

The consumer extracts it and attaches it around the work, because `observe()` and
`span()` record against whatever context is active and take no parent argument.

```python
from opentelemetry import context as otel_context
from opentelemetry.propagate import extract

token = otel_context.attach(extract(message["message_attributes"]))
try:
    handle(message["body"])
finally:
    otel_context.detach(token)
```

The carrier key is case sensitive, and getting it wrong costs the whole trace.
`inject` writes it as `traceparent` in lower case and OpenTelemetry's default
getter looks it up by exact match, so a carrier holding `Traceparent` reads back
as no context and the consumer starts a trace of its own. When the transport
changes the case of its keys, pass `extract()` a getter of your own that matches
without regard to case.

Call `init()` in each process, and pass `agents`, `require_span_attributes`, and `reject_span_attributes` in
each process when you pass them at all.

## Span links

`span()` and `observe()` take no `links` argument. A span they open is a child of
whatever span is active, and that is the only relationship they express. Parent
and child is the right shape for a run and the calls inside it, and for a worker
running one item on behalf of the run that queued it.

Links are the shape for a fan-in, a batch, and asynchronous messaging, which is
what OpenTelemetry's messaging conventions use them for. A span has exactly one
parent, so a consumer handling ten messages cannot be the child of ten producers,
and a consumer that already has a parent from its own server span would be cut off
from the request it arrived on. For those cases open the span with OpenTelemetry
directly and pass the links in.

```python
from opentelemetry.propagate import extract
from opentelemetry.trace import Link, get_current_span

tracer = convergent.tracer_provider().get_tracer("my-app")
links = [
    Link(get_current_span(extract(m["message_attributes"])).get_span_context()) for m in batch
]
with tracer.start_as_current_span(
    "process invoices",
    attributes={"gen_ai.operation.name": "invoke_workflow"},
    links=links,
):
    ...
```

The span then carries the deployment identity and joins the trace like any other.
