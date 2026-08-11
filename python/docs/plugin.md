---
title: Enforce the verification loop with a plugin
description: Install the Claude Code plugin that holds a session open until the recorded spans pass their checks.
---

The [instrument skill](agent-skill.md) tells a coding agent how to add Convergent
tracing and asks it to verify the result. The plugin makes that last step
enforced: with it installed, "instrument my agent" ends only when the recording
passes its checks, because a hook blocks the agent from finishing.

The skill works on its own. The plugin adds enforcement and nothing else, so
install it when you want the verification to be a gate rather than a request.

## Install it

```
/plugin marketplace add convergent-technologies/convergent-sdk
/plugin install convergent-instrument@convergent-sdk
```

Restart the session afterwards. The plugin fetches the current skill
instructions at session start, so the session that installs it is still running
the copy bundled at build time.

## What it enforces

The loop is inert until the skill arms it, which it does in step 1 by writing
`.convergent-instrument/state.json` in your project. Once armed, every attempt to
finish runs two checks, and both have to pass:

- The **checker** runs over the spans the run recorded and exits clean. It reads
  the same coverage plan the skill wrote, so a model call or tool the plan named
  and the recording missed is a failure.
- An **independent verifier** subagent judges the recording against written
  criteria and reports zero open findings. It has no edit tools, and the plugin
  records its verdict from the findings file it leaves behind rather than from
  anything the agent says, so the agent being gated cannot release itself.

Findings come back classified. `fix` means the agent should change the
instrumentation. `ask` means the decision is yours: a first-seen agent name, a
span from a library you may or may not want recorded, whether prompts and
completions are captured at all.

## Commands

- `/convergent-instrument:waive <criteria> <why>` leaves one finding open on
  purpose. The gate stops holding for it at the next stop, and every release
  names each waiver with the reason you gave. Add `--match "<substring>"` where
  one criteria raised several findings and you are waiving one.
- `/convergent-instrument:cancel` disarms the loop for the project and leaves the
  plugin installed.

## Turning it off

Enforcement is bounded and it is not a trap. The gate releases after five
blocked attempts, after two attempts that show the same findings over an
unchanged recording, and whenever the checker cannot run at all. Cancelling,
disabling the plugin, uninstalling it, or deleting the state file all end it too.

Every one of those paths prints a message saying the recording was not verified
and what is still open. None of them is silent, and none of them leaves you
unable to finish a session.

## What is fetched and what is not

The instructions and criteria the agent reads are content, fetched from this
repository at session start and cached, so wording can change without a plugin
release. Executables are never fetched: the checker runs from the copy inside the
installed plugin, which is why its behavior changes only when a new plugin
version ships.
