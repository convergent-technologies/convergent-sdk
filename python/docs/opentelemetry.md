---
title: OpenTelemetry
description: Add Convergent to an app that already produces OpenTelemetry spans.
---

If your agent already produces OpenTelemetry spans, call `init()` as usual. It
adds Convergent to the tracer provider you already have.

```python
import os

import convergent

convergent.init(
    release=os.environ["GIT_SHA"],
    agents=["convergent-demo", "billing-agent"],
)
```

Your samplers and exporters do not change. Your existing exporters also receive
the prompts and answers that the SDK records.

Set `gen_ai.operation.name` in the call that starts a span, for example
`tracer.start_as_current_span("answer", attributes={"gen_ai.operation.name": "chat"})`.
A span that sets it later is not recognized as agent work.

## Keys the SDK adds

Each span records which agent run it belongs to when it starts:

- `convergent.invocation.span_id` and
  `convergent.invocation.start_time_unix_micros` name the running agent,
- `convergent.root_invocation.span_id` and
  `convergent.root_invocation.start_time_unix_micros` name the top-level agent,
- `convergent.parent_invocation.span_id` names the agent above a nested one.

When the SDK cannot record a span's run, the span and everything inside it
carry `convergent.invocation.stamp_failed` instead, and still export. Your own
exporters see these keys too, when the span has room for them under its
attribute limit.

Spans sent to Convergent also carry `convergent.sdk.version`
(`python/<version>`). File and console destinations show it too; your own
exporters do not.

## Filtering what is sent

By default, Convergent receives every span the process records.

To send only agent work, pass `agents=[...]` with your agent names. The SDK then
sends each span that names one of those agents, and every span inside it. Web
server and database spans outside an agent run stay in your process.

To filter by request, mark each request with `context_attributes`. Then pass
`require_span_attributes` or `reject_span_attributes`.

```python
convergent.init(
    release=os.environ["GIT_SHA"],
    require_span_attributes={"customer.id": "acme"},
)

with convergent.span(
    name="support-agent",
    operation="agent_run",
    context_attributes={"customer.id": "acme"},
):
    run_support_request(request)
```

This sends only the spans for customer `acme`. Set the filters in each process.
They do not travel between processes. See [Span filters](../../docs/span-filters.md)
for the full rules.

## Pass the provider yourself

If you do not use the global provider, pass your provider to `init()`. Pass the
same provider to each instrumentation package.

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

To add Convergent to a provider you set up yourself, call
`convergent.otel.install()`. Do not use `add_span_processor()`, because then
`convergent.span()` and `convergent.observe()` cannot find the provider.

```python
import os

from opentelemetry.sdk.trace import TracerProvider

import convergent

provider = TracerProvider()
convergent.otel.install(provider, release=os.environ["GIT_SHA"])
```

## Traces that cross processes

To keep one trace across two services, pass the OpenTelemetry trace context
with the work. Over HTTP, install an instrumentation package on both ends, such
as `opentelemetry-instrumentation-requests` and
`opentelemetry-instrumentation-fastapi`. Over a queue, use
`convergent.carrier.inject()` in the producer and
`convergent.carrier.extract()` in the consumer.

```python
from opentelemetry import context as otel_context

import convergent

# Producer
carrier: dict[str, str] = {}
convergent.carrier.inject(carrier, session=True)
queue.put({"body": payload, "message_attributes": carrier})

# Consumer
token = otel_context.attach(convergent.carrier.extract(message["message_attributes"]))
try:
    handle(message["body"])
finally:
    otel_context.detach(token)
```

Call `init()` in each process. Then an agent in the second service is filed
under the first service's agent instead of starting a trajectory of its own.

When a library sends HTTP requests with headers you pass in, inject into a new
`dict` inside the agent run and merge it into those headers:

```python
trace_headers: dict[str, str] = {}
convergent.carrier.inject(trace_headers)
headers |= trace_headers
```

No HTTP span names the host here, so `init(session_hosts=...)` does not apply
unless an HTTP client span is current.
Pass `session=True` as well only when the request stays inside your own
services.

`init()` wraps the global propagator that exists when it runs, so install any
propagator of your own first. If you replace the global propagator after
`init()`, the member stops, and the SDK logs one warning when the next agent
starts. Otherwise every outbound request made inside agent work carries one
`convergent=` member in its `tracestate` header, whatever the host, model
providers included. The member holds:

- a tag naming your Convergent workspace, which is the same for every API key
  in the workspace. `init()` and `otel.install()` fetch it from
  `GET /v1/check` in the background, so neither waits for it. A network error,
  a 429, or a 5xx is retried twice. While the tag is in use, the SDK asks again
  every 10 minutes, so a workspace turned off or back on needs no restart, and
  a 401 stops it asking.
- the span id of the span that sent the request,
- the span id and start time of the running agent, and under a nested agent
  those of the top-level agent,
- from inside a Temporal workflow run with no agent running, a mark in place of
  the agent's ids, so an agent the request reaches starts its own trajectory,
- the session ID, only where you allow it.

