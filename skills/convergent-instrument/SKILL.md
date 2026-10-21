---
name: convergent-instrument
description: Add or repair Convergent tracing for one Python or TypeScript AI agent, then verify a representative local recording. Use for requests to instrument an agent with convergent-sdk or fix missing agent, model, or tool spans.
---

# Instrument one agent

Add the smallest tracing change that records the requested agent's executed path.
Preserve application behavior, dependency constraints, and existing telemetry.
Before editing, read [Python setup](references/python.md) for a Python agent or
[TypeScript setup](references/typescript.md) for a Node.js agent.

## Inspect the application

Identify the agent entry point, the model client it calls, its tools, and one
representative run command. Inspect the installed SDK and framework versions.
Find the existing tracer provider, instrumentors, exporters, and release source.
Read environment variable names without printing secret values.

Use the user's selected target and existing authorization. Ask only when a
missing target, command, or content decision would make the next action a guess.
A representative run may call paid model APIs or mutate external systems. Keep
those actions within the user's authorized scope.

Before editing, state the expected recording: the named agent, executed model
and tool calls, parent relationships, release, content fields, and any session
or export filter to check. Explain which input and output content will be
recorded. Use non-sensitive test input when available.

## Configure and instrument

Use the project's package manager. Preserve its pins. Add only the SDK and the
integration the executed path needs. Report an installed API mismatch before
changing a version constraint.

Follow the bundled reference for the agent's language for setup, provider
ownership, framework choices, filters, and sessions. Use one instrumentor or one
SDK span per model request. Add SDK agent and tool spans only where the
framework does not already record them. Give agents stable names.

Inside a decorated or wrapped function, use `convergent.current_span()` in
Python or `currentSpan()` in TypeScript. Use `set_input()` and `set_output()`,
or `setInput()` and `setOutput()`, for content when manual spans need it. Keep
model spans open until the response or stream completes.

For manual spans, follow the reference's recording rules. They decide the model
span name, optional model fields, the message format, the token keys, tools the
provider runs, agents used as tools, and the name of one top agent span for a
request that runs several agents. Both references state the same rules, so recordings from
the two languages match. Do not invent counts the provider omits.

## Record and verify

Set `CONVERGENT_STRICT=1` and point `CONVERGENT_SPANS_DIR` at a fresh temporary
directory in the run environment. For local export only, omit both
`CONVERGENT_API_KEY` and an explicit `api_key=` or `apiKey`. Run the
representative command. Where the language reference says a setup writes no
local file, use the check it names instead and report that limit.
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

For a user-authorized hosted check, call `convergent.check()` in Python, or
await `check()` in TypeScript, in the initialized process after the run and
flush. Keep its diagnostic result, then inspect the
specific trace in the workspace before claiming it is visible there. A local
recording or successful flush alone proves neither ingestion nor display.
