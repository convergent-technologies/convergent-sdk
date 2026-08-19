---
title: API reference
description: Every call in the public surface.
---

Everything here is reachable as `convergent.<name>` after `import convergent`.
`init()`, `otel.install()`, and `ConvergentSpanProcessor` reject a setting that
cannot work: logged at ERROR and disabled by default, raised at startup when
`strict=True` or `CONVERGENT_STRICT=1` is set. `File` and `Console` check their
own literal arguments and raise at construction. After setup, nothing raises.

There are no Convergent exception types. A value of the wrong type raises
`TypeError`, a value of the right type that is not allowed raises `ValueError`, and
a spans file that cannot be opened raises `OSError`.

Each callable is documented in one order: the signature, the return value,
then the arguments with the type and whether the argument is required. Each
returned type is documented as a field table. A dash in the allowed values
column means the type is the only constraint on the value.

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

- **`api_key`** (`str`, optional): implies the Convergent destination. Without
  an API key and without a file destination, the configuration is rejected.
- **`endpoint`** (`str`, optional; default: `https://ingest.convergent.dev`):
  the receiver to send spans to, such as a customer-hosted data plane or a
  collector running beside your process.
- **`release`** (`str`, optional): a working configuration requires one, from
  the argument or its variable. Any string naming the version works: a git
  sha, a build id, a date.
- **`agents`** (`list[str]`, optional; default: send every span): the list of
  agent names Convergent is allowed to see. Name them and we get those agents'
  spans and everything inside their runs. Any iterable of names works, and a
  repeated name is dropped.
