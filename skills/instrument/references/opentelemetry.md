# Already using OpenTelemetry

The Already using OpenTelemetry page covers an app that produces OpenTelemetry
spans before Convergent is in it. It says what `init()` does when a tracer
provider already exists, which is add its own span processors and leave the
resource attributes, the samplers, and the exporters alone. It explains the
`agents=[...]` filter and which spans that keeps, how to hand `init()` a
provider of your own, and how to place `convergent.otel.install()` in a pipeline
you build by hand with no `init()` call at all. It closes with what a second
`init()` in one process does.

Page: `python/docs/opentelemetry.md` in this SDK's documentation tree.
