---
title: Configuration
description: What the SDK reads from the environment, and how it behaves at runtime.
---

## What gets installed

`pip install convergent-sdk` brings the SDK, the OpenTelemetry packages it
declares, and their dependencies. The install line sets a floor and no ceiling
on the OpenTelemetry packages, so current releases work. The SDK works with
OpenTelemetry 1.25.0 and newer, and 1.41.0 or later is recommended. Below
1.41.0 everything traces normally, but `FlushResult.dropped` cannot count the
spans a full queue threw away, so it reads 0. The count comes from an argument
`BatchSpanProcessor` gained in 1.41.0, and older releases get a processor
without it.

```
convergent-sdk
opentelemetry-api                        opentelemetry-proto
opentelemetry-sdk                        opentelemetry-semantic-conventions
opentelemetry-exporter-otlp-proto-http   opentelemetry-exporter-otlp-proto-common
googleapis-common-protos                 protobuf
requests                                 urllib3
certifi                                  charset-normalizer
idna                                     typing-extensions
```

The exporter package is the HTTP one, because the SDK sends spans over
OTLP/HTTP.

## Convergent variables

| Variable                     | Effect                                                        |
| ---------------------------- | ------------------------------------------------------------- |
| `CONVERGENT_API_KEY`         | the ingestion key. Implies the Convergent destination         |
| `CONVERGENT_ENDPOINT`        | receiver address. Defaults to `https://ingest.convergent.dev` |
| `CONVERGENT_RELEASE`         | the release, when `init(release=...)` is not passed. One of the two is required. |
| `CONVERGENT_REQUIRE_SPAN_ATTRIBUTES` | the `require_span_attributes` filter, as JSON, when the argument is not passed |
| `CONVERGENT_REJECT_SPAN_ATTRIBUTES` | the `reject_span_attributes` filter, as JSON, when the argument is not passed |
| `CONVERGENT_SPANS_DIR`       | write the spans the filters keep to `spans.jsonl` in this directory |
| `CONVERGENT_TRACES_EXPORTER` | `console` adds a console destination to the others            |
| `CONVERGENT_DEBUG`           | `1` gives the `convergent.sdk` logger a level of its own      |
| `CONVERGENT_STRICT`          | `1` makes a configuration that cannot work raise and stop the process at startup, instead of the default log-and-disable |

Each of `CONVERGENT_API_KEY`, `CONVERGENT_ENDPOINT`, and `CONVERGENT_RELEASE`
is the fallback for an `init()` argument of the same name, and the argument
wins.

`CONVERGENT_REQUIRE_SPAN_ATTRIBUTES` and `CONVERGENT_REJECT_SPAN_ATTRIBUTES`
fill `require_span_attributes` and `reject_span_attributes` the same way. Each
holds the mapping as JSON, e.g. `'{"customer.id": ["acme"]}'`. A value that is
not JSON is rejected the way a malformed argument is.

`CONVERGENT_SPANS_DIR` is read by `init()` only; `otel.install()` takes no
destinations. The variable adds a file destination beside any `destinations`
argument rather than replacing it, so a process that sets both writes the spans the
filters keep to two files and one warning says so.

`CONVERGENT_TRACES_EXPORTER=console` adds a destination rather than replacing
the others, so you can read what you are sending while still sending it.

`CONVERGENT_DEBUG` combines with the `debug` argument. Either one turns debug
logging on, so setting the variable enables it even when the code passes
`debug=False`.

`CONVERGENT_ENDPOINT` is for a receiver of your own, such as a customer-hosted
data plane or a collector running beside your process. A value that is not a URL
is rejected at `init()`.

## Strict startup

