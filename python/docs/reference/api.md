---
title: API
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

Each returned type is documented as a field table. A dash in the allowed values
column means the type is the only constraint on the value.

## __version__

The installed package's version string, `"0.0.0"` in a source checkout with no
`convergent-sdk` distribution installed.

## init()

```python
convergent.init(*, api_key=None, endpoint=None, release=None, agents=None,
                require_span_attributes=None, reject_span_attributes=None,
                destinations=(), tracer_provider=None,
                debug=False, strict=False) -> Status
```

Configures tracing for the process and returns what was configured. Call it once
at startup.

| Argument | Environment variable | Default |
| --- | --- | --- |
| `api_key` | `CONVERGENT_API_KEY` | none |
| `endpoint` | `CONVERGENT_ENDPOINT` | `https://ingest.convergent.dev` |
| `release` | `CONVERGENT_RELEASE` | none |
| `agents` | | send every span |
| `require_span_attributes` | `CONVERGENT_REQUIRE_SPAN_ATTRIBUTES` | send every span |
| `reject_span_attributes` | `CONVERGENT_REJECT_SPAN_ATTRIBUTES` | send every span |
| `destinations` | `CONVERGENT_SPANS_DIR` | none |
| `tracer_provider` | | the global provider |
| `debug` | `CONVERGENT_DEBUG` | `False` |
| `strict` | `CONVERGENT_STRICT` | `False` |

`api_key` implies the Convergent destination. Without an API key and without a
file destination, the configuration is rejected. A release is required too. Any
string naming the version works: a git sha, a build id, a date.

`agents` is the list of agent names Convergent is allowed to see. Name them and
we get those agents' spans and everything inside their runs. If you set none of
`agents`, `require_span_attributes`, and `reject_span_attributes`, every span
the process records is sent.

`require_span_attributes` maps attribute names to the values a span must hold
to be sent, e.g. `require_span_attributes={"customer.id": ["acme", "globex"]}`.
`reject_span_attributes` maps attribute names to the values that withhold a
span, e.g. `reject_span_attributes={"customer.id": ["internal-test"]}`. Each
maps a key to one value or a list of values. A key's listed values combine with
OR. `require_span_attributes` keys combine with AND: every named key must
match. `reject_span_attributes` keys combine with OR: one matching key
withholds the span. `reject_span_attributes` decides first, so a pair named in
both mappings is withheld. `init()` logs an ERROR for such a pair at startup.
The two environment variables fill the arguments in when they are absent, and
each holds the same mapping as JSON, e.g. `'{"customer.id": ["acme"]}'`.

