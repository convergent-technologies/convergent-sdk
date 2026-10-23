---
title: Span filters
description: Choose which spans Convergent receives, by agent name or by attribute value.
---

By default, Convergent receives every span your process records. Span filters
narrow that down. They apply to every Convergent destination: the network,
`File`, and `Console`. Your own OpenTelemetry exporters still receive every
span, because the filters are not a sampler.

Set the filters once, in `init()`:

```python tab="Python"
convergent.init(
    release="2026.10.06.1",
    agents=["order_support"],
    require_span_attributes={"customer.tier": ["paid", "trial"]},
    reject_span_attributes={"customer.id": "internal-test"},
)
```

```ts tab="TypeScript"
init({
  release: "2026.10.06.1",
  agents: ["order_support"],
  requireSpanAttributes: { "customer.tier": ["paid", "trial"] },
  rejectSpanAttributes: { "customer.id": "internal-test" },
});
```

The options are spelled differently in each SDK:

| Python | TypeScript |
| --- | --- |
| `agents=` | `agents` |
| `require_span_attributes=` | `requireSpanAttributes` |
| `reject_span_attributes=` | `rejectSpanAttributes` |
| `context_attributes=` | `contextAttributes` |
| `set_context_attributes()` | `setContextAttributes()` |

A span is sent only when every rule you set keeps it. Leave all three options
out to send every span.

## Send only your agents

`agents` names the agents Convergent may see. Web server, database, and queue
spans outside an agent run stay in your process.

- A span whose `gen_ai.agent.name` is in the list is sent, with every span
  inside it that names no agent.
- A span naming another agent is withheld, and so are its unnamed children.
- A listed agent nested inside a withheld one is sent, with its own children.
- A top level span with no agent name is withheld.
- A span whose parent came from another process is sent when it looks like
  model work: it has a `gen_ai.*` attribute, or its tracer is
  `convergent.sdk`, `pydantic-ai`, `openinference`, or `litellm`.
- An empty list sends nothing.

The rule is decided when a span starts. Set `gen_ai.agent.name` in the call
that starts the span. A name added later does not count. In TypeScript,
Mastra's bridge adds the agent name only when a span ends, so leave `agents`
unset with that bridge.

An agent that runs across two processes needs `agents` in each one.

## Require or reject by attribute value

`require_span_attributes` / `requireSpanAttributes` sends a span only when it
holds every listed key with one of the listed values.
`reject_span_attributes` / `rejectSpanAttributes` withholds a span when it
holds any listed key with one of the listed values.

- A key takes one value or a list. Any listed value matches.
- Reject runs first. A pair named in both rules is always withheld, and the SDK
  logs it at startup.
- A span missing a required key is withheld. Under a reject rule alone, a span
  missing the key is sent. Use a require rule when a missing value must
  withhold the span.
- A require key with an empty list sends no span at all. A reject key with an
  empty list withholds nothing.
- Values must match exactly, by type and case. `"Acme"` does not match
  `"acme"`, `1` does not match `"1"`, and `true` does not match `1`.
- Python also tells whole numbers from decimals, so `1` does not match `1.0`.
  TypeScript has one number type, so `1` matches `1.0`.
