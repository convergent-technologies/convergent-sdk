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
`opentelemetry-instrumentation-fastapi`. Over a queue, use OpenTelemetry's
`inject()` in the producer and `extract()` in the consumer.

```python
from opentelemetry import context as otel_context
from opentelemetry.propagate import extract, inject

# Producer
carrier: dict[str, str] = {}
inject(carrier)
queue.put({"body": payload, "message_attributes": carrier})

# Consumer
token = otel_context.attach(extract(message["message_attributes"]))
try:
    handle(message["body"])
finally:
    otel_context.detach(token)
```

Call `init()` in each process.