It holds nothing else: no agent names, attributes, prompts, or API key. A
request carries no member while the process has no workspace tag, when the SDK
did not stamp the running agent, when no W3C `traceparent` goes out, when the
member would break the W3C `tracestate` limits, or when the request already
holds a `tracestate` header the SDK cannot parse (it removes only a
`convergent=` member from that header). A receiver accepts a member only when its tag matches the
receiver's own, so a call from another workspace leaves the receiver unstamped.
A worker that reads a member while its own tag is still on its way, such as
one that calls `extract()` straight after `init()`, holds the member instead of
refusing it:

- Spans under it that end before the tag arrives wait, and go out stamped once
  the tag matches. If the tag never comes or names another workspace, they go
  out unstamped.
- `flush()` waits for the tag within its timeout, and exit waits up to 5
  seconds for it, within a 10 second limit on the whole exit.
- Until a held member passes, its spans carry neither its stamp nor its
  session while they run.
- A span processor of your own, added beside the SDK's, never sees a held
  member's stamp or session.

A session ID can name a person, so it goes only:

- over HTTP, to the hosts listed in `init(session_hosts=["jobs.example.com"])`.
  No host is listed by default. List only hosts that run the Convergent SDK.
- over a queue or a job row, when you pass `session=True` to
  `convergent.carrier.inject()`.
- Into a Temporal workflow, use
  [`convergent.temporal.TracingInterceptor(send_session=True)`](integrations/index.md#temporal).

The tag is not a secret: anyone who sees one of your requests can copy it, and
a receiver trusts a member with the right tag. If a service accepts requests
from callers you do not trust, remove the `tracestate` header, or just its
`convergent=` member, at that service's public edge.

The SDK sends a session ID of at most 36 characters, whether the sending agent
is top-level or nested, all printable ASCII other than space, comma, and equals
sign. A UUID fits. A session ID of 37 to 128 characters, or one holding
another character, is never cut short: it stays in the sending process, the
member goes without it, and the SDK logs one warning. An ID longer than 128
characters is not a valid session ID at all: `convergent.session()` ignores it
and logs a different warning. Open
a session in the receiving service instead.

Other tools can add members to the `tracestate` header. W3C caps it at 512 characters and 32 members. If the session ID does not fit, the SDK sends the member without it and logs a warning. If the member does not fit, the SDK sends no member and logs a different warning.

The SDK logs each of these warnings once per process. A child process forked
after a warning doesn't log it again.

### Requests from callers you do not trust

A caller who copies your workspace tag can set the span ids your agents are filed under and the session ID on your spans. At the public edge of a service with untrusted callers, remove the `tracestate` header, or its `convergent=` member, before OpenTelemetry reads the request. Also drop `traceparent` there, or start a new trace. Removing only `convergent=` still lets the caller's span become the parent.

The tag is not a secret: it travels in the clear with every request, so anyone
who sees one of your requests can copy it. A receiver trusts a member with the
right tag. Remove the headers in your load balancer or API gateway, or in code
that runs before the OpenTelemetry server instrumentation reads the request.
The receiving SDK then takes no Convergent span ids and no session ID from the
request.

### Propagation without init()

With [`otel.install()`](#no-init-call-at-all) or plain OpenTelemetry, install
the Convergent propagator yourself, once, after any propagator of your own:

```python
from opentelemetry.propagate import set_global_textmap

import convergent

set_global_textmap(convergent.propagator())
```

No host is listed then, so HTTP requests never carry the session ID.

For Temporal, follow these setup rules:

- Pass `convergent.temporal.TracingInterceptor()` to the client.
- The interceptor needs the workspace tag. Only `init()`, `otel.install()` or a `ConvergentSpanProcessor` you build fetch it. Plain OpenTelemetry sends no member.
- Start workflows from inside an agent. A workflow started with no agent above it gets no member and no session.
- The session ID goes only with `send_session=True`. Temporal then stores it in the workflow history.
- Every worker does the same setup: a global provider, `otel.install()`, and the interceptor, on the worker's client. If the caller sets `send_session=True`, the worker's interceptor needs it too.
- When a pydantic-ai agent runs inside a Temporal workflow, a workflow replay can record its agent span twice. See [pydantic-ai's Temporal integration docs](https://ai.pydantic.dev/durable_execution/temporal/) for replay-safe tracing.

See [Temporal](integrations/index.md#temporal) for the full integration setup.
Before starting a workflow from an untrusted request, follow
[Requests from callers you do not trust](#requests-from-callers-you-do-not-trust).
A handler with no local agent can still forward an accepted Convergent member,
including its session ID with `send_session=True`.
A `convergent.session()` the handler opens replaces the received session ID.
Since a session ID can name a person, turn `send_session` on only if the
workflow history may hold it.

```python
import asyncio
import os

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from temporalio.client import Client

import convergent
import convergent.temporal

provider = TracerProvider()
# The interceptor takes its tracer from the global provider.
trace.set_tracer_provider(provider)
convergent.otel.install(provider, release=os.environ["GIT_SHA"])


async def main() -> None:
    client = await Client.connect(
        "localhost:7233",
        interceptors=[convergent.temporal.TracingInterceptor()],
    )
    ...  # start workflows with client


asyncio.run(main())
```

Each worker process also makes its provider global and calls
`convergent.otel.install()` on it, as above. Otherwise the worker ignores the
members it receives: agents in activities start runs of their own instead of
nesting under the workflow's agent, and no session arrives. When the caller's
interceptor has `send_session=True`, the worker's interceptor needs it too, or
the session stops before the workflow.
