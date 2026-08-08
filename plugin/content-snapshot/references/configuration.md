# Configuration

The Configuration page covers what the SDK reads from the environment and how it
behaves once it is running. It lists the fifteen packages the install brings,
then every `CONVERGENT_*` variable and the `init()` argument each one backs. It
gives the OpenTelemetry variables the SDK inherits with their defaults,
including the export interval and the queue depth, and the `File` and `Console`
destinations that write spans somewhere other than the network. The runtime
section covers the export threads, what happens under backpressure, what a
failed export does, and the 401 or 403 that turns sending off for the life of
the process.

Page: `python/docs/configuration.md` in https://github.com/convergent-technologies/convergent-sdk.
