---
title: Instrument with a coding agent
description: Install two portable skills that add Convergent tracing and inspect one recording.
---

The SDK ships two portable agent skills.

`convergent-instrument` adds tracing to one Python agent.
It runs one representative command.
It works toward an expected recording.

`convergent-verify` reads one recording.
It reports evidence-backed findings.
It changes no code.

Each skill tells the agent to work from current sources.
`convergent-instrument` checks the installed SDK and preserves project pins.
Both skills include their workflow and local reference files. They can read
public documentation or a matching public SDK checkout for API details.
Local instrumentation and verification need no Convergent CLI or hosted login.

## Install the skills

Ask your coding agent to install the skills from the GitHub repository
`convergent-technologies/convergent-sdk`. The prompt on the
[Get started](index.md) page does this as its first step.

The skills install into the repository:

- Claude Code reads `.claude/skills`.
- Codex reads `.agents/skills`.

## Start the agent

Give the coding agent the instrument prompt from the [Get started](index.md)
page. It names the agent to instrument and one representative run, and it
tells the agent to use both skills until the recording is clean.

## Instrument one agent

Ask the coding agent to add Convergent tracing to one Python agent.
Name the agent or source path when the repository contains several agents.
Name a representative run command when the repository does not make it clear.

The skill performs these actions:

1. It maps the reachable agent, model, tool, and subagent calls.
2. It defines the recording expected from the selected command.
3. It explains which content will be recorded and resolves missing content decisions.
4. It adds the smallest supported instrumentation.
5. It runs the command into a temporary spans directory.
6. It invokes `convergent-verify` on the recording.
7. It fixes evidence-backed instrumentation issues.
8. It repeats until the recording reaches the expected state.

The skill stops when the recording matches expectations or a problem outside
instrumentation prevents progress. Each rerun follows an evidence-backed change.
It asks for user action when credentials or permission are required.

## Verify a recording

Invoke `convergent-verify` by itself when a recording already exists.
Pass the spans path and agent name when known.
Pass the expected recording when one was defined before the run.

The skill renders one agent subtree.
It includes nested agents with different names.
It hides recorded content values by default.
It reports `issue`, `question`, and `fyi` findings.
It leaves application code unchanged.