- **`require_span_attributes`** (`Mapping[str, object]`, optional; default:
  send every span): sends a span only when it matches every key/value in the
  mapping, an AND across all of them.
  `require_span_attributes={"customer.id": "acme"}` sends only spans marked
  for that customer. A key also takes a list of values, and any one of them
  matches. [Span filters](#span-filters) states the rules.
- **`reject_span_attributes`** (`Mapping[str, object]`, optional; default:
  send every span): withholds a span when it matches any key/value in the
  mapping, an OR across all of them.
  `reject_span_attributes={"customer.id": "internal-test"}` withholds the
  spans marked that way and sends the rest. A key also takes a list of values.
  [Span filters](#span-filters) states the rules.
- **`destinations`** (`Sequence[Destination]`, optional): extra
  [destinations](#destinations) spans go to, in addition to the Convergent
  ingestion endpoint. Every span the filters keep goes to all of them.
- **`tracer_provider`** (`TracerProvider`, optional; default: the global
  provider): the provider to attach to instead of looking one up. Its Resource
  is left alone and the global provider is not set.
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

For `agents`, a value that is not names at all is a `TypeError`. An empty name,
more than 256 names, and a name over 512 characters are each a `ValueError`,
which are the shapes and caps registration accepts.
[Strict startup](../configuration.md#strict-startup) lists every condition a
configuration can be rejected on.

`init()` blocks the calling thread while it registers the deployment, and that call
retries within a budget of about five seconds, so it does not belong in a request
path. With a file as the only destination it registers nothing and returns right
away.

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

Three ways hold a key:

1. Pass [`context_attributes=`](#span) on `span()` or a decorator. The SDK
   stamps each pair onto that span and every span started inside it, as
   `convergent.attributes.<key>`. The pairs stay in the process; nothing
   writes them to outbound requests.
2. The span's own attributes, read when the span ends.
3. Resource attributes, `OTEL_RESOURCE_ATTRIBUTES` included, once per process.

The stamped mark answers first, then the span's own attribute, then the
resource. Your own exporters receive the stamp and every span: the filters
govern only the destinations the SDK set up.

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
- The filter decides each span alone; a kept parent does not keep its
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
| `agents` | `list[str]` | — | the names the server confirmed it linked |
| `destinations` | `list[str]` | — | e.g. `["convergent", "file:/data/traces/spans.jsonl"]` |
| `mode` | `str` | `"owned"`, `"attached"` | whether `init()` created the tracer provider or attached to yours |
| `app_url` | `str \| None` | — | where this deployment is in the workspace. `None` today, because nothing fills it in yet |
| `reason` | `str \| None` | `None`, `"missing_config"`, `"invalid_config"`, `"setup_failed"`, `"no_provider"`, `"already_configured"` | `None`, or why part of the setup is not working |
| `require_span_attributes` | `Mapping \| None` | — | the running require filter, as attribute name to value list. `None` when not configured |
| `reject_span_attributes` | `Mapping \| None` | — | the running reject filter, same shape. `None` when not configured |

The two filter fields echo what validation kept after a keyword argument beat
its environment variable, so they state what this process filters on. The
printed `check()` report shows them as one `filters` row, reject first.

`agents` reports registration with the server. The filter described in
[Filtering what is sent](../opentelemetry.md#filtering-what-is-sent) enforces
the list this process declared, so a name the server declined still has its
spans sent.

### check()

```python
convergent.check() -> Report
```

Reads what `init()` configured, then asks the server what it can see for the
same key and release. A network failure, a rejected key, and an unparseable
response all come back as a report saying so. Print it.

**Returns:** a [Report](#report). Takes no arguments.

`bool(report)` is true when tracing is on, no part of the SDK is known to be
broken, and the server answered for this key. A correct file only setup is
false, because there is no key to answer with. To gate CI on a file only setup,
check `Status.enabled` and then check that the spans file has content.

Do not poll `check()` from a health check on a short interval. Frequent calls can
slow deployment registration.

The server links an agent to a release when its first spans finish ingesting,
so a check immediately after a flush can list no agent. Wait and check again
before you read that as a failure.

#### Report

| Field | Type | Allowed values | Meaning |
| --- | --- | --- | --- |
| `status` | `Status` | — | the `Status` that `init()` returned |
| `round_trip` | `str` | `"ok"`, `"no_credentials"`, `"not_a_check_response"`, or a transport reason such as `"http_401"` or `"TimeoutError"` | `"ok"` when the check endpoint answered, otherwise why it did not |
| `round_trip_ms` | `int \| None` | — | how long the round trip took. Set only when the server answered |
| `endpoint` | `str \| None` | — | where the round trip went. Never carries the key |
| `organization_id` | `str \| None` | — | the workspace the key belongs to |
| `agents` | `list[str]` | — | agents the server has linked to this release |
| `agents_truncated` | `bool` | `True`, `False` | `True` when the server had more than it would list |
| `notes` | `list[Note]` | — | a `Note` for each problem the server can see |

#### Note

| Field | Type | Allowed values | Meaning |
| --- | --- | --- | --- |
| `code` | `str` | any string the server sends, e.g. `"no_spans_yet"` | which problem |
| `message` | `str` | — | the server's own wording for it, ready to print |

`code` is a plain string rather than a fixed set, so a problem named after this
SDK shipped still reaches the reader through `message`. Today the server sends a
note when no deployment is registered for the release, and when a deployment is
registered but no agent has ever been linked to it.

## Tracing

### observe()

```python
convergent.observe(*, name, operation, attributes=None, context_attributes=None) -> Callable
```

Records each call of the decorated function as one span. Works on plain
functions, coroutines, generators, and async generators.

**Returns:** the decorator to apply. The decorator yields nothing, so
[current_span()](#current_span) is how the decorated function records its own
input and output.

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
  land on that span and on every span started during the call. Pass the
  `Mapping` directly, or pass a callable that returns one. The SDK calls it
  once per call, with the decorated function's own arguments, so the pairs can
  come from the call itself:

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
  one `kwargs` mapping under that name. A callable that
  raises, or that returns something that is not a `Mapping`, is logged once.
  The span is still recorded for your own destinations, and it is withheld
  when `require_span_attributes=` or `reject_span_attributes=` is configured,
  because an untagged span must not slip past a filter.
  [span()](#span) states the full rules for both parameters.

A generator's span covers the whole iteration, and abandoning one early is not
recorded as an error. A traced generator nobody exhausted still has its span open
when the process ends, so it was never queued and the exit flush cannot save it.

A name outside 1 to 128 characters, or an unrecognized operation, is logged once
and the span is still recorded. Validation happens at ingest.

### agent()

```python
convergent.agent(*, name, attributes=None, context_attributes=None) -> Callable
```

`observe(name=name, operation="agent_run")`, spelled for the common case.
Everything `observe()` says holds here.

```python
@convergent.agent(name="support-agent")
def handle(ticket): ...
```

**Returns:** the decorator to apply.

**Arguments:** every argument is keyword-only.

- **`name`** (`str`, required): the agent's identity in your workspace.
  Required on purpose: deriving it from the function name would let a rename
  in the code rename the agent.
- **`attributes`** (`Mapping[str, str | bool | int | float]`, optional) and
  **`context_attributes`** ([`ContextAttributes`](#contextattributes),
  optional): as on [observe()](#observe).

### tool()

```python
convergent.tool(*, name=None, attributes=None, context_attributes=None) -> Callable
```

`observe(operation="tool_call")`, spelled for the common case.

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
convergent.span(*, name, operation, attributes=None, context_attributes=None) -> Iterator
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
  They land on that span and on every span started inside the block, library
  spans included. The Mapping form only: a `with` block has no call arguments
  to resolve the callable form the decorators take.

The `context_attributes` pairs live in the OpenTelemetry context for exactly
the block's lifetime. The SDK stamps
each pair onto every span at start as `convergent.attributes.<key>`, so a
stamp overwrites no attribute. Nested blocks merge their pairs, and the inner
value wins for a key both set. When one call names a key in both parameters,
the span carries both: the bare key from `attributes` and the stamped key from
`context_attributes`, and the filter reads the stamped key first. The pairs
stay in the process: nothing writes them to outbound requests. This is how a
request is marked for the
[`require_span_attributes=` and `reject_span_attributes=` filters](#span-filters).

The pairs follow an `asyncio` task. A raw thread starts with an empty context,
so pass `contextvars.copy_context()` to reach a worker thread.

Both parameters take plain `str`, `bool`, `int`, or `float` values. A key the
SDK owns, or a `context_attributes` value of any other type, is dropped and
logged once.

An exception leaving the block sets the span status to error and re-raises.
`GeneratorExit` and `asyncio.CancelledError` pass through untouched.

What reaches the span is the exception's class name and nothing else. `raise
ValueError("account 4Q7W2X is closed")` records the description `ValueError`, no
exception event is added, and neither the message nor the traceback leaves your
process. When you want the message in the trace, put it on a key of your own with
`set_attribute()` once you have decided it is safe to send.

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

The workspace renders `agent_run`, `model_call`, and `tool_call` as their own
kind of step, and renders `text_completion` and `generate_content` the way it
renders `model_call`.

Any other string is recorded exactly as you wrote it, so a custom operation such
as a guardrail check or an approval step is supported. It joins its run the same
way, and the workspace shows most such steps generically, carrying the span name,
the duration, and the attributes; a step may instead be filed under a broader
label the workspace infers from the words in its name. `retrieval`,
`embeddings`, `workflow`, and `agent_create` are recorded and shown the same
way. One operation name is never read as another, so `tool` and `toolcall` stay
the words you wrote and are not filed under `tool_call`.

An `agent_run` also writes `gen_ai.agent.name`, and `gen_ai.agent.version` when
the process reported a release. A `tool_call` writes `gen_ai.tool.name`, and
`gen_ai.tool.type` as `function` unless you pass your own.

Those two are also the only span names the SDK rewrites: an `agent_run` span is
named `invoke_agent <name>` and a `tool_call` span `execute_tool <name>`. Every
other operation keeps the name you gave it, so
`span(name="gpt-5.5", operation="model_call")` appears as `gpt-5.5`.

The conventions name three kinds of tool. `function` is a tool your own code runs,
`extension` is one the agent side runs against an external API, and `datastore` is
one that reads or queries external data. The SDK writes `function`, because a
callable in your process is all it can see, so pass your own value in `attributes`
for the other two.

#### SpanHandle

| Member | What it does |
| --- | --- |
| `set_input(value)` | writes `gen_ai.input.messages`, or `gen_ai.tool.call.arguments` on a tool call |
| `set_output(value)` | writes `gen_ai.output.messages`, or `gen_ai.tool.call.result` on a tool call |
| `set_attribute(key, value)` | writes one attribute of your own |
| `set_context_attributes(pairs)` | marks this span and every span started after the call inside it, for the [span filters](#span-filters) |
| `set_tool_call_id(call_id)` | writes `gen_ai.tool.call.id`, which pairs the model's request for a call with the call itself |
| `trace_id` | the trace the span sits in, as a hex string |
| `span_id` | the span's own id, as a hex string |
| `permalink` | `None` today, because no route displays a single trace |

A value passed to `set_input()` or `set_output()` that is already a list of
message dictionaries goes through untouched. Anything else becomes the text
content of one message.

`set_attribute()` drops any key starting with `convergent.` and these eight, and
logs one line naming the key.

`set_context_attributes()` takes the pairs you only know mid-request, such as a
customer id looked up from a token. It stamps the running span at once, and
every span started after the call inherits the pairs until the span ends. Spans
started before the call keep what they had. Call it inside the span it marks,
on the thread the span runs on; anywhere else it is refused and logged. Each
pair passes the same guards as `context_attributes=`. When pairs were passed
and none is usable, the span is withheld under any span filter, the same rule
the callable form follows. The handle must be on a span the SDK opened. On a
span another library opened, the call is refused and logged, because nothing
would release the pairs when that span ends. A refused call leaves the span
untagged, and an untagged span does not match a `reject_span_attributes=`
rule.

```
gen_ai.operation.name    gen_ai.input.messages
gen_ai.agent.name        gen_ai.output.messages
gen_ai.agent.version     gen_ai.tool.call.arguments
gen_ai.tool.name         gen_ai.tool.call.result
```

A value must be a string, bool, int, float, or a flat sequence of those.

`set_tool_call_id()` takes 1 to 256 characters.

### current_span()

```python
convergent.current_span() -> SpanHandle
```

**Returns:** a [SpanHandle](#spanhandle) on the innermost active span,
whatever created it. Takes no arguments. This is how a function decorated with
`observe()`, `agent()`, or `tool()` records content, since the decorator
yields no handle.

```python
@convergent.tool()
def lookup_invoice(invoice_id):
    convergent.current_span().set_input(invoice_id)
```

Never returns `None` and never raises. With no span active, or with tracing
unconfigured, the handle's methods do nothing. The span may be a framework's
rather than one this SDK opened, and in a callback or a spawned task it may not
be the span you expect.

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
| `permalink` | `str \| None` | — | `None` today, because no route displays a single trace |

### flush()

```python
convergent.flush(timeout_ms=5000) -> FlushResult
```

Drains buffered spans now. Call it in any process that might be killed rather
than exit normally, e.g. a Lambda between invocations.

**Returns:** a [FlushResult](#flushresult).

**Arguments:**

- **`timeout_ms`** (`int`, optional; default: `5000`): the deadline `flush()`
  measures against and passes on to each destination.

#### FlushResult

| Field | Type | Allowed values | Meaning |
| --- | --- | --- | --- |
| `ok` | `bool` | `True`, `False` | every flush call succeeded and delivered the spans it drained |
| `pending` | `int` | — | spans still queued when the budget ran out |
| `dropped` | `int` | — | spans thrown away since the last call, by a full queue or by an export that failed or was refused |
| `elapsed_ms` | `int` | — | how long it took |

`bool(result)` is `ok`.

`ok` does not require an empty queue. In a live process spans are produced while
the flush runs, so `pending` above zero after a successful flush is normal. Read
`pending` before a short lived process exits.

An export the receiver refused turns `ok` off and adds its spans to `dropped`,
and so does a destination that cannot write. The count is taken and reset on each
call, so when two threads flush at the same moment one of them sees a given loss
and the other does not.

A destination sends everything already queued before it returns, so
a long queue or a slow receiver carries the call past the `timeout_ms`
deadline, and `elapsed_ms` reports how long it really took. Each export request is bounded by
`OTEL_EXPORTER_OTLP_TIMEOUT`, which defaults to ten seconds, so lower that
variable when you need a tighter ceiling. It bounds the network exporter only. A
file or console drain depends on the disk or stream underneath it and has no
deadline the SDK enforces.

### ContextAttributes

```python
def marked(context_attributes: convergent.ContextAttributes) -> None: ...
```

What the decorators accept for `context_attributes=`: the
`Mapping[str, str | bool | int | float]` itself, or a callable returning one,
which the SDK resolves from the decorated function's own arguments on every
call. Exported for annotating your own; see [observe()](#observe).

## Destinations

Places spans go besides Convergent, passed to `init(destinations=...)`. These are
descriptions rather than exporters, so constructing one opens nothing. Each checks
its own arguments and raises where you wrote it, whatever `strict` is set to.

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

A `File` on its own is a complete configuration: no credentials, and nothing sent
over the network at all. Use it where the process cannot reach the receiver, for
example a locked-down container or a CI job.

The file holds whatever the spans carry, which for an auto-instrumented agent is
every prompt and completion, so widen `mode` only when you know who else can read
the directory. An existing file is tightened to `mode` as well, so a run that
started before the file was owner-only does not keep appending to a world-readable
one. `filename` has to be a bare name: `File(filename="../spans.jsonl")` raises
`ValueError` where you construct it, because a path there would write spans outside
the directory you named.

Two `File` values naming one file collapse into one destination. Two different
files do not, and one warning says every span is being written twice.

No deployment is registered on this route, so the release each span carries is the
file's only version evidence. Pass `release` and Convergent links those traces to
it when the file is ingested.

Keep the directory on a local disk you control. Line integrity across several
writers relies on append behavior a network filesystem does not guarantee.

The spans file is opened when `init()` runs, so a directory it cannot create or a
file it cannot write costs that destination a warning, or raises `OSError` under
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

There are two uses for it. While developing, it shows exactly what is being sent
without standing up a collector. In Lambda, Cloud Run, or Modal it is a
transport, because those platforms collect stdout off the container, so it works
where the process cannot open a socket and nobody will fetch a file. Those
platform logs will then hold your prompts, and moving them from there to
Convergent is a log forwarder you run and own.

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
OpenTelemetry directly. Never returns `None`. Takes no arguments. With tracing
off you get a provider that records nothing, so your code needs no check
around it. Call `init()` first.

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
  to. Handing the provider over is what makes `span()` and `observe()` record
  through it.
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

Construction makes no network call, and no span ever waits for one. The first
span starts deployment registration on a thread of its own. Every span carries
your release, whether registration has landed yet or not.

## Environment variables

Each variable fills its argument in when the argument is not passed, and the
argument wins. `CONVERGENT_DEBUG` is the exception: either it or `debug=True`
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
