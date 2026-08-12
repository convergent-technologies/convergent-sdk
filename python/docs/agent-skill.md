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

## Install the skills

Use the Skills CLI to install both skills for your coding agent:

```bash
npx skills add convergent-technologies/convergent-sdk
```

Select `convergent-instrument` and `convergent-verify` when prompted.

You can also copy each directory from `skills/` into your agent's skills directory.
Keep both directory names unchanged.

The [SDK README](../README.md#instrument-with-a-coding-agent) has prompts for new and
existing setups.

## Instrument one agent

Ask the coding agent to add Convergent tracing to one Python agent.
Name the agent or source path when the repository contains several agents.
Name a representative run command when the repository does not make it clear.

The skill performs these actions:

1. It maps the reachable agent, model, tool, and subagent calls.
2. It defines the recording expected from the selected command.
3. It asks which recorded content to exclude.
4. It adds the smallest supported instrumentation.
5. It runs the command into a temporary spans directory.
6. It invokes `convergent-verify` on the recording.
7. It fixes evidence-backed instrumentation issues.
8. It repeats until the recording reaches the expected state.

The skill uses no retry limit.
It stops when no instrumentation change can advance the recording.
It asks for user action when credentials or permission are required.

## Verify a recording

Invoke `convergent-verify` by itself when a recording already exists.
Pass the spans path and agent name when known.
Pass the expected recording when one was defined before the run.

The skill renders one agent subtree.
It includes nested agents with different names.
It hides recorded content values by default.
It reports `issue`, `question`, and `fyi` findings.
It writes no files.
