---
title: API Reference
description: Every call in the public surface.
---

Everything here is reachable as `convergent.<name>` after `import convergent`.

`init()`, `otel.install()`, and `ConvergentSpanProcessor` reject a setting that
cannot work. By default they log it at ERROR and disable tracing. With
`strict=True` or `CONVERGENT_STRICT=1`, they raise at startup. `File` and
`Console` check their own literal arguments and raise at construction. After
setup, nothing raises.

The SDK has no exception types of its own. Unless a callable below says it
ignores an invalid value, errors map as follows:

- A value of the wrong type raises `TypeError`.
- A value of the right type that is not allowed raises `ValueError`.
- A spans file that cannot be opened raises `OSError`.

Each callable lists its signature, its return value, and then its arguments
with their types and whether each is required. Each returned type has a field
table. A dash in the allowed values column means the type is the only
constraint.

## Setup

### __version__

The installed package's version string, `"0.0.0"` in a source checkout with no
`convergent-sdk` distribution installed.

### init()

```python
convergent.init(*, api_key=None, endpoint=None, release=None, agents=None,
                require_span_attributes=None, reject_span_attributes=None,
                destinations=(), tracer_provider=None,
                debug=False, strict=False) -> Status
```

Configures tracing for the process. Call it once at startup.

**Returns:** a [Status](#status) stating what was configured.

**Arguments:** every argument is keyword-only. Most fall back to an
[environment variable](#environment-variables) when they are not passed.

- **`api_key`** (`str`, optional): implies the Convergent destination. With
  no API key and no file destination, `init()` rejects the configuration.
- **`endpoint`** (`str`, optional; default: `https://ingest.convergent.dev`):
  the receiver to send spans to, such as a customer-hosted data plane or a
  collector running beside your process.
- **`release`** (`str`, optional): required for a working configuration,
  from the argument or its variable. Any string that names the version works,
  such as a git sha, a build id, or a date.
- **`agents`** (`list[str]`, optional; default: send every span): the agent
  names Convergent may see. The SDK sends those agents' spans and everything
  inside their runs. Any iterable of names works. A repeated name is dropped.
- **`require_span_attributes`** (`Mapping[str, object]`, optional; default:
  send every span): sends a span only when it matches every key/value in the
  mapping (AND).
  `require_span_attributes={"customer.id": "acme"}` sends only spans marked
  for that customer. A key also takes a list of values, and any one of them
  matches. [Span filters](#span-filters) states the rules.
- **`reject_span_attributes`** (`Mapping[str, object]`, optional; default:
  send every span): withholds a span when it matches any key/value in the
  mapping (OR).
  `reject_span_attributes={"customer.id": "internal-test"}` withholds the
  spans marked that way and sends the rest. A key also takes a list of values.
  [Span filters](#span-filters) states the rules.
- **`destinations`** (`Sequence[Destination]`, optional): extra
  [destinations](#destinations) spans go to, in addition to the Convergent
  ingestion endpoint. Every span the filters keep goes to all of them.
- **`tracer_provider`** (`TracerProvider`, optional; default: the global
  provider): the provider to attach to instead of looking one up. The SDK
  leaves its Resource unchanged and does not set the global provider.
- **`debug`** (`bool`, optional; default: `False`): gives the
  `convergent.sdk` logger a level of its own.
- **`strict`** (`bool`, optional; default: `False`): makes a configuration
  that cannot work raise at startup, instead of the default log-and-disable.

If you set none of `agents`, `require_span_attributes`, and
`reject_span_attributes`, every span the process records is sent.

```python
convergent.init(release="1.4.0", destinations=[convergent.File("/data/traces"), convergent.Console()])
```

A second `init()` with different settings keeps the first configuration and
returns a `Status` with `reason="already_configured"`.

For `agents`, a value that is not a collection of names raises `TypeError`.
An empty name, more than 256 names, or a name over 512 characters raises
`ValueError`. These limits match what registration accepts.
[Strict startup](../configuration.md#strict-startup) lists every condition a
configuration can be rejected on.

Do not call `init()` in a request path. It blocks the calling thread while it
registers the deployment, and registration retries for up to about five
seconds. With a file as the only destination, it registers nothing and returns
right away.

#### Span filters

`require_span_attributes` and `reject_span_attributes` filter by attribute
value:

- A key takes one value or a list. Listed values combine with OR.
- `require_span_attributes` keys combine with AND.
  `reject_span_attributes` keys combine with OR.
- Reject decides first. A pair named in both mappings is withheld, and
  `init()` logs an ERROR for it at startup.
- The [environment variables](#environment-variables) hold the same mappings
  as JSON, e.g. `'{"customer.id": "acme"}'`, and fill the arguments in when
  they are absent.

A key can come from three places, read in this order:

1. [`context_attributes=`](#span) on `span()` or a decorator. The SDK
   stamps each pair onto that span and every span started inside it, as
   `convergent.attributes.<key>`. The pairs stay in the process. Nothing
   writes them to outbound requests.
2. The span's own attributes, read when the span ends.
3. Resource attributes, including `OTEL_RESOURCE_ATTRIBUTES`, read once per
   process.

The filters apply only to the destinations the SDK set up. Your own exporters
receive every span, including the stamp.

Context attributes across threads and processes:

- Set the filters once, in `init()`. They judge every span in the process.
- Every span inherits its parent span's stamped context attributes, whatever
  thread or context it starts on. A library that parents its spans to your
  run inherits the run's attributes this way.
- A span's own `context_attributes=` adds pairs and wins for a key both hold.
  Its descendants follow the override.
- A span with no in-process parent inherits nothing. When your own code
  starts rootless spans on another thread, open a `span()` with
  `context_attributes=` there, or use a threading instrumentor.
  OpenTelemetry's `ThreadingInstrumentor` is one.
- A separate process inherits nothing. Configure the SDK and the filters in
  each process.

The matching rules:

- Comparison is exact, by type and case. `1` matches neither `"1"` nor
  `True`. `"Acme"` does not match `"acme"`.
- The filter decides each span alone. A kept parent does not keep its
  children. To keep or exclude a whole run, hold the key with
  `context_attributes=`. A bare attribute on the run span covers that one
  span only.
- An unmarked span never passes `require_span_attributes`. Under
  `reject_span_attributes` alone, an unmarked span is sent.
- A list-valued or enum-valued span attribute never matches.
  `reject_span_attributes` cannot exclude it. `require_span_attributes`
  withholds it.
- `require_span_attributes={"customer.id": []}` sends no span at all.
  `reject_span_attributes={"customer.id": []}` withholds nothing.
- Past the attribute limit, OpenTelemetry evicts the span's oldest attribute.
  A span that loses a stamped key that way is withheld under
  `require_span_attributes`.
- The filters and `agents` combine. A span is sent only when every configured
  filter keeps it.
- The filters cover one process. Set the mark and the filters in each
  service. Nothing about them travels between processes. A service with no
  filters sends everything it records.

### Status

Returned by `init()`.

| Field | Type | Allowed values | Meaning |
| --- | --- | --- | --- |
| `enabled` | `bool` | `True`, `False` | tracing is on |
| `deployment` | `str \| None` | — | the registered deployment id |
| `release` | `str \| None` | — | the release this process reported |
| `agents` | `list[str]` | — | the names `init()` declared, as the server echoed them back |
| `destinations` | `list[str]` | — | e.g. `["convergent", "file:/data/traces/spans.jsonl"]` |
| `mode` | `str` | `"owned"`, `"attached"` | whether `init()` created the tracer provider or attached to yours |
| `app_url` | `str \| None` | — | reserved; always `None` |
| `reason` | `str \| None` | `None`, `"missing_config"`, `"invalid_config"`, `"setup_failed"`, `"no_provider"`, `"already_configured"` | `None`, or why part of the setup is not working |
| `require_span_attributes` | `Mapping \| None` | — | the running require filter, as attribute name to value list. `None` when not configured |
| `reject_span_attributes` | `Mapping \| None` | — | the running reject filter, same shape. `None` when not configured |

The two filter fields show what this process filters on: what validation
kept, after any keyword argument overrode its environment variable. The
printed `check()` report shows them as one `filters` row, reject first.

`agents` is the list this process declared. The filter in
[Filtering what is sent](../opentelemetry.md#filtering-what-is-sent) enforces
it locally. The server records an agent when its first spans arrive.

### check()

```python
convergent.check() -> Report
```

Reads what `init()` configured, then asks the server what it can see for the
same key. Print the result. A network failure, a rejected key, or an
unparseable response comes back as a report that says so.

**Returns:** a [Report](#report). Takes no arguments.

`bool(report)` is true when tracing is on, no part of the SDK is known to be
broken, and the server answered for this key. A correct file-only setup is
false, because it has no key. To gate CI on a file-only setup, check
`Status.enabled` and then check that the spans file has content.

Do not poll `check()` from a health check on a short interval.

The server records an agent when its first spans finish ingesting. A check
right after a flush can list no agents. Wait and check again before you treat
that as a failure.

#### Report

| Field | Type | Allowed values | Meaning |
| --- | --- | --- | --- |
| `status` | `Status` | — | the `Status` that `init()` returned |
| `round_trip` | `str` | `"ok"`, `"no_credentials"`, `"not_a_check_response"`, or a transport reason such as `"http_401"` or `"TimeoutError"` | `"ok"` when the check endpoint answered, otherwise why it did not |
| `round_trip_ms` | `int \| None` | — | how long the round trip took. Set only when the server answered |
| `endpoint` | `str \| None` | — | where the round trip went. Never carries the key |
| `organization_id` | `str \| None` | — | the workspace the key belongs to |
| `agents` | `list[str]` | — | agents the server has recorded for the key's workspace |
| `agents_truncated` | `bool` | `True`, `False` | `True` when the server had more than it would list |
| `notes` | `list[Note]` | — | a `Note` for each problem the server can see |

#### Note

| Field | Type | Allowed values | Meaning |
| --- | --- | --- | --- |
| `code` | `str` | any string the server sends, e.g. `"no_spans_yet"` | which problem |
| `message` | `str` | — | the server's own wording for it, ready to print |

`code` is a plain string, not a fixed set, so a problem added after this SDK
shipped still reaches you through `message`. Today the server sends no notes.

## Tracing

### session()

```python
convergent.session(session_id) -> Iterator[None]
```

Sets `convergent.session.id` on every span started inside the block through
the configured provider, including spans from instrumented libraries. It
opens no span and does not join separate traces into one trace.

```python
with convergent.session("conversation-42"):
    answer(question)
```

**Returns:** a context manager. It yields no value.

**Arguments:**

- **`session_id`** (`str`, required): your application's conversation or
  thread ID. The SDK removes surrounding whitespace, then accepts 1 to 128
  characters. For example, `" conversation-42 "` becomes `"conversation-42"`.

A span that starts with a string-valued `convergent.session.id` keeps it,
even when the string is blank. The active session ID replaces an existing
non-string value. Nested sessions use the inner ID and restore the outer ID
when they end. An invalid argument logs once and adds no new ID. An outer
session stays active.

Await async work before leaving the block, or open a session inside the
task.
A raw thread needs a context copy or its own session block. The ID does not
enter OpenTelemetry baggage or outbound requests, so open a session in each
service.

`attributes=` and `set_attribute()` keep the direct keys
`gen_ai.conversation.id` and `session.id`. `session()` adds its own key,
which takes priority at ingestion. `context_attributes` and
`set_context_attributes()` reject all three session keys. Use `session()`
to propagate the ID.

### observe()

```python
convergent.observe(*, name, operation, attributes=None, context_attributes=None, model=None, system_instructions=None, tools=None) -> Callable
```

Records each call of the decorated function as one span. Works on plain
functions, coroutines, generators, and async generators.

**Returns:** the decorator to apply. The decorator yields no handle. To record
input and output inside the decorated function, use
[current_span()](#current_span).

**Arguments:** every argument is keyword-only.

- **`name`** (`str`, required): the span's name, 1 to 128 characters. For
  `agent_run`, it is the agent's identity. Keep it stable, like a class name,
  and put the varying part in `attributes`.
- **`operation`** (`str`, required): what the span records.
  [Operations](#operations) lists the recognized values, and any other string
  is recorded exactly as you wrote it.
- **`attributes`** (`Mapping[str, str | bool | int | float]`, optional):
  attributes for that one span.
- **`context_attributes`** ([`ContextAttributes`](#contextattributes),
  optional): pairs set in the OpenTelemetry context while the call runs. They
  land on that span and on every span started during the call. Pass a
  `Mapping`, or a callable that returns one. The SDK calls the callable once
  per call with the decorated function's arguments, so the pairs can come from
  the call itself:

  ```python
  @convergent.agent(
      name="support-agent",
      context_attributes=lambda customer_id, **_: {"customer.id": customer_id},
  )
  def handle(customer_id: str, ticket: str) -> str: ...
  ```

  The callable receives the arguments bound to their parameter names. Name
  the parameters you need and absorb the rest with `**_`. On a method the
  names include `self`. A function taking only `**kwargs` hands the callable
  one `kwargs` mapping under that name. If the callable raises or returns
  something other than a `Mapping`, the SDK logs it once. The span is still
  recorded for your own destinations. If `require_span_attributes=` or
  `reject_span_attributes=` is configured, the span is withheld, so an
  untagged span cannot get past a filter.
  [span()](#span) states the full rules for both parameters.
- **`model`** (`str`, optional), **`system_instructions`** (`str`, optional),
  and **`tools`** (`Sequence[Mapping[str, Any]]`, optional): how the agent is
  configured. `model` is the model it calls, `system_instructions` is the
  prompt it runs with, and `tools` is the tool definitions it offers. They
  apply when the operation resolves to `invoke_agent`, as both `agent_run` and
  `operation="invoke_agent"` do. They land on that one span, with `tools` as
  one JSON string. On other operations the SDK ignores them and logs the
  reason. They only describe the agent. They do not configure its model client
  or run its tools. `tools` accepts a sequence of JSON-safe mappings, including
  OpenAI-style `{"type": "function", "function": {...}}` definitions and
  Anthropic-style `{"name": ..., "input_schema": ...}` definitions.
  See [agent configuration attributes](attributes.md#operation-and-identity)
  for the recorded keys and their precedence.

A generator's span covers the whole iteration. Abandoning a generator early is
not recorded as an error. If no one exhausts a traced generator, its span is
still open when the process ends. The span was never queued, so the exit flush
cannot save it.

A name outside 1 to 128 characters, or an unrecognized operation, is logged once
and the span is still recorded. Validation happens at ingest.

### agent()

```python
convergent.agent(*, name, attributes=None, context_attributes=None, model=None, system_instructions=None, tools=None) -> Callable
```

Shorthand for `observe(name=name, operation="agent_run")`. Everything on
`observe()` applies here.

```python
@convergent.agent(name="support-agent")
def handle(ticket): ...
```

**Returns:** the decorator to apply.

**Arguments:** every argument is keyword-only.

- **`name`** (`str`, required): the agent's identity in your workspace. It is
  required so that renaming the function in code does not rename the agent.
- **`attributes`** (`Mapping[str, str | bool | int | float]`, optional) and
  **`context_attributes`** ([`ContextAttributes`](#contextattributes),
  optional): as on [observe()](#observe). A custom key becomes an attribute
  on the run. The SDK rejects keys prefixed `convergent.`. There is
  no separate `custom=` keyword.
- **`model`**, **`system_instructions`**, and **`tools`**: as on
  [observe()](#observe).

  ```python
  @convergent.agent(
      name="support-agent",
      model="gpt-4o",
      system_instructions="Look up the order before answering.",
      tools=[{"name": "lookup_order", "description": "Fetch one order by ID."}],
      attributes={"acme.prompt_revision": "r2"},
  )
  def handle(ticket: str) -> str:
      ...
  ```

  This records the configuration on the run span. A child model call does not
  fill in missing agent configuration.

### tool()

```python
convergent.tool(*, name=None, attributes=None, context_attributes=None) -> Callable
```

Shorthand for `observe(operation="tool_call")`.

```python
@convergent.tool()
def lookup_invoice(invoice_id): ...
```

**Returns:** the decorator to apply.

**Arguments:** every argument is keyword-only.

- **`name`** (`str`, optional; default: the decorated function's `__name__`):
  the tool's name.
- **`attributes`** (`Mapping[str, str | bool | int | float]`, optional) and
  **`context_attributes`** ([`ContextAttributes`](#contextattributes),
  optional): as on [observe()](#observe).

Write it as `@convergent.tool()` or `@convergent.tool(name="lookup_invoice")`.
The bare `@convergent.tool` form is not supported.

### span()

```python
convergent.span(*, name, operation, attributes=None, context_attributes=None, model=None, system_instructions=None, tools=None) -> Iterator
```

Records one span for the body of a `with` block.

```python
with convergent.span(name="answer", operation="model_call") as handle:
    handle.set_input(prompt)
```

**Returns:** a context manager that yields a [SpanHandle](#spanhandle).

**Arguments:** every argument is keyword-only.

- **`name`** (`str`, required): the span's name, 1 to 128 characters.
- **`operation`** (`str`, required): what the span records.
  [Operations](#operations) lists the recognized values, and any other string
  is recorded exactly as you wrote it.
- **`attributes`** (`Mapping[str, str | bool | int | float]`, optional):
  attributes for that one span.
- **`context_attributes`** (`Mapping[str, str | bool | int | float]`,
  optional): pairs set in the OpenTelemetry context for the block's lifetime.
  They land on that span and on every span started inside the block,
  including library spans. Only the `Mapping` form works here. A `with` block
  has no call arguments, so the decorators' callable form does not apply.
- **`model`**, **`system_instructions`**, and **`tools`**: as on
  [observe()](#observe).

Use `context_attributes` to mark a request for the
[`require_span_attributes=` and `reject_span_attributes=` filters](#span-filters).
The pairs live in the OpenTelemetry context for exactly the block's lifetime.
The SDK stamps each pair onto every span at start as
`convergent.attributes.<key>`, so a stamp never overwrites an attribute. Nested
blocks merge their pairs, and the inner value wins for a key both set. If one
call names a key in both parameters, the span carries the bare key from
`attributes` and the stamped key from `context_attributes`. The filter reads
the stamped key first. The pairs stay in the process. Nothing writes them to
outbound requests.

The pairs follow an `asyncio` task. A raw thread starts with an empty context,
so pass `contextvars.copy_context()` to reach a worker thread.

Both parameters take plain `str`, `bool`, `int`, or `float` values. A key the
SDK owns, or a `context_attributes` value of any other type, is dropped and
logged once. The session keys `convergent.session.id`,
`gen_ai.conversation.id`, and `session.id` are also rejected in
`context_attributes`. Use [session()](#session) for those IDs.

An exception leaving the block sets the span status to error and re-raises.
`GeneratorExit` and `asyncio.CancelledError` pass through untouched.

The span records only the exception's class name. `raise
ValueError("account 4Q7W2X is closed")` records the description `ValueError`.
The SDK adds no exception event, and neither the message nor the traceback
leaves your process. To put the message in the trace, write it to a key of your
own with `set_attribute()` once you know it is safe to send.

#### Operations

| What you write | What the span says |
| --- | --- |
| `agent_run` | `invoke_agent` |
| `model_call` | `chat` |
| `tool_call` | `execute_tool` |
| `retrieval` | `retrieval` |
| `embeddings` | `embeddings` |
| `workflow` | `invoke_workflow` |
| `agent_create` | `create_agent` |
| `text_completion` | `text_completion` |
| `generate_content` | `generate_content` |

The emitted operations `invoke_agent`, `chat`, and `execute_tool` identify
agent, model, and tool spans. `text_completion` and `generate_content` also
identify model spans. Other operations keep their text and record generic
spans. For example, `operation="toolcall"` stays `toolcall` and does not
become a tool call. See [span kinds](attributes.md#fields-kept-for-each-span-kind)
for the fields each kind keeps.

An `agent_run` also writes `gen_ai.agent.name`, and `gen_ai.agent.version` when
the process reported a release. When passed, it writes `convergent.agent.model`,
`convergent.agent.system_instructions`, and `convergent.agent.tools`, the last
as one JSON string. A `tool_call` writes `gen_ai.tool.name`, and
`gen_ai.tool.type` as `function` unless you pass your own.

These two are also the only operations whose span names the SDK rewrites. An
`agent_run` span is named `invoke_agent <name>`, and a `tool_call` span is
named `execute_tool <name>`. Every other operation keeps the name you gave it,
so `span(name="gpt-5.5", operation="model_call")` appears as `gpt-5.5`.

The OpenTelemetry conventions name three tool types:

- `function` is a tool your own code runs.
- `extension` is a tool the agent side runs against an external API.
- `datastore` is a tool that reads or queries external data.

The SDK writes `function`, because it sees only a callable in your process. For
the other two, pass your own value in `attributes`.

#### SpanHandle

| Member | What it does |
| --- | --- |
| `set_input(value)` | writes `gen_ai.input.messages`, or `gen_ai.tool.call.arguments` on a tool call |
| `set_output(value)` | writes `gen_ai.output.messages`, or `gen_ai.tool.call.result` on a tool call |
| `set_attribute(key, value)` | writes one attribute of your own |
| `set_context_attributes(pairs)` | marks this span and every span started after the call inside it, for the [span filters](#span-filters) |
| `set_tool_call_id(call_id)` | writes `gen_ai.tool.call.id`, which pairs the model's request for a call with the call itself |
| `input_artifact(source, *, filename=None, media_type=None, origin=None)` | records a file the span read and uploads a copy, listed in `convergent.artifacts.input` |
| `output_artifact(source, *, filename=None, media_type=None, origin=None)` | records a file the span produced and uploads a copy, listed in `convergent.artifacts.output` |
| `trace_id` | the trace the span sits in, as a hex string |
| `span_id` | the span's own id, as a hex string |
| `permalink` | reserved; always `None` |

On tool calls, `set_input()` and `set_output()` record a string unchanged
and serialize other values as JSON. On other operations, a nonempty list of
mappings with a `role` key in every item becomes a JSON message list. Other
values become the text of one message, with role `user` for input and
`assistant` for output. For example, `set_input([])` records one user message
whose text is `[]`. These methods do not read function arguments or return
values automatically.

`set_attribute()` drops keys starting with `convergent.` and the eight keys
listed below. It logs the rejected key.

Use `set_context_attributes()` for pairs you learn mid-request, e.g. a
customer id looked up from a token. It stamps the running span at once. Every
span started after the call inherits the pairs until the span ends. Spans
started before the call keep what they had.

- Call it inside the span it marks, on the thread the span runs on. Anywhere
  else, the SDK refuses and logs the call.
- Each pair passes the same checks as `context_attributes=`. If you pass pairs
  and none is usable, any span filter withholds the span. The callable form
  follows the same rule.
- The handle must be on a span the SDK opened. On a span another library
  opened, the SDK refuses and logs the call, because nothing would release the
  pairs when that span ends.

A refused call leaves the span untagged. An untagged span does not match a
`reject_span_attributes=` rule.

```
gen_ai.operation.name    gen_ai.input.messages
gen_ai.agent.name        gen_ai.output.messages
gen_ai.agent.version     gen_ai.tool.call.arguments
gen_ai.tool.name         gen_ai.tool.call.result
```

A value must be a string, bool, int, float, or a flat sequence of those.

`set_tool_call_id()` takes 1 to 256 characters.

`input_artifact()` and `output_artifact()` take a local path or the bytes
themselves, on any operation. Use `input_artifact()` for a document given to
the agent, content handed to a model, or a file a tool loaded. Use
`output_artifact()` for the agent's deliverable or a file a tool wrote.
`filename` defaults to the path's name. Any file is accepted. `media_type`
defaults to `application/octet-stream`. The upload carries the filename so the
server can identify the file's type. Call `flush(uploads=True)` before a
short-lived process exits.

The call copies the file and hashes it with SHA-256 on your thread before it
returns, so later changes to the file do not reach Convergent. For a 16 MiB
file this takes tens of milliseconds. The span lists each file as one JSON list
per role. Each entry has `digest` (`sha256:` plus the hash in hex),
`size_bytes`, `media_type`, and, when known, `filename` and `origin`.

`origin` says who supplied the file. Leave it out when you do not know. A value
outside the table is logged once and left off the listing; the file is still
recorded.

| Value | Who supplied the file |
| --- | --- |
| `user` | an end user handed it in |
| `agent` | the agent's own code wrote it |
| `model` | a model generated it |
| `tool` | a tool produced or fetched it |
| `external` | any other system, such as storage, the web, or another service |

Only the upload runs in the background. After the span ends and passes the
[span filters](#span-filters), a background thread uploads the copy:

- It first asks the server whether your workspace already holds the file. If
  so, it sends no bytes, so a fresh process does not resend files an earlier
  run uploaded.
- A file already stored, or one the server refused (409, 413, or 422), is not
  sent again.
- A file whose upload ran out of retries or hit an error is sent again when a
  later span lists it.
- A span the filters withhold uploads nothing.

Limits:

- A span lists at most 100 files.
- Spans that have not ended hold at most 128 MiB of copies across the process.
- The SDK logs a file and counts it in `convergent.artifacts.dropped` when the
  file is past either limit, larger than 16 MiB, unreadable, or has a
  `media_type` that is not printable ASCII of at most 255 characters.
- A filename longer than 1,024 characters is logged once and left off the
  listing; the file is still recorded.
- Files are recorded only on spans the SDK opened, and only when `api_key` is
  set.
- If the upload route answers 404, file uploads are off for your
  organization. The process then stops uploading and listing files.
- Pre-signed URLs are not supported yet.

### current_span()

```python
convergent.current_span() -> SpanHandle
```

**Returns:** a [SpanHandle](#spanhandle) on the innermost active span,
whatever created it. Takes no arguments. The decorators yield no handle, so a
function decorated with `observe()`, `agent()`, or `tool()` uses this to record
content.

```python
@convergent.tool()
def lookup_invoice(invoice_id):
    convergent.current_span().set_input(invoice_id)
```

Never returns `None` and never raises. With no span active, or with tracing
unconfigured, the handle's methods do nothing. The span may belong to a
framework rather than this SDK. In a callback or a spawned task, it may not be
the span you expect.

### current_trace()

```python
convergent.current_trace() -> TraceRef | None
```

**Returns:** a [TraceRef](#traceref) stating where the active span sits, or
`None` when there is none. Takes no arguments.

#### TraceRef

| Field | Type | Allowed values | Meaning |
| --- | --- | --- | --- |
| `trace_id` | `str` | — | the trace, as a hex string |
| `span_id` | `str` | — | the active span, as a hex string |
| `permalink` | `str \| None` | — | reserved; always `None` |

### flush()

```python
convergent.flush(timeout_ms=5000, *, uploads=False, upload_timeout_ms=60000) -> FlushResult
```

Drains buffered spans now. Call it in any process that might be killed
instead of exiting normally, e.g. a Lambda between invocations. Call it after
the traced function returns, because a span that has not ended is not flushed.

| Runtime | Where to call `init()` and `flush()` |
| --- | --- |
| Lambda | `init()` at module load. `flush()` per invocation. |
| Celery and worker pools | `init()` in the worker process, not the pre-fork parent. `flush()` at the end of each task. |
| Async handlers | `await asyncio.to_thread(convergent.flush)` in a `finally` block. |

**Returns:** a [FlushResult](#flushresult).

**Arguments:**

- **`timeout_ms`** (`int`, optional; default: `5000`): the deadline `flush()`
  measures against and passes on to each destination.
- **`uploads`** (`bool`, optional; default: `False`): also wait for the files
  from `input_artifact()` and `output_artifact()` to upload. `ok` then also
  requires that none is left pending.
- **`upload_timeout_ms`** (`int`, optional; default: `60000`): how long to wait
  for uploads after the span drain, when `uploads` is true. It is separate
  from the span budget because a large file takes longer than a span batch.

#### FlushResult

| Field | Type | Allowed values | Meaning |
| --- | --- | --- | --- |
| `ok` | `bool` | `True`, `False` | every flush call succeeded and delivered the spans it drained |
| `pending` | `int` | — | spans still queued when the budget ran out |
| `dropped` | `int` | — | spans thrown away since the last call, by a full queue or by an export that failed or was refused |
| `elapsed_ms` | `int` | — | how long it took |
| `pending_uploads` | `int` | — | files not uploaded yet |
| `dropped_uploads` | `int` | — | files not uploaded since the last call: over the queue bound, refused by the upload route, or out of retries |

`bool(result)` is `ok`.

`ok` does not require an empty queue. A live process produces spans while the
flush runs, so `pending` above zero after a successful flush is normal. Read
`pending` before a short-lived process exits.

An export the receiver refused, or a destination that cannot write, turns `ok`
off and adds its spans to `dropped`. Each call reads and resets the count. If
two threads flush at the same moment, only one of them sees a given loss.

A destination sends everything already queued before it returns. A long queue
or a slow receiver can carry the call past the `timeout_ms` deadline.
`elapsed_ms` reports the actual time. `OTEL_EXPORTER_OTLP_TIMEOUT` bounds each
export request and defaults to ten seconds. Lower it for a tighter ceiling. It
bounds the network exporter only. A file or console drain depends on the disk
or stream underneath and has no deadline the SDK enforces.

### ContextAttributes

```python
def marked(context_attributes: convergent.ContextAttributes) -> None: ...
```

The type the decorators accept for `context_attributes=`. It is either a
`Mapping[str, str | bool | int | float]` or a callable that returns one. The
SDK calls the callable with the decorated function's arguments on every call.
Exported so you can annotate your own code. See [observe()](#observe).

### ArtifactOrigin

```python
def attach(span: convergent.SpanHandle, origin: convergent.ArtifactOrigin) -> None: ...
```

What `origin=` accepts on `input_artifact()` and `output_artifact()`:
`Literal["user", "agent", "model", "tool", "external"]`. Exported for annotating
your own; see [SpanHandle](#spanhandle).

## Destinations

Places to send spans besides Convergent, passed to `init(destinations=...)`.
A destination is a description, not an exporter, so constructing one opens
nothing. Each one checks its own arguments and raises where you wrote it,
whatever `strict` is set to.

### File

```python
convergent.init(release="1.4.0", destinations=[convergent.File("/data/traces", mode=0o640)])
```

Writes every span the filters keep to `<path>/<filename>` as OTLP/JSON, one
span per line.

| Field | Type | Allowed values | Meaning |
| --- | --- | --- | --- |
| `path` | `str \| os.PathLike` | — | the directory to write in |
| `filename` | `str` | a bare file name | the file's name, `spans.jsonl` by default, so several processes can share one directory |
| `mode` | `int` | `0` to `0o777` | the file's permission bits, `0o600` by default |

A `File` alone is a complete configuration. It needs no credentials and sends
nothing over the network. Use it where the process cannot reach the receiver,
e.g. a locked-down container or a CI job.

The file holds whatever the spans carry. For an auto-instrumented agent, that
is every prompt and completion. Widen `mode` only when you know who else can
read the directory. The SDK also tightens an existing file to `mode`. A run
that started before the file was owner-only does not keep appending to a
world-readable file. `filename` must be a bare name.
`File(filename="../spans.jsonl")` raises `ValueError` at construction, because
a path there would write spans outside the directory you named.

Two `File` values naming the same file collapse into one destination. Two
different files do not, and the SDK logs one warning that every span is being
written twice.

This route registers no deployment, so the release on each span is the file's
only version evidence. Pass `release`, and Convergent links those traces to it
when the file is ingested.

Keep the directory on a local disk you control. Several writers rely on append
behavior to keep lines intact, and a network filesystem does not guarantee it.

`init()` opens the spans file. If the SDK cannot create the directory or write
the file, that destination logs a warning, or `init()` raises `OSError` under
strict mode.

### Console

```python
convergent.init(release="1.4.0", destinations=[convergent.Console(pretty=True)])
```

Writes every span the filters keep to stdout or stderr as OTLP/JSON.

| Field | Type | Allowed values | Meaning |
| --- | --- | --- | --- |
| `stream` | `str` | `"stdout"`, `"stderr"` | where the spans go, `"stdout"` by default |
| `pretty` | `bool` | `True`, `False` | indent for reading. Off by default, which is the same shape a spans file has |

Use it in two cases:

- While developing, to see exactly what is sent without running a collector.
- In Lambda, Cloud Run, or Modal, as a transport. Those platforms collect
  stdout from the container, so it works where the process cannot open a
  socket and no one will fetch a file. The platform logs then hold your
  prompts. Moving them to Convergent takes a log forwarder that you run and
  own.

### Destination

```python
def configure(destinations: list[convergent.Destination]) -> None: ...
```

`File | Console`, for annotating a list of your own.

## OpenTelemetry

### tracer_provider()

```python
convergent.tracer_provider() -> TracerProvider
```

**Returns:** the `TracerProvider` this process is configured with, for using
OpenTelemetry directly. Takes no arguments. Call `init()` first. Never returns
`None`. With tracing off, it returns a provider that records nothing, so your
code needs no check around it.

### otel.install()

```python
convergent.otel.install(provider, *, api_key=None, endpoint=None, release=None,
                        agents=None, require_span_attributes=None, reject_span_attributes=None,
                        strict=False) -> ConvergentSpanProcessor
```

Adds Convergent to a provider you built, with no `init()` call.

**Returns:** the [ConvergentSpanProcessor](#otelconvergentspanprocessor) it
added to the provider.

**Arguments:** every argument after `provider` is keyword-only and means what
it means on [init()](#init), with the same
[environment variable](#environment-variables) fallbacks.
`destinations` is not accepted, so this route needs an API key.
`CONVERGENT_SPANS_DIR` is read by `init()` only.

- **`provider`** (`TracerProvider`, required): the provider to add Convergent
  to. `span()` and `observe()` then record through it.
- **`api_key`** (`str`, optional)
- **`endpoint`** (`str`, optional; default: `https://ingest.convergent.dev`)
- **`release`** (`str`, optional)
- **`agents`** (`list[str]`, optional; default: send every span): no
  environment variable fills it in.
- **`require_span_attributes`** (`Mapping[str, object]`, optional; default:
  send every span): [Span filters](#span-filters) states the rules.
- **`reject_span_attributes`** (`Mapping[str, object]`, optional; default:
  send every span): [Span filters](#span-filters) states the rules.
- **`strict`** (`bool`, optional; default: `False`)

### otel.ConvergentSpanProcessor

```python
provider.add_span_processor(
    convergent.otel.ConvergentSpanProcessor(release="a3f21c9", tracer_provider=provider)
)
```

The processor `install()` builds, for placing in your pipeline by hand.

**Arguments:** [otel.install()](#otelinstall)'s arguments without `provider`,
every one keyword-only, plus:

- **`tracer_provider`** (`TracerProvider`, optional; default: the global
  provider): the provider `span()` and `observe()` then record through.

Construction makes no network call, and no span waits for one. The first span
starts deployment registration on its own thread. Every span carries your
release, whether or not registration has finished.

## Environment variables

Each variable fills in its argument when the argument is not passed. A passed
argument wins. The exception is `CONVERGENT_DEBUG`. Either it or `debug=True`
turns debug logging on. [Configuration](../configuration.md) states the full
rules and the values each variable accepts.

- **`CONVERGENT_API_KEY`**: the ingestion key. Implies the Convergent
  destination.
- **`CONVERGENT_ENDPOINT`**: the receiver address. Defaults to
  `https://ingest.convergent.dev`.
- **`CONVERGENT_RELEASE`**: the release, when `init(release=...)` is not
  passed. One of the two is required.
- **`CONVERGENT_REQUIRE_SPAN_ATTRIBUTES`**: the `require_span_attributes`
  filter, as JSON, e.g. `'{"customer.id": "acme"}'`.
- **`CONVERGENT_REJECT_SPAN_ATTRIBUTES`**: the `reject_span_attributes`
  filter, as JSON.
- **`CONVERGENT_SPANS_DIR`**: write the spans the filters keep to
  `spans.jsonl` in this directory. Read by `init()` only.
- **`CONVERGENT_TRACES_EXPORTER`**: `console` adds a console destination to
  the others.
- **`CONVERGENT_DEBUG`**: `1` gives the `convergent.sdk` logger a level of its
  own.
- **`CONVERGENT_STRICT`**: `1` makes a configuration that cannot work raise
  and stop the process at startup, instead of the default log-and-disable.