`init()` checks its arguments and the `CONVERGENT_*` variables before it changes
anything in the process, and `strict` decides what a failed check does. Off, the
default, logs the exact problem at ERROR, configures nothing, sends nothing, and
returns `Status(enabled=False, reason="invalid_config")`, so a telemetry mistake
never stops your deployment. On, with `strict=True` or `CONVERGENT_STRICT=1`, the
same problem raises while `init()` is still running and stops the process at
startup. Turn it on where a bad setup should be caught before it ships, such as
CI and staging.

The check itself is the same either way. A value of the wrong type raises
`TypeError`, a value of the right type that is not allowed raises `ValueError`,
and a variable that is not set is never a problem. These are the conditions:

- `api_key`, `endpoint`, or `release` is not a string. `TypeError`.
- `endpoint` or `CONVERGENT_ENDPOINT` is not a URL, such as `"ingest.convergent"`
  with no scheme. `ValueError`.
- Nothing is configured at all, meaning no `api_key` argument, no
  `CONVERGENT_API_KEY`, and no destinations. `ValueError` naming
  `CONVERGENT_API_KEY`.
- No release is set, meaning no `release` argument and no `CONVERGENT_RELEASE`.
  `ValueError`.
- `agents` is not a list of names, such as `agents="support-agent"`. `TypeError`.
- `agents` holds an empty name, more than 256 names, or a name over 512
  characters. `ValueError` naming the cap.
- `require_span_attributes` or `reject_span_attributes` is not a mapping of attribute names to values, such as
  `require_span_attributes="customer.id"`, or an attribute name in it is not a string.
  `TypeError`.
- `require_span_attributes` or `reject_span_attributes` is an empty mapping, or an attribute name in it is
  empty or padded with spaces. `ValueError`.
- A `require_span_attributes` or `reject_span_attributes` value is not a plain string, number, or bool. A
  nested list, a mapping, bytes, and a subclass such as an enum member are
  each a `TypeError` naming the fix.
- A `require_span_attributes` or `reject_span_attributes` value is `None`. `ValueError` saying to pass an
  empty list to match nothing.
- `CONVERGENT_REQUIRE_SPAN_ATTRIBUTES` or `CONVERGENT_REJECT_SPAN_ATTRIBUTES`
  holds a value that is not JSON. `ValueError` naming the variable.
- `destinations` holds anything other than a `File` or a `Console`. `TypeError`.
- A `File` destination's spans file cannot be opened, because the directory
  cannot be created or the file cannot be written. `OSError`.
- `tracer_provider` is not an OpenTelemetry SDK `TracerProvider`. `TypeError`.
- `debug` is not a bool. `TypeError`.
- `CONVERGENT_DEBUG`, `CONVERGENT_STRICT`, or `CONVERGENT_TRACES_EXPORTER` holds
  a value it does not recognize. `ValueError`. The first two take `1`, `true`,
  `yes`, `on`, `0`, `false`, `no`, and `off`. The third takes `console`.

One condition softens outside strict mode. A spans file that cannot be opened
costs that one destination, which is skipped with a warning while the rest of the
configuration delivers, and the configuration is rejected only once nothing is
left to send to.

A rejection leaves the process as it was, with one mark: proving a `File`
destination can be opened creates the file and any missing parent directories.
No provider is installed and no deployment is registered, so a corrected
`init()` call afterwards works.

## Destinations

`api_key` sends to Convergent. `destinations` adds places on top, and every span
the `agents`, `require_span_attributes`, and `reject_span_attributes` filters keep goes to all of them.

```python
convergent.init(release="1.4.0", destinations=[convergent.File("/data/traces"), convergent.Console()])
```

A `File` destination writes the spans the filters keep to disk instead of the
network, and it needs no credentials. Use it where the process cannot reach
the receiver, for example a locked-down container or a CI job. Two `File`
values naming one file collapse into one destination; two different files do
not, and one warning says every span is being written twice.

`File` and `Console` are the two destinations. Anything else in `destinations`,
such as a raw OpenTelemetry `SpanProcessor`, is rejected. To add a processor of
your own, take the provider from `tracer_provider()` and add it there. To reach a
third backend, point `CONVERGENT_ENDPOINT` at an OpenTelemetry Collector you run
and let it route, which is usually less work than writing an exporter.

