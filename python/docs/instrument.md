---
title: Instrument
description: Mark the agent run, the tool calls, and the steps in between.
---

The snippets on this page assume tracing is already configured.
Use `agent()` for an agent run, `tool()` for a tool call, and `span()` or
`observe()` for other steps. Call `init()` before recording spans. See
[Get started](index.md) for setup and
[Instrument with a coding agent](agent-skill.md) for the coding-agent workflow.

## Mark the agent run

Put `agent()` on the function that handles one request. Give it a stable name.

```python
@convergent.agent(name="support-agent")
def answer(question: str) -> str:
    run = convergent.current_span()
    run.set_input(question)
    reply = "A support specialist will follow up."
    run.set_output(reply)
    return reply
```

Calling `answer("Where is my order?")` returns
`"A support specialist will follow up."` and records a span named
`invoke_agent support-agent` with those input and output messages. The agent name is `support-agent` on every call.
A name such as `f"support-agent-{user_id}"` creates a separate agent for each
user. Put values that change per request in attributes instead.

```python
@convergent.agent(
    name="support-agent",
    attributes={"team": "support"},
    context_attributes=lambda customer_id, **_: {"customer.id": customer_id},
)
def handle(customer_id: str, ticket: str) -> str:
    return "A support specialist will follow up."
```

For `handle("acme", "Order 42 is late")`, the run records custom attribute
`team="support"`. The run and every span started inside it record
`customer.id="acme"`. The callable receives arguments by parameter name; use
`**_` for arguments it does not need. A mapping also works when values are
already known.

