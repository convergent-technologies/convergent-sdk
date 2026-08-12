# Parallel queue workers

One dispatcher puts six invoices on a queue. Three worker processes pull from it at
the same time. Each invoice is one unit of work, and the whole batch is one trace.

This is the shape a container running a queue consumer has, so it covers the two
things that shape gets wrong most often: where `flush()` goes, and how the trace
context reaches the worker that picked up the message.

The example runs with no API key, no network, and no dependencies beyond the SDK.
A scripted stand-in for a chat completions endpoint answers the model calls on
loopback.

## Run it

```bash
uv run python/examples/parallel-workers/run_local.py
uv run python/examples/parallel-workers/verify.py
```

`run_local.py` starts the stub model, spawns three workers, runs the dispatcher,
waits for the queue to drain, then sends each worker `SIGTERM` and waits for it to
exit. Spans land in `spans/` next to the example, one file per process.

`verify.py` reads those files and asserts the trace below. It exits non-zero when
anything is off, so it is what a CI step runs.

## The trace it produces

Nineteen spans in one trace: one workflow, and six invoices each recording an agent
run, a model call, and a tool call.

```
dispatch-invoices
|-- invoke_agent invoice-worker    name=invoice-worker  version=df1aa812a  24ms
|   |-- gpt-4o-mini    model=gpt-4o-mini  input_tokens=180  output_tokens=24  24ms
|   `-- execute_tool lookup_invoice    name=lookup_invoice
|-- invoke_agent invoice-worker    name=invoice-worker  version=df1aa812a  23ms
|   |-- gpt-4o-mini    model=gpt-4o-mini  input_tokens=192  output_tokens=31  22ms
|   `-- execute_tool lookup_invoice    name=lookup_invoice
...
```

Six agent runs sit under one workflow span even though they ran in three different
processes. The workflow span belongs to the dispatcher, and it ends as soon as the
last message is queued, while the workers are still working. A queue producer does
not wait for its consumers, and OpenTelemetry allows a child span to start and end
after its parent has finished.

## Where flush() goes, and why

The worker flushes once per invoice, in the loop, after the traced function has
returned:

```python
try:
    run_item(message)
finally:
    queue.task_done()
convergent.flush()
```

A `flush()` inside a function decorated with `observe()` runs while that
function's span is still open, so the run's own span is not in the batch being
drained. Moving this one call inside `handle()` and killing the workers with
`SIGKILL` instead of `SIGTERM` produces sixteen spans instead of nineteen: all six
model calls and all six tool calls arrive, and three of the six agent runs are
gone, which leaves those twelve spans with no agent attached.

A process that exits normally drains what is queued, so the mistake hides until
something kills the process, which is what happens when a container is stopped.

## Carrying trace context between processes

The SDK has no `convergent.inject` and no `convergent.extract`. Moving trace
context between processes is OpenTelemetry's job, and you do it with its own
propagator.

The dispatcher writes the context into a field beside the message body:

```python
carrier: dict[str, str] = {}
inject(carrier)
queue.put({"body": {"invoice_id": invoice_id}, "message_attributes": carrier})
```

The worker reads it back and attaches it before running the item:

```python
token = otel_context.attach(extract(message["message_attributes"]))
try:
    handle(message["body"])
finally:
    otel_context.detach(token)
```

The attach is needed because `observe()` records against whatever OpenTelemetry
context is active and takes no parent argument, so there is no way to hand the
extracted context to the decorator directly.

There are two things to know about the carrier. The propagator writes the key as
`traceparent`, in lower case, which is what the W3C trace context specification
says. And OpenTelemetry's default getter looks the key up by exact match, so a
carrier holding `Traceparent` reads back as no context and the worker starts a new
trace instead of joining yours. A queue message is a dict you build yourself, so
keeping the key as the propagator wrote it avoids the problem. A
transport that changes the case of its keys does not, and there you have to pass
`extract()` a getter of your own that matches case-insensitively.

## One spans file per worker

Every process names its own file:

```python
convergent.init(destinations=[convergent.File(spans_dir, filename=f"spans-worker-{index}.jsonl")])
```

Several processes appending to one file relies on append behavior a network
filesystem does not guarantee, which the
[`File` destination reference](../../docs/reference/api.md#file) covers.
Separate files remove the question. `show_spans.py` and `verify.py` both read every
`spans*.jsonl` in the directory. A trace split across four files still reads as one
trace.

## What stays the same on every run

Which worker picks up which invoice is not fixed and must not matter. Three
consecutive runs of this example gave one worker 3, 9, and 6 spans in turn, while
the total was 19 every time. So `verify.py` asserts totals and nesting, and never
counts spans per worker.

The token counts have to be a function of the invoice rather than of arrival order.
`stub_model.py` answers each invoice with a fixed pair of counts, so `inv_88213` is
always 180 in and 24 out. A counter that incremented per call would give different
numbers each run, because three workers reach the endpoint in whatever order they
get there.

## Mapping this onto ECS

The container runs the worker loop. `run_local.py` stands in for the scheduler that
starts and stops it.

Stopping a task is where tracing gets lost. Amazon ECS issues the equivalent of
`docker stop`, which sends the container's stop signal, `SIGTERM` unless the image
sets `STOPSIGNAL`, waits, and then sends `SIGKILL`. The wait is the container
definition's `stopTimeout`. On Fargate it defaults to 30 seconds and cannot exceed
120. On EC2 it falls back to the container agent's `ECS_CONTAINER_STOP_TIMEOUT`,
which is also 30 seconds by default. A container that exits within the window is
never sent `SIGKILL`.

So everything after `SIGTERM` has to fit inside `stopTimeout`: the invoice in hand,
and then the flush. A destination sends everything already queued before `flush()`
returns, so a long queue or a slow receiver carries the call past `timeout_ms`. Each
export request is bounded by `OTEL_EXPORTER_OTLP_TIMEOUT`, which defaults to 10
seconds; lower that variable when you need a tighter ceiling. In this example spans
go to a file, so nothing waits on the network and the flush returns immediately.
That bound applies once spans go to Convergent over the network.

A real deployment reads from Amazon SQS rather than a local queue, and the part that
transfers unchanged is the carrier. An SQS message attribute is custom metadata
carried alongside the body, its name may hold letters, digits, underscores, hyphens,
and periods, and the name is case-sensitive, so `traceparent` arrives at the
consumer spelled the way the dispatcher wrote it. The dispatcher's `inject` and the
worker's `extract` do not change.

## Files

| File | What it does |
| --- | --- |
| `dispatcher.py` | Opens the workflow span and queues six invoices, each with the trace context beside its body |
| `worker.py` | One worker process: `init()`, pull, extract the context, run the invoice, flush, and stop on `SIGTERM` |
| `stub_model.py` | A scripted chat completions endpoint on loopback, answering every worker at once |
| `run_local.py` | Starts the stub model, spawns three workers, runs the dispatcher, and drains the queue |
| `verify.py` | Asserts the nineteen spans, the single trace, the nesting, and the model attributes |

`run_local.py` uses the spawn start method on purpose. A spawned child starts with
none of the parent's tracing, so it has to call `init()` itself, which is what a
container does. It inherits the environment and nothing else, so `run_local.py` sets
`CONVERGENT_RELEASE`, `OTEL_SERVICE_NAME`, and the stub model's address before it
spawns anything.
