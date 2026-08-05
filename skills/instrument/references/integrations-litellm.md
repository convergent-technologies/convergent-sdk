# litellm

The litellm page covers turning on litellm's built-in OpenTelemetry callback, so
there is no instrumentation package to install. It gives the install
line, the `pydantic-settings` dependency the callback imports, and the
`USE_OTEL_LITELLM_REQUEST_SPAN` variable without which litellm writes the model
attributes onto your agent span and opens no model span of its own. It says to
append to `litellm.callbacks` rather than assign to it, and lists what a
`litellm_request` span carries, including litellm's own price for the call.

Page: `python/docs/integrations/litellm.md` in this SDK's documentation tree.