`attributes` affects one span. `context_attributes` also reaches child spans
from instrumented libraries using the configured provider. It supplies the
context attributes used by [span filters](reference/api.md#span-filters). Nested contexts
use the inner value when both set a key. These values remain in the process;
the SDK does not send them as OpenTelemetry baggage.

The SDK rejects custom keys starting with `convergent.`. Use ordinary keys
such as `team` or `acme.prompt_revision`. The receiver's custom-key rules are
in [Custom attributes](reference/attributes.md#custom-attributes).

### Describe the agent configuration

Pass `model`, `system_instructions`, and `tools` to describe the configured
agent. These arguments record metadata; they do not configure a model client
or call a tool.

```python
@convergent.agent(
    name="support-agent",
    model="gpt-4o",
    system_instructions="Look up the order before answering.",
    tools=[
        {
            "name": "lookup_order",
            "description": "Fetch one order by ID.",
            "input_schema": {
                "type": "object",
                "properties": {"order_id": {"type": "string"}},
                "required": ["order_id"],
            },
        },
    ],
)
def handle(ticket: str) -> str:
    return "A support specialist will follow up."
```

The run records model `gpt-4o`, the supplied instructions, and one tool
definition. These values belong to the agent run. Convergent does not fill
missing agent configuration from child model calls.

For example, if the run omits `model` and a child reports model `gpt-4o-mini`, the
agent's model remains absent. The child still records its own model.

`span()` and `observe()` accept the same three arguments for
`operation="agent_run"` or `operation="invoke_agent"`. On another operation,
the SDK ignores these arguments and logs the reason. To describe a model
call, use model-call attributes such as `gen_ai.request.model`.

### Agent configuration attributes

Convergent reads the agent run's attributes in the order below. The
[attribute reference](reference/attributes.md#attribute-precedence) describes
how Convergent selects and checks values.
The SDK arguments write `convergent.agent.model`,
`convergent.agent.system_instructions`, and `convergent.agent.tools`.
These take priority on agent runs. An invalid earlier value blocks later
direct aliases.

Read the numbered entries for each field from top to bottom.

| Field | Order | Attribute or event |
| --- | --- | --- |
| Model | 1 | `convergent.agent.model` |
| | 2 | `convergent.request_model` |
| | 3 | `gen_ai.request.model` |
| | 4 | `model_name` from a matching `pydantic-ai` tracer |
| | 5 | `llm.model_name` |
| System instructions | 1 | `convergent.agent.system_instructions` |
| | 2 | `convergent.system_instructions` |
| | 3 | `gen_ai.system_instructions` |
| | 4 | `gen_ai.system.message` event content from a matching `pydantic-ai` tracer |
| Tools | 1 | `convergent.agent.tools` |
| | 2 | `convergent.tool_definitions` |
| | 3 | `gen_ai.tool.definitions` |

A matching tracer is named `pydantic_ai`, `pydantic-ai`, or a dotted child of
either name. The event and `model_name` rules only supply non-null values.
Direct attributes from the table work with any tracer.

For example, an agent run with `convergent.agent.model=" "` and
`gen_ai.request.model="gpt-4o"` has no recorded model. The first value
is present but blank. A valid value `" gpt-4o "` keeps its surrounding spaces.

See [System instructions](reference/attributes.md#system-instructions) for
accepted text, message lists, and invalid input.

## Mark the tool calls

`tool()` records one tool call. Without a name, it uses the function's name.
Record arguments and results explicitly.

```python
@convergent.tool()
def lookup_order(order_id: str) -> dict:
    call = convergent.current_span()
    call.set_input({"order_id": order_id})
    result = {"order_id": order_id, "status": "shipped"}
    call.set_output(result)
    return result
```

`lookup_order("42")` records `execute_tool lookup_order`. Its arguments and
result are JSON strings for the two objects above. It does not create an agent
run. When called inside one, it becomes a child span.

If the model supplied a tool call ID, use `call.set_tool_call_id(call_id)` to
connect the model's request to this call. Write `@convergent.tool()` with
parentheses; bare `@convergent.tool` is not supported.

## Mark everything else

`span()` records a block. `observe()` records each call of a function. The
decorator supports ordinary functions, coroutines, generators, and async
generators.

```python
with convergent.span(
    name="answer",
    operation="model_call",
    attributes={"gen_ai.request.model": "gpt-4o"},
) as call:
    call.set_input("Hello")
    reply = "Hello. How can I help?"
    call.set_output(reply)
```

This records one model-call span named `answer`, requested model `gpt-4o`,
one user message, and one assistant message. It does not call a model.

`agent_run`, `model_call`, and `tool_call` record agent, model, and tool spans.
`text_completion` and `generate_content` also record model spans. Other
operations, such as `retrieval` or `guardrail_check`, record generic steps.
The [operation reference](reference/api.md#operations) lists the exact emitted
values and span names.

## Record what went in and out

Use a span handle's `set_input()` and `set_output()` methods. Except on tool
calls, a nonempty list of dictionaries, each with a `role` key, becomes a
JSON message list. Other values become the content of one user or assistant text message.

| Call | Emitted value |
| --- | --- |
| `call.set_input("Hello")` on a model or agent run | `gen_ai.input.messages`: one user message whose text is `Hello`. |
| `call.set_output("Hi")` on a model or agent run | `gen_ai.output.messages`: one assistant message whose text is `Hi`. |
| `call.set_input({"order_id":"42"})` on a tool call | `gen_ai.tool.call.arguments` contains the string `{"order_id":"42"}`. |
| `call.set_output({"status":"shipped"})` on a tool call | `gen_ai.tool.call.result` contains the string `{"status":"shipped"}`. |

For `call.set_input("Hello")`, the SDK writes this list as compact JSON text
in `gen_ai.input.messages`:

```json
[
  {
    "role": "user",
    "parts": [
      {"type": "text", "content": "Hello"}
    ]
  }
]
```

`set_input([])` records one user message whose text is `[]`; it does not
record an empty message list.

For general operations, these methods still emit message attributes. Whether
they become messages or custom attributes depends on the
[span kind](reference/attributes.md#fields-kept-for-each-span-kind).

## Get the current span

`current_span()` returns a handle for the active span. Use it inside a
decorated function, which receives no handle from the decorator.

```python
@convergent.agent(name="support-agent")
def answer(question: str, tier: str) -> str:
    run = convergent.current_span()
    run.set_input(question)
    run.set_attribute("tier", tier)
    reply = "A support specialist will follow up."
    run.set_output(reply)
    return reply
```

`answer("Order 42 is late", "enterprise")` records the question, the reply,
and custom attribute `tier="enterprise"` on the run. Child spans do not
inherit a value set with `set_attribute()`. Use `context_attributes` or
`set_context_attributes()` when children and span filters need the value.

A handle has `trace_id` and `span_id` for log correlation. `current_trace()`
returns those IDs for the active span, or `None` when none is active.
`current_span()` always returns a handle. Its recording methods do nothing
before tracing is configured or when there is no active span.

## Link the turns of a conversation

Use your application's conversation or thread ID to group runs. A session
does not open a span or force runs into one trace.

```python
with convergent.session("conversation-42"):
    answer("Where is my order?", "enterprise")
    answer("Can I change the address?", "enterprise")
```

With no enclosing active span, these calls create separate traces. Both carry
session ID `conversation-42`. The session also sets the ID on spans from instrumented
libraries using the configured provider.

The `python/examples/sessions` example in the
[public SDK repository](https://github.com/convergent-technologies/convergent-sdk)
records two concurrent conversations with two turns each, SDK tool spans, and
OpenTelemetry child spans. It requires SDK 0.0.9 or newer and no credentials.

`session()` trims surrounding whitespace and accepts 1 to 128 characters.
`session(" conversation-42 ")` therefore records `conversation-42`. An invalid
ID, such as `" "` or `42`, logs once and runs the block without adding a new
session ID. If an outer session exists, it remains active.

Nested sessions use the innermost ID, then restore the outer ID. A span that
already has a string-valued `convergent.session.id` at start keeps that value,
even if the string is invalid for ingestion. A non-string value is replaced
by the active session ID.

For example, inside `session("outer")`, an inner `session("inner")` sets `inner` on new
spans. After the inner block ends, new spans use `outer`. A library
span started with `convergent.session.id="library"` keeps `library`.

### Session attributes from other instrumentation

The receiver reads `convergent.session.id`, then `gen_ai.conversation.id`,
then `session.id`. Direct IDs must be nonblank strings of at most 128
characters. Unlike `session()`, the receiver preserves their whitespace.

The SDK keeps `gen_ai.conversation.id` and `session.id` under the keys you
supply through `attributes=` or `set_attribute()`. It does not rename them
on export. A scoped session adds `convergent.session.id`, which takes priority
when the receiver reads the span.

For example, a span with `session.id="framework-7"` inside
`session("conversation-42")` keeps both attributes. Convergent uses
`conversation-42` for that span. Without the session block, it uses `framework-7`.
A present blank `convergent.session.id` blocks either alias.

`context_attributes` and `set_context_attributes()` reject all three session
keys. They log the rejected key. Use `session()` to propagate an ID and use a
direct attribute only when it should belong to one span. If you used one of
these context keys for filtering, follow the
[0.0.8 upgrade guide](stability.md#upgrading-from-008).

Within a connected run, the session on the shallowest span wins for grouping.
Ties use the earliest start time, then the span ID. Nested agent runs share
that session. If no span supplies an ID, Convergent generates one for the
root run; it cannot connect separate turns without your application's ID.

### Async tasks and worker threads

Async tasks copy the current context when they are created. Await their work
before leaving the session block, or open a session inside each task.

```python
import asyncio

async def record_turn():
    with convergent.span(name="answer", operation="agent_run"):
        await asyncio.sleep(0)

async def run():
    with convergent.session("conversation-42"):
        task = asyncio.create_task(record_turn())
        await task
```

The agent span records session ID `conversation-42`. If `await task` moves
below the session block and no parent span was active when the task was
created, the span has no session ID. A task can still inherit a session ID
from a copied local parent span.

A raw worker thread needs an explicit context copy or its own `session()`
block. Session IDs do not enter outbound requests or OpenTelemetry baggage.
Open the session explicitly in each service that records part of the
conversation.

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
answer("Order 42 is late", "enterprise")

recorded = {span.name: span for span in spans.get_finished_spans()}
run = recorded["invoke_agent support-agent"]
assert run.attributes["gen_ai.agent.name"] == "support-agent"
assert run.attributes["tier"] == "enterprise"
assert run.attributes["gen_ai.agent.version"] == "9f2c1d4"
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