- A span attribute that holds a list never matches.
- Each span is judged on its own. A kept parent does not keep its children. To
  keep or withhold a whole request, mark it with
  [context attributes](#mark-a-request-with-context-attributes). An attribute
  set with `attributes` or `set_attribute()` / `setAttribute()` covers that one
  span only.

## Where each key is read from

The filter reads each key from the first place that holds it:

1. The span's context attributes, described below.
2. The span's own attributes.
3. The resource attributes, including `OTEL_RESOURCE_ATTRIBUTES`.

A later place never replaces an earlier one.

## Mark a request with context attributes

Context attributes mark a span and every span started inside it, including
spans from other libraries. Use them for a value that should decide a whole
request, such as a customer ID. Pass them as `context_attributes=` (Python) /
`contextAttributes` (TypeScript) on `agent()`, `tool()`, `observe()`, or
`span()`.

`agent()`, `tool()`, and `observe()` also take a function that builds the
pairs from the call's arguments. When you learn a value partway through a
request, such as a tier looked up from a token, call
`set_context_attributes()` / `setContextAttributes()` on the current span.

```python tab="Python"
@convergent.agent(
    name="order_support",
    context_attributes=lambda customer_id, **_: {"customer.id": customer_id},
)
def support(customer_id: str, question: str) -> str:
    tier = lookup_tier(customer_id)
    convergent.current_span().set_context_attributes({"customer.tier": tier})
    return answer(customer_id, question)
```

```ts tab="TypeScript"
const support = agent(
  {
    name: "order_support",
    contextAttributes: (customerId: string) => ({ "customer.id": customerId }),
  },
  async (customerId: string, question: string) => {
    currentSpan().setContextAttributes({ "customer.tier": await lookupTier(customerId) });
    return answer(customerId, question);
  },
);
```

The function receives the arguments differently in each SDK. In Python, it
gets them by parameter name, so name the ones you need and absorb the rest with
`**_`. In TypeScript, it gets them by position. `span()` takes only a fixed set
of pairs.

The rules:

- Values are strings, numbers, or booleans.
- SDK keys, keys starting with `convergent.`, and the session keys
  `convergent.session.id`, `gen_ai.conversation.id`, and `session.id` are
  ignored with a warning. Use `session()` for session IDs.
- Nested calls inherit the outer pairs. An inner pair wins for the same key.
- The pairs stay in your process. They never enter OpenTelemetry baggage or
  outbound requests, so a separate process inherits nothing. Set the marks and
  the filters in each service.
- Each pair is sent as `convergent.attributes.<key>`. In Python, it is written
  on the span itself, so your own exporters see it too. In TypeScript, it is
  added only to the copy Convergent receives.
- `set_context_attributes()` / `setContextAttributes()` marks the running span
  and every span started inside it after the call. Spans started before keep
  what they had. It works only on a span the SDK opened, while that span is
  active. In Python, call it on the thread the span runs on. Anywhere else, the
  call is refused and logged, and the span stays unmarked. An unmarked span
  never matches a reject rule.

Some inputs leave a span's marks unresolved: a function that throws, a
function or `set_context_attributes()` / `setContextAttributes()` call that
gives something other than a set of pairs, or pairs that are all ignored. Under
any attribute rule, an unresolved span and every span inside it are withheld.
In TypeScript, an `async` function returns a Promise, so it is unresolved too.
Look the value up inside the wrapped function and call `setContextAttributes()`
instead.

Marks follow your code across threads and async work only when the context
does:

- In Python, the pairs follow an `asyncio` task. A raw thread starts with an
  empty context. Pass `contextvars.copy_context()` to the thread, open a
  `span()` with `context_attributes=` there, or use OpenTelemetry's
  `ThreadingInstrumentor`.
- In TypeScript, async work keeps the marks through the context manager that
  `init()` registers. With your own provider, your context manager must carry
  context across `await`.

## Set the rules from the environment

`CONVERGENT_REQUIRE_SPAN_ATTRIBUTES` and `CONVERGENT_REJECT_SPAN_ATTRIBUTES`
hold the same rules as JSON. Each is used only when the matching option is not
passed.

```sh
export CONVERGENT_REQUIRE_SPAN_ATTRIBUTES='{"customer.tier": ["paid", "trial"]}'
export CONVERGENT_REJECT_SPAN_ATTRIBUTES='{"customer.id": "internal-test"}'
```

A variable sets a rule but marks no span. A span still needs the key from a
context attribute, its own attributes, or a resource attribute. `agents` has no
environment variable.

## What a malformed rule does

A malformed rule turns off sending to Convergent. Nothing is sent, so a bad
rule never leaks unfiltered spans. `init()` logs the problem and returns
`enabled` false with reason `invalid_config`. With strict mode on, `init()`
raises at startup instead.

A rule is malformed when:

- `agents` is not a list of names, holds an empty name, holds more than 256
  names, or holds a name over 512 characters.
- An attribute rule is empty, such as `{}`.
- A key is empty or padded with spaces.
- A value is `None` (Python) / `null` (TypeScript). Pass an empty list to match
  nothing.
- A value is not a string, number, or boolean, such as a nested list or an
  object.
- An environment variable holds text that is not JSON.

Strict startup lists the error each case raises, for
[Python](../python/docs/configuration.md#strict-startup) and
[TypeScript](../typescript/docs/configuration.md#strict-startup).

## See the running rules

`init()` returns a status with the rules it is running: `agents`, plus
`require_span_attributes` and `reject_span_attributes` (Python) /
`requireSpanAttributes` and `rejectSpanAttributes` (TypeScript). Each
attribute rule shows every value as a list, and is `None` (Python) / `null`
(TypeScript) when you did not set it.

Print the `check()` report to see the attribute rules on one `filters` line,
reject first.

```python tab="Python"
print(convergent.check())
```

```ts tab="TypeScript"
console.log(String(await check()));
```

## Limits to know

- In TypeScript, spans that pass the `agents` rule enter the export queue
  before the attribute rules run, so rejected spans still take queue slots. The
  queue holds 2,048 spans by default. When it is full, new spans are dropped,
  including ones that would pass. Raise `OTEL_BSP_MAX_QUEUE_SIZE` for larger
  bursts.
- In Python, OpenTelemetry drops a span's oldest attribute once the span passes
  its attribute limit, 128 by default. A span that loses a context attribute
  this way is withheld under a require rule.
