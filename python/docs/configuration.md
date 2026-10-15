---
title: Configuration
description: What the SDK reads from the environment and how it behaves at runtime.
---

## What gets installed

`pip install convergent-sdk` installs the SDK, the OpenTelemetry packages it
declares, and their dependencies.

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

The SDK sets a minimum OpenTelemetry version and no maximum, so current
releases work. The SDK works with OpenTelemetry 1.25.0 and newer. Use 1.41.0
or later. Below 1.41.0, tracing works normally, but `FlushResult.dropped`
always reads 0. The count of spans dropped from a full queue comes from an
argument that `BatchSpanProcessor` gained in 1.41.0.

The SDK sends spans over OTLP/HTTP, so it installs the HTTP exporter package.

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

`CONVERGENT_API_KEY`, `CONVERGENT_ENDPOINT`, and `CONVERGENT_RELEASE` are
fallbacks for the `init()` arguments of the same name. The argument wins.

`CONVERGENT_REQUIRE_SPAN_ATTRIBUTES` and `CONVERGENT_REJECT_SPAN_ATTRIBUTES`
fill `require_span_attributes` and `reject_span_attributes` the same way. Each
holds the mapping as JSON, e.g. `'{"customer.id": "acme"}'`. A value that is
not JSON is rejected like a malformed argument. The variable configures the
filter but does not mark any span. A span passes
`CONVERGENT_REQUIRE_SPAN_ATTRIBUTES` only when a `context_attributes=` mark,
the span's own attribute, or a resource attribute holds the key. See
[Mark the agent run](reference/api.md#agent).

Only `init()` reads `CONVERGENT_SPANS_DIR`. `otel.install()` takes no
destinations. The variable adds a file destination next to any `destinations`
argument. If you set both, the SDK writes the kept spans to two files and logs
one warning.

`CONVERGENT_TRACES_EXPORTER=console` adds a destination and keeps the others,
so you can read what you send while still sending it.

`CONVERGENT_DEBUG` combines with the `debug` argument. Either one turns on debug
logging, so the variable enables it even when the code passes `debug=False`.

Set `CONVERGENT_ENDPOINT` to send to a receiver of your own, such as a
customer-hosted data plane or a collector running beside your process. `init()`
rejects a value that is not a URL.

## Strict startup

`init()` checks its arguments and the `CONVERGENT_*` variables before it changes
anything in the process. The `strict` setting decides what a failed check does.

| Mode | On a failed check |
| --- | --- |
| Off (default) | Logs the exact problem at ERROR, configures nothing, sends nothing, and returns `Status(enabled=False, reason="invalid_config")`. A telemetry mistake never stops your deployment. |
| On (`strict=True` or `CONVERGENT_STRICT=1`) | Raises while `init()` is still running and stops the process at startup. |

Turn strict mode on where you want to catch a bad setup before it ships, such
as CI and staging.

The checks are the same in both modes. A value of the wrong type raises
`TypeError`. A value of the right type that is not allowed raises `ValueError`.
A variable that is not set never fails a check. In the table, "a filter" means
`require_span_attributes` or `reject_span_attributes`.

| Condition | Error |
| --- | --- |
| `api_key`, `endpoint`, or `release` is not a string. | `TypeError` |
| `endpoint` or `CONVERGENT_ENDPOINT` is not a URL, such as `"ingest.convergent"` with no scheme. | `ValueError` |
| Nothing is configured: no `api_key` argument, no `CONVERGENT_API_KEY`, and no destinations. | `ValueError` naming `CONVERGENT_API_KEY` |
| No release is set: no `release` argument and no `CONVERGENT_RELEASE`. | `ValueError` |
| `agents` is not a list of names, such as `agents="support-agent"`. | `TypeError` |
| `agents` holds an empty name, more than 256 names, or a name over 512 characters. | `ValueError` naming the cap |
| A filter is not a mapping of attribute names to values, such as `require_span_attributes="customer.id"`, or an attribute name in it is not a string. | `TypeError` |
| A filter is an empty mapping, or an attribute name in it is empty or padded with spaces. | `ValueError` |
| A filter value is not a plain string, number, or bool, such as a nested list, a mapping, bytes, or a subclass such as an enum member. | `TypeError` naming the fix |
| A filter value is `None`. | `ValueError` saying to pass an empty list to match nothing |
| `CONVERGENT_REQUIRE_SPAN_ATTRIBUTES` or `CONVERGENT_REJECT_SPAN_ATTRIBUTES` holds a value that is not JSON. | `ValueError` naming the variable |
| `destinations` holds anything other than a `File` or a `Console`. | `TypeError` |
| A `File` destination's spans file cannot be opened, because the directory cannot be created or the file cannot be written. | `OSError` |
| `tracer_provider` is not an OpenTelemetry SDK `TracerProvider`. | `TypeError` |
| `debug` is not a bool. | `TypeError` |
| `CONVERGENT_DEBUG`, `CONVERGENT_STRICT`, or `CONVERGENT_TRACES_EXPORTER` holds a value it does not recognize. | `ValueError` |

`CONVERGENT_DEBUG` and `CONVERGENT_STRICT` accept `1`, `true`, `yes`, `on`, `0`,
`false`, `no`, and `off`. `CONVERGENT_TRACES_EXPORTER` accepts `console`.

Outside strict mode, a spans file that cannot be opened costs only that
destination. The SDK skips it with a warning, and the rest of the configuration
still delivers. The configuration is rejected only when no destination is left.

A rejection leaves the process as it was, with one exception. Checking that a
`File` destination can be opened creates the file and any missing parent
directories. No provider is installed and no deployment is registered, so a
corrected `init()` call afterwards works.

## Destinations

`api_key` sends to Convergent. `destinations` adds more places. Every span that
the `agents`, `require_span_attributes`, and `reject_span_attributes` filters
keep goes to all of them.

```python
convergent.init(release="1.4.0", destinations=[convergent.File("/data/traces"), convergent.Console()])
```

A `File` destination writes the kept spans to disk instead of the network and
needs no credentials. Use it where the process cannot reach the receiver, such
as a locked-down container or a CI job. Two `File` values naming the same file
collapse into one destination. Two `File` values naming different files stay
separate, and one warning says that every span is written twice.

`File` and `Console` are the only destinations. `init()` rejects anything else
in `destinations`, such as a raw OpenTelemetry `SpanProcessor`. To add your own
processor, take the provider from `tracer_provider()` and add it there. To
reach a third backend, point `CONVERGENT_ENDPOINT` at an OpenTelemetry
Collector you run and let it route the spans. This is usually less work than
writing an exporter.

## What gets sent

Everything the filters keep leaves your process. Convergent does not scrub or
transform your content, so a prompt on a kept span reaches the receiver.

The SDK does not record an exception's message or traceback. A span records
only the exception's class name, so `raise ValueError("account 4Q7W2X is closed")`
inside a `span()` block arrives as the bare word `ValueError`.

Each framework and instrumentation has its own switch for the content it
writes. The [integrations](integrations/index.md) pages name the switch for
each package.

The SDK has no redaction mode. Matching secret-shaped key names would check
names, not values, so `{"note": "the password is hunter2"}` would go straight
through.

- To choose which spans leave your process by value, set
  `init(require_span_attributes=...)` or `init(reject_span_attributes=...)`.
- To transform or redact the content of the spans you send, put an
  [OpenTelemetry Collector](https://opentelemetry.io/docs/collector/configuration/)
  between your process and the receiver.

A Collector matches values as well as names. It runs outside your application,
so a change is a configuration change rather than a deploy. One rule covers
every destination and every service.

## OpenTelemetry variables

The SDK inherits OpenTelemetry's own variables.

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

`flush()` takes its deadline from its own `timeout_ms` argument, which defaults
to 5000.

A provider takes its sampler when it is constructed, so you cannot add a
sampler to an existing provider. Either set `OTEL_TRACES_SAMPLER`, which the
provider that `init()` creates reads, or construct `TracerProvider(sampler=...)`
yourself and pass it to `init(tracer_provider=...)`.

The SDK sets no attribute length cap of its own. OpenTelemetry enforces a cap
by cutting the finished string. A `gen_ai.*.messages` value is JSON, so the cut
lands mid token and the reader loses all of the content, not only the overflow.
At ingest, Convergent moves content too large to keep on a span to the blob
store and replaces it with a reference. The receiver refuses a span whose
content exceeds its body limit with a 413, and the SDK logs it.

## Runtime behavior

Each destination exports on its own background daemon thread, so recording a
span does not wait on the network. The process has one tracer provider, so
tracing several agents does not multiply threads or flush time.

Under sustained backpressure, the oldest span is dropped and OpenTelemetry logs
`Queue full, dropping Span.` once per drop. `FlushResult.dropped` counts them.

When a network export fails, the SDK retries, then discards the batch and logs
an error. It does not spill the batch to disk, and your code never sees an
exception.

On a 401 or 403, the SDK stops sending and logs
`disabled because the collector rejected its credentials` once. The span
exporter makes this check, so the line appears on the first span batch and
never in a process that records no span. Sending stays off for the life of the
process.

A child process gets fresh copies of the SDK's locks, so a fork does not inherit
a lock another thread held at the moment of the fork.
