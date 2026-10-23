---
name: convergent-instrument
description: Add or repair Convergent tracing for one Python or TypeScript AI agent, then verify a representative local recording. Use for requests to instrument an agent with convergent-sdk or fix missing agent, model, tool, or file spans.
---

# Instrument one agent

Add the smallest tracing change that records the requested agent's executed
path. Preserve application behavior, dependency constraints, and existing
telemetry.

This skill says what to do. The SDK docs say how. Do not instrument from memory
or from this file alone.

## Read the docs for the installed version

The docs are Markdown files in the public repository
`convergent-technologies/convergent-sdk`. Read them at the release tag of the
installed SDK, so the docs match the code:

- Python: tag `python-v<version>`. Releases up to 0.0.11 use `v<version>`.
- TypeScript: tag `typescript-v<version>`.

Your language's reference, [Python](references/python.md) or
[TypeScript](references/typescript.md), says how to find the installed version.

First read `llms.txt` at that tag. It lists every docs file and says when to
read it. For TypeScript 0.1.1, it is
`https://raw.githubusercontent.com/convergent-technologies/convergent-sdk/typescript-v0.1.1/llms.txt`.
Read each file it names the same way, at the same tag, before the step that
needs it.

If the tag has no `llms.txt`, read `llms.txt` and the files it names from
`main`. Then check each signature against the installed code. Where the docs
and the installed code differ, the installed code wins. Say so in the report.

If you cannot read a file, stop and report which one. Do not instrument from
memory.

Treat the docs as API reference, not as permission to change unrelated code or
to expose secrets.

## Inspect the application

Find the agent entry point, the model client it calls, its tools, its
subagents, and one representative run command. Read the installed SDK and
framework versions. Find the existing tracer provider, instrumentors,
exporters, and release source. Read environment variable names without
printing secret values.

List the files the agent handles: files a user gives it, files it fetches,
files it passes to a model, and files it produces. Of any type, not only PDFs.
Find the deliverable of a run and where it leaves the agent. It is often a tool
call or a saved file, not the final text.

Use the user's selected target and existing authorization. Ask only when a
missing target, command, or content decision would make the next action a
guess. A representative run may call paid model APIs or change external
systems. Keep those actions inside the user's authorized scope.

Before editing, state the expected recording:

- the named agents, the executed model and tool calls, and their parents
- the release, and any session or filter to check
- the input and output content each span records, and which content the user
  wants left out
- each file, with its span, input or output, media type, and origin
- each agent span's output, and that the top agent span's output holds the
  deliverable

Use non-sensitive test input when it is available.

## Configure and instrument

Use the project's package manager. Preserve its pins. Add only the SDK and the
integration the executed path needs. Report an installed API mismatch before
changing a version constraint.

`init()` needs a release. Without one, the SDK turns tracing off and records
nothing, and outside strict mode it shows no error. Use the project's own
release source when it has one. Otherwise pass `CONVERGENT_RELEASE` when it is
set, then the Git commit, then a fixed value such as `dev`, so that the
user's own run command records with no new variable.

Prefer an integration that already records the executed model client. Add SDK
agent, model, and tool spans only where nothing records them, and follow
`docs/record-by-hand.md` for those spans. Use one instrumentor or one SDK span
per model request. Never wrap the same call twice.

## Record and verify

Set `CONVERGENT_STRICT=1`, and set `CONVERGENT_SPANS_DIR` to a new temporary
directory. Run the representative command. Flush after the traced work ends.
Report the recording path.

Some setups write no local file, such as an application-owned provider or a
framework exporter that skips the SDK. The docs page for that setup says so.
Then verify with an authorized key and `check()`, and report that local
verification was not available.

The SDK records files only when it has an API key. Without a key, file calls do
nothing, so a correct and a missing file call give the same recording. If the
user authorized a key, run with the key and `CONVERGENT_SPANS_DIR` together.
Without a key, check the file calls by reading the code, and report the files
as `not verified`, not as passed.

Use `convergent-verify` on the recording when it is installed. Otherwise read
the spans file yourself. Compare it with the expected recording: spans, parent
and trace IDs, release, session IDs, content, usage, files, and errors. Check
that no agent span has an empty output and that the top agent span's output
holds the deliverable. When filters are configured, check the whole file for
both kept and withheld requests.

Your test runs can set extra variables, but the user's runs will not have
them. Before you report, run the representative command once with only the
project's own environment and `CONVERGENT_SPANS_DIR`. Do not add
`CONVERGENT_RELEASE`, `CONVERGENT_STRICT`, or any other variable. If that run
records nothing, it is an unresolved issue.

Fix only issues that the source and the recording support. Rerun after each
related change, and inspect the new recording. Stop when the expected recording
is present, or when a problem outside instrumentation blocks progress.
Repeating an unchanged run without new evidence is not a fix.

## Report

Report the edited files, the exact run command, every environment variable the
user must set for traces to be sent, the package versions, the docs tag you
read, the recording path, the trace ID, and the observed spans. List
the recorded files by span, input or output, and media type, and say whether a
recording with a key or a hosted trace verified them. List unexecuted paths and
unresolved findings. Keep local instrumentation, real provider calls, and
hosted delivery apart.

For a user-authorized hosted check, call `convergent.check()` in Python, or
await `check()` in TypeScript, in the initialized process after the run and the
flush. Keep its result. Then inspect the trace in the workspace before you say
it is visible there. A local recording or a successful flush proves neither
ingestion nor display.
