---
title: Instrument
description: Mark the agent run, the tool calls, and the steps in between.
---

Four calls open spans: `agent()` for one agent run, `tool()` for one tool call,
and `span()` and `observe()` for the steps in between. `init()` configures where
those spans go. The SDK ships an agent skill that helps a coding agent do this
instrumentation; see [Instrument with a coding agent](agent-skill.md).

## Mark the agent run

`agent()` records one run of one agent. Put it on the function that handles a
single request.

```python
@convergent.agent(name="convergent-demo")
def answer(question: str) -> str:
    ...
```

The name is the agent's identity in your workspace. Keep it stable, like a class
name. `"convergent-demo"` works; `f"convergent-demo-{user_id}"` gives you one agent
per user and nothing to compare across runs. Put the varying part in
`attributes`.

```python
@convergent.agent(name="convergent-demo", attributes={"tier": "enterprise"})
def answer(question: str) -> str:
    ...
```

`attributes` lands on the run span alone. To put a key on the run and on every
span inside it — which is what the [span filters](reference/api.md#span-filters)
read first — pass `context_attributes=`. When the value varies per request, pass
a callable instead of the mapping. The SDK calls it on every call, with the
decorated function's arguments bound to their parameter names. Name the
parameters you need and absorb the rest with `**_`.

```python
@convergent.agent(
    name="support-agent",
    context_attributes=lambda customer_id, **_: {"customer.id": customer_id},
)
def handle(customer_id: str, ticket: str) -> str:
    ...
```

`span()` takes the mapping form only. Its block runs inside the request, so build the mapping there:

```python
with convergent.span(
    name="support-agent",
    operation="agent_run",
    context_attributes={"customer.id": customer_id},
):
    ...
```

## Mark the tool calls

`tool()` records one tool call. Left without a name it takes the function's own
name, which is already a stable identity.

```python
@convergent.tool()
def lookup_invoice(invoice_id: str) -> dict:
    ...
```

Write it as `@convergent.tool()` with the parentheses. The bare `@convergent.tool`
form is not supported.

## Mark everything else

`span()` wraps a block and hands you a handle. `observe()` is the decorator form
of the same thing, and works on plain functions, coroutines, generators, and
async generators. Both take an `operation`, and `agent_run`, `model_call`, and
`tool_call` are the three the workspace renders as their own kind of step.

```python
with convergent.span(name="convergent-demo", operation="agent_run"):
    with convergent.span(name="gpt-5.5", operation="model_call"):
        reply = respond(question)


@convergent.observe(name="lookup_invoice", operation="tool_call")
def lookup_invoice(invoice_id: str) -> dict:
    ...
```

An `agent_run` is the run itself, and `agent()` writes one. A `model_call` shows
the prompt, the response, the model, and the token counts. A `tool_call` shows
the arguments and the result, and `tool()` writes one. `text_completion` and
`generate_content` render the way a `model_call` does.

Any other operation, `retrieval` or `workflow` or a name of your own such as
`guardrail_check`, is recorded exactly as you wrote it and shows with an
unknown rendering.

One operation name is never read as another. `toolcall` and `tool` stay the words
you wrote and are not filed under `tool_call`.

## Record what went in and out

`set_input()` and `set_output()` record the prompt and the answer. The variable
in `with convergent.span(...) as call` is what you call them on.

```python
with convergent.span(name="gpt-5.5", operation="model_call") as call:
    call.set_input(question)
    reply = respond(question)
    call.set_output(reply)
```

A value that is already a list of message dictionaries goes through untouched.
Anything else becomes the text content of one message.

The handle also carries its own trace id, so a log line can point at the trace it
came from with `logger.info("calling model  trace=%s", call.trace_id)`. Outside a
`with` block, `convergent.current_trace()` gives the same ids for whatever span is
active, or `None` when there is none.

## Get the current span

`current_span()` is how a decorated function reaches its own span to record what
went in and out and to set attributes, because a decorator hands the function no
variable.

```python
@convergent.agent(name="convergent-demo")
def answer(question: str, tier: str) -> str:
    run = convergent.current_span()
    run.set_input(question)
    run.set_attribute("tier", tier)
    reply = respond(question)
    run.set_output(reply)
    return reply
```

The decorator's own `attributes` takes values you know when you write the code.
`set_attribute()` takes the ones you only know at runtime. Both land on that one
span and neither reaches a child span, so the
[span filters](reference/api.md#span-filters) cannot use them to keep or exclude
a whole run. Pass `context_attributes=` for that; see
[Mark the agent run](#mark-the-agent-run).

Call it anywhere without guarding it. Outside a span, or before `init()`, it
hands back an object whose methods do nothing.

## Link the turns of a conversation

A multi-turn agent opens a new run for each turn, and each run is its own trace.
`gen_ai.conversation.id` holds the id that ties those turns together. Set it on
the agent span, with the same value on every turn.

```python
@convergent.agent(name="convergent-demo")
def answer(question: str, conversation_id: str) -> str:
    run = convergent.current_span()
    run.set_attribute("gen_ai.conversation.id", conversation_id)
    ...
```

Use the id your application already has for the thread, such as a chat session
id, a support ticket number, or a Slack thread key. Values look like
`conv_5j66UpCpwteGg4YSxUnt7lPY`.

The industry uses two names for this id. The OpenTelemetry GenAI conventions
call it `gen_ai.conversation.id` and OpenInference calls it `session.id`.
Convergent reads both. Setting either one through the SDK writes
`gen_ai.conversation.id` and Convergent's own `convergent.session.id`, and a
trace that arrives with either industry name gets `convergent.session.id`
added. A framework you hand a conversation id to writes one of the two names
itself, and that is enough.

> **Note:** The traces list reads this attribute. A trace that carries it shows a Conversation
> value, and opening that value filters the list down to the turns that share the id.

## Flush before a short-lived process exits

Export runs on a background thread every five seconds, so a process that ends
right after its last span still has spans in the queue. A normal interpreter
exit is covered: `init()` registers an exit hook (Python's `atexit`) that
drains the queue, so a script that runs to the end, or stops on an exception,
keeps its spans without any extra call.

Some ways of ending a process never run that hook, and the queued spans are
lost: a serverless platform that freezes the process between invocations
(such as a Lambda), `os._exit()`, and `SIGKILL`. In those environments, call
`convergent.flush()` before the exit point.

```python
convergent.flush()
```

A span that has not ended yet is not in the flush, so call it after the traced
function returns, not inside it.

Where the call goes depends on the runtime:

- Lambda: `init()` at module load, once per cold start, and `flush()` per
  invocation.
- Celery and worker pools: `flush()` at the end of each task, and `init()` in the
  worker process rather than the pre-fork parent.
- multiprocessing with fork: children inherit the SDK and drain at exit, and an
  explicit `flush()` is still safer.
- multiprocessing with spawn: children start blank, so set the environment
  variables and call `init()` in the child.
- Async handlers: `init()`, `flush()`, and `check()` are synchronous and can
  block. Close the traced context first, then drain from a worker thread with
  `await asyncio.to_thread(convergent.flush)` in a `finally` block, so a failed
  request is drained too.

Do not call `flush()` per span or in a hot loop.

## Check your instrumentation in a test

To assert that your code records the spans you meant it to, collect them in
memory. OpenTelemetry ships the two pieces this needs, so this SDK has no test
helper of its own.

```python
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

convergent.init(release="9f2c1d4", destinations=[convergent.Console()])
spans = InMemorySpanExporter()
convergent.tracer_provider().add_span_processor(SimpleSpanProcessor(spans))
```

With no git checkout, any fixed string works as the release: a build id, an
image tag, a date.

Then run the traced code and read `spans.get_finished_spans()`. Each one is an
OpenTelemetry `ReadableSpan`, so you assert on `name`, `attributes`, `parent`, and
`status`.

```python
answer("my invoice")

recorded = {span.name: span for span in spans.get_finished_spans()}
run = recorded["invoke_agent convergent-demo"]
assert run.attributes["gen_ai.agent.name"] == "convergent-demo"
assert run.attributes["gen_ai.agent.version"] == "9f2c1d4"
assert recorded["gpt-5.5"].attributes["gen_ai.operation.name"] == "chat"
assert recorded["gpt-5.5"].parent.span_id == run.context.span_id
```

Assert only on what the traced code sets. A run that records the question and the
answer but never sets `gen_ai.request.model` raises `KeyError` on that key, because
naming a span after the model does not set the attribute.

`SimpleSpanProcessor` exports each span as it ends, so nothing waits on a batch and
no `flush()` is needed before reading. The `Console()` destination is there so
`init()` has a destination and tracing turns on. No credentials are involved, so
this runs in CI as it stands. Swap it for `File(tmp_path)` to keep a line of
OTLP/JSON per span off stdout.

`init()` claims the process once. A second call with different values keeps the
first configuration and logs one warning, so call it once for the whole test
session and build a fresh `InMemorySpanExporter` per test. Reading
`spans.get_finished_spans()` gives every span recorded since that exporter was
added.

A child span appears in the list before its parent, because a span is exported
when it ends and the parent ends last. Assert on names or on `parent` rather than
on list order.
