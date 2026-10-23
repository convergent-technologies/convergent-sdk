---
title: Configuration
description: What the SDK reads from the environment and how it behaves at runtime.
---

## What gets installed

`npm install convergent-sdk @opentelemetry/api` installs the SDK and the
OpenTelemetry for JavaScript packages it depends on: the 2.10 SDK packages and
the matching 0.221 OTLP exporter packages.

`@opentelemetry/api` is a peer dependency. The SDK takes version 1.9. Install
it yourself so your application and the SDK share one copy. Two different
copies can stop `init()` from registering. See [Runtime behavior](#runtime-behavior).

The SDK needs Node.js 22.15 or newer.

## Convergent variables

`init()` reads an environment variable for each option you leave out. The
option wins.

| Variable | Option | Effect | Default |
| --- | --- | --- | --- |
| `CONVERGENT_API_KEY` | `apiKey` | the ingestion key. Sends spans to Convergent | required unless a `File` or `Console` destination is set |
| `CONVERGENT_ENDPOINT` | `endpoint` | the receiver address | `https://ingest.convergent.dev` |
| `CONVERGENT_RELEASE` | `release` | the version of your code | required |
| `CONVERGENT_REQUIRE_SPAN_ATTRIBUTES` | `requireSpanAttributes` | the require filter, as JSON | none |
| `CONVERGENT_REJECT_SPAN_ATTRIBUTES` | `rejectSpanAttributes` | the reject filter, as JSON | none |
| `CONVERGENT_SPANS_DIR` | none | also write spans to `spans.jsonl` in this directory | none |
| `CONVERGENT_TRACES_EXPORTER` | none | `console` also writes spans to the console | none |
| `CONVERGENT_STRICT` | `strict` | stop the process at startup on a bad configuration | `false` |
| `CONVERGENT_DEBUG` | `debug` | show the SDK's warnings on the console | `false` |

`CONVERGENT_STRICT` and `CONVERGENT_DEBUG` turn on with `1`, `true`, `yes`, or
`on`, and stay off with `0`, `false`, `no`, or `off`, in any case. An empty
value counts as unset. Any other value, or a `strict` or `debug` option that
is not a boolean, makes the configuration invalid. A flag is on when either the
option or its variable turns it on, so `CONVERGENT_DEBUG=1` turns on debug even
when the code passes `debug: false`.

The two filter variables hold the same mapping as the option, as JSON, such as
`'{"customer.id": "acme"}'`. They apply only when the option is left out. A
value that is not JSON makes the configuration invalid.
[Span filters](../../docs/span-filters.md) has the rules.

`CONVERGENT_SPANS_DIR` adds `new File(<dir>)`. `CONVERGENT_TRACES_EXPORTER=console`
adds a `Console` when none is configured. Both add to the `destinations`
option and do not replace it. The exporter value ignores case, and a blank
value is ignored. Any value other than `console` makes the configuration
invalid.

`ConvergentSpanProcessor` reads the same variables, except `CONVERGENT_DEBUG`,
`CONVERGENT_SPANS_DIR`, and `CONVERGENT_TRACES_EXPORTER`.

## Strict startup

`init()` checks its options and the `CONVERGENT_*` variables before it sets up
anything. The `strict` setting decides what a failed check does.

| Mode | On a failed check |
| --- | --- |
| Off (default) | Logs one warning naming the first problem, sends nothing, and returns `enabled: false` with `reason: "invalid_config"`. A telemetry mistake never stops your deployment. |
| On (`strict: true` or `CONVERGENT_STRICT=1`) | Throws the same problem from `init()`, before tracing is set up, so the deployment stops at startup. |

Turn strict mode on where you want to catch a bad setup before it ships, such
as CI and staging. Strict mode affects only this startup check.

An option of the wrong type throws a `TypeError`. A value that cannot work
throws an `Error`.

| Condition | Error |
| --- | --- |
| `strict` or `debug` is not a boolean. | `TypeError` |
| `CONVERGENT_STRICT` or `CONVERGENT_DEBUG` holds a value it does not take. | `Error` |
| `apiKey`, `endpoint`, or `release` is not a string. | `TypeError` |
| `destinations` is not an array, or holds something other than a `File` or a `Console`. | `TypeError` |
| `CONVERGENT_TRACES_EXPORTER` holds a value other than `console`. | `RangeError` |
| `agents` is not a list of strings. | `TypeError` |
| `agents` holds an empty name, more than 256 names, or a name over 512 characters. | `Error` |
| A filter is not a plain object, or a value in it is not a string, number, boolean, or a list of them. | `TypeError` |
| A filter is an empty object such as `requireSpanAttributes: {}`, an attribute name in it is empty or padded with spaces, or a value is `null`. | `Error` |
| A filter variable does not hold JSON. | `Error` naming the variable |
| `endpoint` or `CONVERGENT_ENDPOINT` is not an `http` or `https` URL, or holds a user name, password, query, or fragment. | `Error` |
| No API key and no destination is configured. | `Error` naming `CONVERGENT_API_KEY` |
| The API key holds a line break. | `Error` |
| No release is set. | `Error` |
| A `File` destination's file cannot be opened. | the error from Node's file system |

The warning never shows the value of the endpoint, the key, or a variable, in
case it holds a secret.

Outside strict mode, a file that cannot be opened costs only that destination.
The SDK skips it with a warning and the other destinations keep working. The
configuration is rejected only when no destination is left.

After a successful `init()`, a later call keeps the first configuration and
never throws. Its `debug` still applies.

## What gets sent

Every span the filters keep leaves your process. Convergent does not scrub or
change your content. Whatever you pass to `setInput()`, `setOutput()`, and
`setAttribute()` is sent as you gave it.

The SDK does not record an error's message or stack. When a traced function
throws, the span records only the error's class name. A thrown string records
nothing.

The SDK has no redaction option. To choose which spans leave your process, use
the [span filters](../../docs/span-filters.md). To change or redact the content
of the spans you send, put an
[OpenTelemetry Collector](https://opentelemetry.io/docs/collector/configuration/)
between your process and the receiver, and point `CONVERGENT_ENDPOINT` at it.

With an API key, spans go to `<endpoint>/v1/traces` as OTLP over HTTP,
protobuf, gzip compressed, with the key in a `Bearer` authorization header.

A `File` or `Console` destination writes every recorded prompt and output too.
In a hosted runtime, console output usually reaches the platform's log service.

## Destinations

`File` and `Console` write spans to a local file or the console, with or
without an API key. [Local files](../../docs/local-files.md) explains how to use
them.

## OpenTelemetry variables

OpenTelemetry's own settings apply unchanged.

| Variable | Effect |
| --- | --- |
| `OTEL_SERVICE_NAME` | the service name on spans from the provider `init()` creates |
| `OTEL_RESOURCE_ATTRIBUTES` | extra resource attributes on that provider |
| `OTEL_BSP_MAX_QUEUE_SIZE` | spans each destination holds before new spans are dropped. Default 2048 |
| `OTEL_BSP_MAX_EXPORT_BATCH_SIZE` | spans sent in one request |
| `OTEL_BSP_SCHEDULE_DELAY` and the other `OTEL_BSP_*` variables | batching |
| `OTEL_EXPORTER_OTLP_HEADERS`, `OTEL_EXPORTER_OTLP_TRACES_HEADERS` | extra headers on every request |

The provider that `init()` creates sets your release as `service.version`. The
release wins over a `service.version` in `OTEL_RESOURCE_ATTRIBUTES`. The
provider also carries OpenTelemetry's default resource attributes and a random
`service.instance.id` for each process.

OpenTelemetry adds the headers in `OTEL_EXPORTER_OTLP_HEADERS` and
`OTEL_EXPORTER_OTLP_TRACES_HEADERS` to every request to Convergent, so headers
meant for another collector reach Convergent too. When either is set, the SDK
warns once and names the variable, never its value. Your Convergent API key
still wins over an `authorization` entry there.

## See the SDK's warnings

The SDK writes its warnings through OpenTelemetry's diagnostic logger. Node
shows nothing until a logger is set. You have two ways to see them.

Turn on `debug: true` or `CONVERGENT_DEBUG=1`. When your application has not
set a diagnostic logger, the SDK sets a console logger at the `INFO` level.
That also shows OpenTelemetry's own export failure and retry messages. The SDK
never replaces a logger your application set.

Or set your own logger before `init()` and leave `debug` off:

```ts
import { diag, DiagConsoleLogger, DiagLogLevel } from "@opentelemetry/api";

diag.setLogger(new DiagConsoleLogger(), DiagLogLevel.WARN);
```

With your own logger, you choose its level. OpenTelemetry's `DEBUG` level
prints the spans being exported, so use it only where logs may hold your
prompts and outputs.

## Runtime behavior

`init()` creates a tracer provider and registers it for the whole process. It
also registers a context manager, unless one is already registered, so a
session and the active span follow `await`. If OpenTelemetry refuses to
register the provider, tracing stays off and `init()` returns
`reason: "existing_provider"`. Two causes are common: another
library already registered a provider, or two copies of `@opentelemetry/api`
are loaded. Run `npm ls @opentelemetry/api` first. A copy bundled inside
another package does not show there. To keep your own provider, use
[ConvergentSpanProcessor](opentelemetry.md) instead.

Each destination sends spans in batches in the background, so recording a span
does not wait on the network. Each destination holds up to 2,048 spans
waiting to be sent. When the queue is full, new spans are dropped.
`OTEL_BSP_MAX_QUEUE_SIZE` changes the limit. A larger queue handles a larger
burst and uses more memory.

When the server refuses a batch as too large (HTTP 413), the SDK warns once.
That batch is lost and later batches are still sent. Lower
`OTEL_BSP_MAX_EXPORT_BATCH_SIZE` to send fewer spans per request. A single span
that is too large on its own needs smaller content.

When the server rejects the API key (HTTP 401 or 403), the SDK warns once and
stops sending spans over the network for the life of the process.

A failed export never throws into your code. The SDK logs a warning instead.

When a script runs out of work, the SDK sends its pending spans once, on
Node's `beforeExit` event. That does not happen after `process.exit()`, a
crash, `SIGKILL`, or a platform freezing the process, and it does not wait for
file uploads. The SDK installs no signal handler. For a script, a server, or a
serverless function, call [flush()](reference/api.md#flush) yourself.
