---
name: convergent-instrument
description: Add or repair Convergent tracing for one Python AI agent, then verify a representative local recording. Use for requests to instrument an agent with convergent-sdk or fix missing agent, model, or tool spans.
---

# Instrument one Python agent

Add the smallest tracing change that records the requested agent's executed path.
Preserve application behavior, dependency constraints, and existing telemetry.
Read [Python setup](references/python.md) before editing.

## Inspect the application

Identify the agent entry point, the model client it calls, its tools, and one
representative run command. Inspect the installed SDK and framework versions.
Find the existing tracer provider, instrumentors, exporters, and release source.
Read environment variable names without printing secret values.

Use the user's selected target and existing authorization. Ask only when a
missing target, command, or content decision would make the next action a guess.
A representative run may call paid model APIs or mutate external systems; keep
those actions within the user's authorized scope.

Before editing, state the expected recording: the named agent, executed model
and tool calls, parent relationships, release, content fields, and any session
or export filter to check. Explain which input and output content will be
recorded. Use non-sensitive test input when available.

## Configure and instrument

Use the project's package manager. Preserve its pins. Add only the SDK and the
integration the executed path needs. Report an installed API mismatch before
changing a version constraint.

Follow the bundled Python reference for setup, provider ownership, framework
choices, filters, and sessions. Use one instrumentor per model request. Add SDK
agent and tool spans only where the framework does not already record them.
Give agents stable names.

Use `convergent.current_span()` inside a decorated function. Use `set_input()`
and `set_output()` for content when manual spans need it. Keep model spans open
until the response or stream completes. Copy token usage from the response;
do not invent counts when the provider omits them.

## Record and verify

Set `CONVERGENT_STRICT=1` and point `CONVERGENT_SPANS_DIR` at a fresh temporary
directory in the run environment. For local export only, omit both
`CONVERGENT_API_KEY` and an explicit `api_key=`. Run the representative command.
Flush after traced work and framework callbacks end. Report the recording path.

Use `convergent-verify` when it is installed. Otherwise inspect the OTLP JSONL
directly. Compare the executed path with the expected spans, parent and trace
IDs, release, session IDs, content, usage, and errors. Check the complete file
for both kept and withheld requests when filters are configured.

Fix only issues supported by the source and recording. Rerun after each related
change and inspect the new recording. Stop when the expected recording is
present or when a non-instrumentation problem prevents progress. Repeating an
unchanged run without new evidence is not a fix.

## Report

Report edited files, exact run command, package versions, recording path, trace
ID, and observed spans. List unexecuted paths and unresolved findings. Distinguish
local instrumentation, real provider calls, and hosted delivery.

For a user-authorized hosted check, call `convergent.check()` in the initialized
process after the run and flush. Keep its diagnostic result, then inspect the
specific trace in the workspace before claiming it is visible there. A local
recording or successful flush alone proves neither ingestion nor display.
