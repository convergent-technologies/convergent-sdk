# API

The API page is the whole public surface, one section per call, with a field
table for every type a call returns. It documents `init()`, `check()`,
`observe()`, `agent()`, `tool()`, `span()`, `current_span()`,
`current_trace()`, `flush()`, `tracer_provider()`, the `File` and `Console`
destinations, `otel.install()`, and `ConvergentSpanProcessor`. The tables give
each argument, the environment variable behind it, and its default, along with
the fields of `Status`, `Report`, `Note`, `SpanHandle`, `TraceRef`, and
`FlushResult`. It also holds the table mapping each operation string to the span
name it produces.

Page: `python/docs/reference/api.md` in https://github.com/convergent-technologies/convergent-sdk.
