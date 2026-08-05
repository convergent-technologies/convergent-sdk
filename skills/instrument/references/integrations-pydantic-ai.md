# pydantic-ai

The pydantic-ai page covers pointing pydantic-ai's own instrumentation at the
tracer provider `init()` configured, so each `run()` becomes an agent run in the
workspace. It gives the install line and the `Instrumentation` capability
to pass, including the `include_content=True` that puts prompts and completions
in the trace. It says to name every agent, to use one `Instrumentation`
capability per agent, and to leave `agent()` off a `run()` that pydantic-ai
opens itself. It closes with the spans and attributes a run arrives with.

Page: `python/docs/integrations/pydantic-ai.md` in this SDK's documentation tree.
