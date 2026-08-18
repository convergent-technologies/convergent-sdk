---
name: convergent-instrument
description: Add Convergent tracing to one Python AI agent and prove one representative run produces a useful recording. Use when a user asks to instrument an AI agent, add Convergent or convergent-sdk, or fix a missing or incomplete Convergent recording. Discover the current SDK and telemetry setup, prove basic visibility, enrich the executed path, and verify the result.
---

# Instrument one Python agent

Instrument one agent.
Prove one representative run.
Keep the user's application structure intact.

## Use current docs

SDK and integration APIs change.
If the project does not pin `convergent-sdk`, install the latest release.
If the project pins an older `convergent-sdk`, tell the user before you rely on newer docs.
Read [references/python.md](references/python.md) before changing code.
Read only the sections that match the target agent.
When internet access exists, read the current docs first.
Read the installed `convergent-sdk` version and use its tag in the doc URLs.
Fetch `https://raw.githubusercontent.com/convergent-technologies/convergent-sdk/v<version>/python/docs/meta.json` to list the doc pages.
Fetch the matching page from `https://raw.githubusercontent.com/convergent-technologies/convergent-sdk/v<version>/python/docs/<page>.md`.
When the tag fetch returns 404, fetch the same paths from `main` instead.
An unreleased version carries no tag, so treat that 404 as normal.
Do not guess a page name.
When a fetched page and [references/python.md](references/python.md) disagree, trust the fetched page.
When a fetched page omits an API that [references/python.md](references/python.md) names, use the reference and the installed source.
When a fetch fails, use [references/python.md](references/python.md) and the installed package source, and do not retry.
Inspect the installed package version and source before using an API.
Use installed code for exact imports and signatures.
Do not invent an SDK API.

## Core rules

Report the current phase and next action in one short update.
Instrument one real agent entry point first.
Prove basic visibility before adding detail.
Preserve the application's existing telemetry provider.
Use one instrumentation package for each executed model client.
Preserve the project's version constraints.
Limit edits to the target call graph.
Do not change application control flow, response behavior, runtime, or deployment configuration.
Do not create an application abstraction only for tracing.
Require verification after code changes.
When no user can answer, choose the reversible option and record the decision in the report.
When you cannot invoke `convergent-verify`, run `scripts/show_spans.py` from its skill directory.

## Tag a run for filtering

A tag must reach the run and its child spans before you add a filter.
Pass `context_attributes={"<key>": value}` on the `span()` that opens the run.
When the tag value varies per request, open the run with `span()`, not a decorator.
A decorator's `context_attributes=` mapping is fixed when the module loads.
Do not tag a run with `set_attribute()`.
`set_attribute()` writes one span, so a filter orphans or leaks the children.
Prove each new or changed filter with the recipe in [references/python.md](references/python.md).

## Phase 0: Explore

Do a short read-only pass.

- Identify the target agent entry point.
- Identify one command that exercises a representative path.
- Identify the Python version and package manager.
- Identify the model client and agent framework.
- Identify where tools execute.
- Identify where subagent handoffs execute.
- Identify the release source.
- Inspect installed SDK and instrumentation package versions when present.
- Search for existing OpenTelemetry, Sentry, Datadog, Honeycomb, Traceloop, and LangSmith setup.
- Search for `CONVERGENT_API_KEY` by name only.

Trace local calls from the entry point.
Stop at the target agent's boundary.
Ignore clients that the target cannot reach.
Match the instrumentation package to the imports used by the executed model call.
Do not select the package from the repository root manifest.
Do not read or print secret values.

List the candidates when more than one agent entry point matches.
Ask the user to select one before editing.

Stop when the repository contains no Python agent.
Report the unsupported language.

Ask only when the next edit would be a guess.
Ask before a command spends money or changes shared state.
Ask for missing credentials or permission when the run requires them.
Reuse each answer for the rest of the session.

Before editing, send one confirmation message.
Name the target, command, existing tracing setup, intended files, and risk.
Name each planned instrumentation point as `file:line`.
Group more than ten tools by dispatch site.
State that validation records prompts, completions, tool arguments, and tool results by default.
Ask which content the user wants excluded.

## Phase 1: Prove basic visibility

Prove that one agent run writes a local recording.

Define the basic expected recording.
Require one target `agent_run`.
Require one executed model call under the target.
Require one connected tree.
Require the release.

Follow the matching configuration and integration sections in [references/python.md](references/python.md).
Use the project's existing package manager.
Install only the missing SDK and instrumentation packages.
Preserve the project's version constraints.
Reuse the existing tracer provider when one exists.
Create a new temporary spans directory.
Run validation with `CONVERGENT_STRICT=1`.
Keep credentials in the environment.

Prefer the supported package for the executed model client.
Write a model span only when no package covers that client.
Wrap the target `agent_run` once.
Let a supported framework own the agent span when it already opens one.

Show the directory path.
Run the approved command.
Invoke `convergent-verify` with the basic expected recording.

Check the executed entry point when no recording appears.
Check configuration and flush behavior when no file appears.
Check initialization order when the model span is absent.
Check the invoked client when a wrapper records nothing.
Check overlapping wrappers when spans appear twice.
Check provider ownership when spans form separate trees.

Do not add tool or subagent detail until the basic recording passes.
Stop and report the evidence when it does not support another tracing fix.

## Phase 2: Enrich the executed path

Turn the basic recording into a complete recording.

Use the basic recording and source code to define the final expected recording.
Name each model call that executed.
Name each tool that executed.
Name each subagent that executed.
State the required nesting.
State the conversation identifier when the agent has multiple turns.
State the required content fields.
State the release source.
Name each request a filter must keep and each request a filter must withhold.

Treat unexecuted source branches as outside this recording.
Wrap each executed tool once.
Instrument registry tools at their shared dispatch site.
Keep model spans open until the response arrives.
Keep streamed spans open until the stream ends.
Keep tool wrappers compatible with the original callable.
Keep agent names stable across runs.
Keep conversation identifiers stable across turns.

Run the approved command after each related set of changes.
Invoke `convergent-verify` with the final expected recording.
Compare the report with the final expected recording.

## Phase 3: Resolve findings

Fix each instrumentation issue supported by the evidence.
Rerun the approved command after each fix.
Inspect the new recording.
Continue while the evidence supports another instrumentation fix.

Use no pass limit.
Make a new fix before repeating a command with unchanged evidence.
Keep unrelated application behavior outside this loop.
When the user requests one change to working instrumentation, report other gaps as `fyi` and leave them unfixed.

Route an `issue` to an instrumentation fix.
Route a `question` to the user.
Leave an expected provider limitation as `fyi`.
Keep a dismissed finding closed for the rest of the session.

## End states

Return `complete` when the command writes the final expected recording.
Require `convergent-verify` to report no unresolved instrumentation issue.
Do not return `complete` after a filter change without a recording that proves both directions.
Call `convergent.check()` in the initialized process when the run uses an API key.
Print the report.
Remove the temporary check code after verification.
Keep the printed report in your final message.
Report local recording success separately from hosted delivery success.
Do not claim hosted delivery without a successful round trip and the target agent name.
Show the edited files and one reason for each file.
Show the command, expected recording, recording path, trace, and report.

Return `needs user` when one user action can continue the run.
Name the action.
Give the exact command and expected recording path.
Continue after the user responds.

Return `blocked` when a non-instrumentation failure prevents the run.
Show the command and relevant error.
Explain why tracing changes cannot fix it.

The user can stop the loop at any time.
