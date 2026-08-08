---
name: agent-identity-stability
description: One product carries one agent name, and the name is the same on every run.
---

# Agent identity stability

The workspace keys an agent's history, rates, and comparisons to its name.
These clauses judge whether the recorded names can carry a history.

## One product, one name

The recording carries one agent name for the product, and every run of the
product uses that name. A sub-agent the plan lists as its own agent carries its
own name, held to the same standard.

## The name holds still

An agent name carrying a user id, a run id, a session id, a timestamp, or any
other value that changes between runs or users is a violation. Every run under
such a name opens a new history in the workspace, so nothing accumulates.
`support-agent` holds still; `support-agent-2026-08-07`, `support-agent-user-49312`,
and `support-agent-a41f9c` do not.

## A first-seen name against a similar existing one

Where the server reports the agents linked for this release and the recording
carries a name the server has never seen, while the server lists a similar
name, the finding is a question for the user: a new name opens a new history,
and a misspelling of an existing name looks exactly like a new agent. Classify
it `ask`.
