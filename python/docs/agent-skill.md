---
title: Instrument with a coding agent
description: Instrument your agent with one prompt to Claude Code, Codex, or another coding agent.
---

The SDK includes two skills for coding agents. `convergent-instrument` adds
tracing to your agent, and `convergent-verify` checks each recording without
changing code.

## Instrument your agent

From the root of your repository, give a coding agent this prompt. Fill in the
last two lines with the agent(s) you wish to instrument and a representative
command to run them.

```text
Install the convergent-instrument and convergent-verify skills from the github repository convergent-technologies/convergent-sdk.

convergent-instrument is a skill to instrument your agent, and convergent-verify is a skill to inspect each recording. You will be given the agent file or directory to instrument, as well as the instructions or command that runs the agent for the representative run. Use both skills to instrument and verify your agent. Continue using both skills until the recording has no unresolved instrumentation issue.

Agent to instrument: <file or directory>
Representative run: <command or instructions that run the agent>
```

The coding agent installs the skills into `.claude/skills` (Claude Code) or
`.agents/skills` (Codex). It then:

1. Maps the agent, model, tool, and subagent calls in your code.
2. Decides what the recording should look like, and asks you about any content
   it is unsure whether to record.
3. Adds the smallest instrumentation that works.
4. Runs your command and records the result locally.
5. Checks the recording with `convergent-verify` and fixes any issue it finds.
6. Repeats until the recording looks as expected.

It asks you when it needs credentials or permission. It stops when a problem
outside instrumentation blocks it.

## Check your instrumentation

After you change your agent, or if you added instrumentation yourself, ask a
coding agent to check it. The check runs your agent once and reads the trace it
records. It does not change any code. Fill in the last two lines of the prompt
with the agent you wish to check and a representative command to run it.

```text
Install the convergent-verify skill from the github repository convergent-technologies/convergent-sdk.

Run the representative command below with CONVERGENT_SPANS_DIR set to a temporary directory. Then use convergent-verify to check the spans.jsonl file it writes. Report what you find. Do not change any code.

Agent to check: <file or directory>
Representative run: <command or instructions that run the agent>
```

The coding agent reports each finding as an `issue` (something to fix), a
`question` (something it needs you to decide), or an `fyi`.