## What gets sent

Whatever the filters keep is what leaves your process. Convergent does not scrub
or transform your content, so a prompt on a kept span reaches the receiver.

An exception's message and traceback are the one thing the SDK does not record. A
span records the exception's class name and nothing else, so
`raise ValueError("account 4Q7W2X is closed")` inside a `span()` block arrives as
the bare word `ValueError`.

Content that a framework or an instrumentation writes is theirs to configure, and
each one has its own switch. The [integrations](integrations/index.md) pages name the
switch for each package.

There is no redaction mode. Matching secret-shaped key names matches names rather
than values, so `{"note": "the password is hunter2"}` would go straight through.
To decide which spans leave your process by value, set `init(require_span_attributes=...)` or
`init(reject_span_attributes=...)`.
To transform or redact the content of the spans you do send, put an
[OpenTelemetry Collector](https://opentelemetry.io/docs/collector/configuration/)
between your process and the receiver. A Collector matches values as well as
names, it runs outside your application so a change is configuration rather than
a deploy, and one rule covers every destination and every service.

## OpenTelemetry variables

These are OpenTelemetry's own, and the SDK inherits them.

| Variable                            | Effect                                         | Default |
| ----------------------------------- | ---------------------------------------------- | ------- |
| `OTEL_SERVICE_NAME`                 | display name sent with deployment registration | none    |
| `OTEL_EXPORTER_OTLP_TIMEOUT`        | ceiling on one export                          | 10s     |
| `OTEL_BSP_SCHEDULE_DELAY`           | how often a batch is exported                  | 5s      |
| `OTEL_BSP_MAX_QUEUE_SIZE`           | span queue depth before the oldest is dropped  | 2048    |
| `OTEL_BSP_MAX_EXPORT_BATCH_SIZE`    | spans per batch                                | 512     |
| `OTEL_ATTRIBUTE_VALUE_LENGTH_LIMIT` | cap on one attribute value                     | none    |
| `OTEL_ATTRIBUTE_COUNT_LIMIT`        | cap on attributes per span                     | 128     |
| `OTEL_TRACES_SAMPLER`               | which spans are recorded                       | record every span |

`flush()` measures its own deadline from its `timeout_ms` argument, which
defaults to 5000.

A sampler is taken when a provider is constructed, so it cannot be added to one
that already exists. Either set `OTEL_TRACES_SAMPLER`, which the provider
`init()` creates reads, or construct `TracerProvider(sampler=...)` yourself and
hand it to `init(tracer_provider=...)`.

The SDK sets no attribute length cap of its own. OpenTelemetry enforces one by
cutting the finished string, and a `gen_ai.*.messages` value is JSON, so the cut
lands mid token and the reader loses all of the content rather than the
overflow. Content too large to keep on a span is moved to the blob store at
ingest and replaced by a reference. A span whose content is too large for the
receiver's body limit is refused with a 413, which the SDK logs.

## Runtime behavior

Each destination exports on a background daemon thread of its own, so a span your
code records does not wait on the network. There is one tracer provider for the
whole process, so tracing several agents does not multiply threads or flush time.

Under sustained backpressure the oldest span is dropped and OpenTelemetry logs
`Queue full, dropping Span.` once per drop. `FlushResult.dropped` counts them.

Network export failures retry and then discard the batch with an error log. They
are not spilled to disk, and your code never sees an exception.

On 401 or 403 the SDK stops sending and logs `disabled because the collector
rejected its credentials` once. That check sits in the span exporter, so the
line appears on the first span batch and never in a process that records no
span. Sending stays off for the life of the process.

A child process gets fresh copies of the SDK's locks, so a fork does not inherit
a lock another thread held at the moment of the fork.
