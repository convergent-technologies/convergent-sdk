---
name: trace-structure
description: A recorded run reads as one tree, with every call under the agent that made it and every fact on the span.
---

# Trace structure

Presence is not enough. A span that carries the right name and the wrong parent
loses the run it belongs to, and a span whose attributes read `MISSING` carries
a name and nothing computed. These clauses judge the shape of the recording
rather than its contents.

## Every model and tool call sits under an agent run

Every `chat` span and every `execute_tool` span has an `invoke_agent` span
somewhere above it in the same trace. A model or tool span arriving as its own
root, or under a parent chain that reaches no agent run, is a violation: the
call is stored with no agent attached, so it joins no agent's history. The
finding names the span and the parent it arrived under, and it is a `fix`,
usually a span opened outside the enclosing agent run rather than inside it.

## A run records more than its entry point

A trace holding an agent run span and nothing under it is a violation wherever
the plan names a model call or a tool for that run. One span per run supports
almost no comparison between runs, and it is what wrapping only the entry point
produces. The finding names the trace and the plan items that recorded nothing
under it.

## Every span carries the facts its kind is read for

An agent run carries `gen_ai.agent.name`. A model call carries
`gen_ai.request.model` and, where the provider reports them, the
`gen_ai.usage.` token counts. A tool call carries `gen_ai.tool.name` and
`gen_ai.tool.call.id`. The script prints `MISSING` in place of each of these
that the span does not carry, and every `MISSING` it prints is a violation
unless the ledger shows the user waived that fact. A missing tool call id is
what makes one tool call show as two rows in the workspace.

## Recorded durations match the work

A model span's duration covers the request it names. A span reading a few
milliseconds around a call that takes seconds was closed before the response
arrived, which is the shape of a span opened around the call rather than inside
the function making it. Compare the durations the script prints against what
the calls plausibly took, and where a model call has no token counts and a
duration far below the rest, report it as a violation rather than as a fast
call.
