---
title: Instrument with a coding agent
description: Install the skill that has your coding agent add Convergent tracing to your codebase.
---

The SDK ships an agent skill that tells a coding agent how to instrument your
agent with Convergent: which calls to wrap, which instrumentation package to
install for the model client you already import, and how to prove a span
arrived.

## Install it

The skill is the `skills/instrument/` directory at the repository root. Copy it
into your own project.

```bash
cp -r skills/instrument /path/to/your/project/.claude/skills/instrument
```

Then ask the agent to add Convergent tracing to your agent. Claude Code and
Cursor both read skills from `.claude/skills/`. For another tool, put the
directory wherever that tool loads skills from.

## What the agent does

The skill is a four-step workflow.

1. **Plan the coverage.** Read the code and list, in one message, the entry point
   that handles a request end to end, every model call including retries, every
   tool, and every sub-agent. Ask you to strike anything you want left out, and
   raise the judgment calls: which function is the entry point, which id holds
   still across the turns of a conversation, whether a sub-agent should be its
   own agent, and whether a prompt or a tool argument must not be recorded.
2. **Configure.** Install the SDK with your project's own tool, call `init()`
   once at startup, and derive `release` from something the project already has.
   Never guess or commit a key.
3. **Instrument.** Prefer an instrumentation package over a hand-written span,
   because it hooks the client and catches the streaming and async paths a
   wrapper misses. The skill carries the package, the version to pin, and the
   instrumentor class for each model client, and the rules for the seams that go
   wrong: where a model span has to sit to see the token counts, how to reach a
   call inside a framework loop, and why two packages must never wrap the same
   call.
4. **Verify.** Run `check()`, then run the agent and confirm delivery with the
   strongest option the environment has: a read API, a local OTLP collector, or a
   spans file. Count the recorded tree against the plan from step 1 and report
   what is missing.

## What is in the directory

`SKILL.md` is the workflow above, and it is the whole of what the agent reads on
every run.

`scripts/show_spans.py` reads a spans file and prints the span count, the
operations, the agent names, the release, and the token usage, then draws each
run as a tree. `--expect-agents` and `--expect-tools` turn the plan from step 1
into a check that exits nonzero and names what is missing. It uses the standard
library only.

`references/` holds one short file per page of this documentation. Each says what
its page covers and where the page is, so the agent opens a page when it needs
one rather than reading all of them.
