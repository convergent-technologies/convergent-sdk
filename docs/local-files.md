---
title: Write spans to a file or the console
description: Keep spans on disk or print them, with no API key and no network.
---

`File` and `Console` are destinations: places the SDK writes finished spans.
They need no API key and make no network request. Pass them to `init()`. With
an API key they are added to Convergent, so every span goes to all of them.

```python tab="Python"
convergent.init(
    release="2026.10.06.1",
    destinations=[convergent.File("./traces"), convergent.Console(stream="stderr")],
)
```

```ts tab="TypeScript"
import { Console, File, init } from "convergent-sdk";

init({
  release: "2026.10.06.1",
  destinations: [new File("./traces"), new Console({ stream: "stderr" })],
});
```

Both receive only the spans the [span filters](span-filters.md) keep.

Use `File` where the process cannot reach Convergent, such as a locked down
container or a CI job. Use `Console` while developing, to see exactly what is
sent. `Console` also works as a transport on platforms that collect stdout,
such as AWS Lambda, Cloud Run, or Modal. Moving those logs to Convergent then
takes a log forwarder that you run.

`File` and `Console` are the only destinations. To add your own exporter, add
it to your OpenTelemetry provider. When you attach Convergent to a provider you
own, with `otel.install()` / `ConvergentSpanProcessor` (Python) or
`ConvergentSpanProcessor` (TypeScript), destinations are not accepted and an
API key is required.

## What each destination writes

| Destination | Writes |
| --- | --- |
| `File(path, filename="spans.jsonl", mode=0o600)` (Python) / `new File(path, { filename, mode })` (TypeScript) | `<path>/<filename>`, one span per line |
| `Console(stream="stdout", pretty=False)` (Python) / `new Console({ stream, pretty })` (TypeScript) | the same lines to `stdout` or `stderr`. `pretty` indents each span for reading. |

`filename` defaults to `spans.jsonl` and `mode` to `0o600`, so only the file's
owner can read it. `stream` defaults to `stdout`, and `pretty` is off.

## Turn them on from the environment

- `CONVERGENT_SPANS_DIR=<dir>` adds a `File` that writes `spans.jsonl` in that
  directory.
- `CONVERGENT_TRACES_EXPORTER=console` adds a `Console` when you did not
  configure one. The value ignores case, and a blank value is ignored. Any
  other value makes the configuration invalid.

Both add to the destinations you pass rather than replacing them, so you can
read what you send while still sending it. Two names for the same file write
it once. Two different files each get every span, and the SDK logs a warning.

Only `init()` reads these variables.

## The file format

Every Convergent SDK writes the same format. Each line is one OTLP/JSON
`ExportTraceServiceRequest` holding one span. Trace and span IDs are hex,
enums are integers, 64 bit numbers are strings, and default values are left
out.

Pass `release`. With no API key, no deployment is registered, so the release
on each span is the only record of which version wrote it.

## Privacy

Both destinations write every recorded prompt and output. In a hosted runtime,
console output usually reaches the platform's log service, so those logs then
hold your prompts. Widen a file's `mode` only when you know who else can read
the directory.

## File safety rules

- `filename` must be a bare name. A name with a directory part, such as
  `../spans.jsonl`, is refused when you create the `File`. So is a `mode`
  outside `0` to `0o777`.
- The SDK creates missing directories and opens the file during setup. It
  appends and never truncates.
- The SDK applies `mode` to a new file and to an existing one. If the
  filesystem refuses the change, the SDK warns and keeps the existing
  permissions.
- On Linux and macOS, a symbolic link at the file itself is refused. Links in
  the parent directories are followed.
- TypeScript also refuses a file that is not a regular file or that another
  user owns. On Windows, access depends on the file's ACLs, not on `mode`.
- Several processes can append to one file on a local disk without splitting
  lines. A network filesystem does not guarantee this, so keep the directory
  on a local disk you control.

## When a file cannot be opened

A file that cannot be opened is skipped with a warning, and the other
destinations keep working. With strict mode on, `init()` raises that error
instead. If no destination is left, `init()` returns reason `invalid_config`.

A failed write never raises into your code. `flush()` waits for written spans
and syncs the file to disk.

## Check a file only setup

`init()` lists the destinations that started, such as `convergent`,
`file:/srv/app/traces/spans.jsonl`, and `console:stderr`.

`check()` reports false for a working file only setup, because there is no API
key to check. To confirm it works, check that `init()` returned `enabled` true
and that the spans file has content.