Three ways hold a key. Pass [`context_attributes=`](#span) on `span()` or a
decorator. The SDK stamps each pair onto that span and every span started
inside it, as `convergent.attributes.<key>`. The pairs stay in the process;
nothing writes them to outbound requests. The span's own attributes satisfy
the key per span, read when the span ends. Resource attributes satisfy it per
process, `OTEL_RESOURCE_ATTRIBUTES` included. The stamped mark answers first,
then the span's own attribute, then the resource. Your own exporters receive
the stamp. Past the attribute limit, OpenTelemetry evicts the span's oldest
attribute. A span that loses a stamped key that way is withheld under
`require_span_attributes`.

Under `require_span_attributes`, a span that holds the key in no source is not
sent. Under `reject_span_attributes` alone, an unmarked span is sent. An
unmarked span never passes `require_span_attributes`. Comparison is exact, by
type and case: `1` matches neither `"1"` nor `True`, and `"Acme"` does not
match `"acme"`. A list-valued or enum-valued span attribute never matches, so
`reject_span_attributes` cannot exclude it and `require_span_attributes`
withholds it. `require_span_attributes={"customer.id": []}` matches nothing and
sends no span at all. `reject_span_attributes={"customer.id": []}` withholds
nothing. The filters and `agents` combine: a span is sent only when every
configured filter keeps it.

The filters cover one process. Set the mark and the filters in each service.
Nothing about them travels between processes. A service with no filters sends
everything it records.

`destinations` adds places on top of Convergent, and every span the filters
keep goes to all of them.

```python
convergent.init(release="1.4.0", destinations=[convergent.File("/data/traces"), convergent.Console()])
```

`tracer_provider` is the provider to attach to instead of looking one up. Its
Resource is left alone and the global provider is not set.

A second `init()` with different settings keeps the first configuration and
returns a `Status` with `reason="already_configured"`.

Any iterable of names works for `agents`, and a repeated name is dropped. A value
that is not names at all is a `TypeError`. An empty name, more than 256 names, and
a name over 512 characters are each a `ValueError`, which are the shapes and caps
registration accepts. [Strict startup](../configuration.md#strict-startup) lists every
condition a configuration can be rejected on.

`init()` blocks the calling thread while it registers the deployment, and that call
retries within a budget of about five seconds, so it does not belong in a request
path. With a file as the only destination it registers nothing and returns right
away.

## check()

```python
convergent.check() -> Report
```

Reads what `init()` configured, then asks the server what it can see for the
same key and release. A network failure, a rejected key, and an unparseable
response all come back as a report saying so. Print it.

`bool(report)` is true when tracing is on, no part of the SDK is known to be
broken, and the server answered for this key. A correct file only setup is
false, because there is no key to answer with. To gate CI on a file only setup,
check `Status.enabled` and then check that the spans file has content.

Do not poll `check()` from a health check on a short interval. Frequent calls can
slow deployment registration.

### Report

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

### Note

| Field | Type | Allowed values | Meaning |
| --- | --- | --- | --- |
| `code` | `str` | any string the server sends, e.g. `"no_spans_yet"` | which problem |
| `message` | `str` | — | the server's own wording for it, ready to print |

`code` is a plain string rather than a fixed set, so a problem named after this
SDK shipped still reaches the reader through `message`. Today the server sends a
note when no deployment is registered for the release, and when a deployment is
registered but no agent has ever been linked to it.

## observe()

```python
convergent.observe(*, name, operation, attributes=None, context_attributes=None) -> Callable
```

Records each call of the decorated function as one span. Works on plain
functions, coroutines, generators, and async generators.

`attributes` land on that one span. `context_attributes` land on that span and
on every span started while the call runs. [span()](#span) states the full
rules for both.

A generator's span covers the whole iteration, and abandoning one early is not
recorded as an error. A traced generator nobody exhausted still has its span open
when the process ends, so it was never queued and the exit flush cannot save it.

For `agent_run`, `name` is the agent's identity. Keep it stable, like a class
name. Put the varying part in `attributes`.

A name outside 1 to 128 characters, or an unrecognized operation, is logged once
and the span is still recorded. Validation happens at ingest.

The decorator yields nothing, so [current_span()](#current_span) is how the
decorated function records its own input and output.

## agent()

```python
convergent.agent(*, name, attributes=None, context_attributes=None) -> Callable
```

`observe(name=name, operation="agent_run")`, spelled for the common case.
Everything `observe()` says holds here.

```python
@convergent.agent(name="support-agent")
def handle(ticket): ...
```

`name` is required and keyword-only on purpose. It is the agent's identity in
your workspace, so deriving it from the function name would let a rename in the
code rename the agent.

## tool()

```python
convergent.tool(*, name=None, attributes=None, context_attributes=None) -> Callable
```

`observe(operation="tool_call")`, spelled for the common case. Leave `name` out
and the decorated function's `__name__` is the tool's name.

```python
@convergent.tool()
def lookup_invoice(invoice_id): ...
```

Write it as `@convergent.tool()` or `@convergent.tool(name="lookup_invoice")`.
The bare `@convergent.tool` form is not supported.

## span()

```python
convergent.span(*, name, operation, attributes=None, context_attributes=None) -> Iterator
```

Records one span for the body of a `with` block and yields a
[SpanHandle](#spanhandle).

```python
with convergent.span(name="answer", operation="model_call") as handle:
    handle.set_input(prompt)
```

`attributes` land on that one span. `context_attributes` land on that span and
on every span started inside the block, library spans included. The pairs live
in the OpenTelemetry context for exactly the block's lifetime. The SDK stamps
each pair onto every span at start as `convergent.attributes.<key>`, so a
stamp overwrites no attribute. Nested blocks merge their pairs, and the inner
value wins for a key both set. When one call names a key in both parameters,
the span carries both: the bare key from `attributes` and the stamped key from
`context_attributes`, and the filter reads the stamped key first. The pairs
stay in the process: nothing writes them to outbound requests. This is how a
request is marked for the
[`require_span_attributes=` and `reject_span_attributes=` filters](#init).

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

### Operations

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

### SpanHandle

| Member | What it does |
| --- | --- |
| `set_input(value)` | writes `gen_ai.input.messages`, or `gen_ai.tool.call.arguments` on a tool call |
| `set_output(value)` | writes `gen_ai.output.messages`, or `gen_ai.tool.call.result` on a tool call |
| `set_attribute(key, value)` | writes one attribute of your own |
| `set_tool_call_id(call_id)` | writes `gen_ai.tool.call.id`, which pairs the model's request for a call with the call itself |
| `trace_id` | the trace the span sits in, as a hex string |
| `span_id` | the span's own id, as a hex string |
| `permalink` | `None` today, because no route displays a single trace |

A value passed to `set_input()` or `set_output()` that is already a list of
message dictionaries goes through untouched. Anything else becomes the text
content of one message.

`set_attribute()` drops any key starting with `convergent.` and these eight, and
logs one line naming the key.

```
gen_ai.operation.name    gen_ai.input.messages
gen_ai.agent.name        gen_ai.output.messages
gen_ai.agent.version     gen_ai.tool.call.arguments
gen_ai.tool.name         gen_ai.tool.call.result
```

A value must be a string, bool, int, float, or a flat sequence of those.

`set_tool_call_id()` takes 1 to 256 characters.

## current_span()

```python
convergent.current_span() -> SpanHandle
```

A handle on the innermost active span, whatever created it. This is how a
function decorated with `observe()`, `agent()`, or `tool()` records content,
since the decorator yields no handle.

```python
@convergent.tool()
def lookup_invoice(invoice_id):
    convergent.current_span().set_input(invoice_id)
```

Never returns `None` and never raises. With no span active, or with tracing
unconfigured, the handle's methods do nothing. The span may be a framework's
rather than one this SDK opened, and in a callback or a spawned task it may not
be the span you expect.

## current_trace()

```python
convergent.current_trace() -> TraceRef | None
```

Where the active span sits, or `None` when there is none.

### TraceRef

| Field | Type | Allowed values | Meaning |
| --- | --- | --- | --- |
| `trace_id` | `str` | — | the trace, as a hex string |
| `span_id` | `str` | — | the active span, as a hex string |
| `permalink` | `str \| None` | — | `None` today, because no route displays a single trace |

## flush()

```python
convergent.flush(timeout_ms=5000) -> FlushResult
```

Drains buffered spans now. Call it in any process that might be killed rather
than exit normally, e.g. a Lambda between invocations.

### FlushResult

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

`timeout_ms` is the deadline `flush()` measures against and passes on to each
destination. A destination sends everything already queued before it returns, so
a long queue or a slow receiver carries the call past that deadline, and
`elapsed_ms` reports how long it really took. Each export request is bounded by
`OTEL_EXPORTER_OTLP_TIMEOUT`, which defaults to ten seconds, so lower that
variable when you need a tighter ceiling. It bounds the network exporter only. A
file or console drain depends on the disk or stream underneath it and has no
deadline the SDK enforces.

## tracer_provider()

```python
convergent.tracer_provider() -> TracerProvider
```

The provider this process is configured with, for using OpenTelemetry directly.
Never returns `None`. With tracing off you get a provider that records nothing,
so your code needs no check around it. Call `init()` first.

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

## Status

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

`agents` reports registration with the server. The filter described in
[Filtering what is sent](../opentelemetry.md#filtering-what-is-sent) enforces
the list this process declared, so a name the server declined still has its
spans sent.

## otel.install()

```python
convergent.otel.install(provider, *, api_key=None, endpoint=None, release=None,
                        agents=None, require_span_attributes=None, reject_span_attributes=None,
                        strict=False) -> ConvergentSpanProcessor
```

Adds Convergent to a provider you built, with no `init()` call. Handing the
provider over is what makes `span()` and `observe()` record through it.

`api_key`, `endpoint`, `release`, `require_span_attributes`,
`reject_span_attributes`, and `strict` fall back to the same environment
variables `init()` reads; `agents` has none.
`destinations` is not accepted, so this route needs an API key.
`CONVERGENT_SPANS_DIR` is read by `init()` only.

## otel.ConvergentSpanProcessor

```python
provider.add_span_processor(
    convergent.otel.ConvergentSpanProcessor(release="a3f21c9", tracer_provider=provider)
)
```

The processor `install()` builds, for placing in your pipeline by hand. It takes
`install()`'s arguments without `provider`, plus `tracer_provider`, which is the
provider `span()` and `observe()` then record through.

Construction makes no network call, and no span ever waits for one. The first
span starts deployment registration on a thread of its own. Every span carries
your release, whether registration has landed yet or not.
