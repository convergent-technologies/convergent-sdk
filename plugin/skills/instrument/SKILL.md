---
name: instrument
description: Instrument a Python AI agent with Convergent tracing so its runs, model calls, and tool calls appear in Convergent. Use when the user asks to add Convergent, set up tracing or observability for an agent, see their agent's runs, debug a trace or agent missing from Convergent, or mentions convergent-sdk, convergent.init or convergent.agent.
---

# Instrument an agent with Convergent

The instructions for this skill are content, fetched fresh from the skill's
repository at session start and cached by this plugin. Read whichever of these
exists, in this order, and follow it from the top:

1. `${CLAUDE_PLUGIN_DATA}/content/SKILL.md`
2. `${CLAUDE_PLUGIN_ROOT}/content-snapshot/SKILL.md`

Two notes apply because this plugin is installed:

- Step 1's `.convergent-instrument/state.json` arms real enforcement here: the
  plugin's Stop hook runs step 4's checker and keeps the session open until a
  pass ends clean, the loop's bound is hit, or the user runs the cancel
  command.
- Step 4's verifier is available as the `convergent-instrument:verifier`
  subagent, and the checker runs with
  `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/run_checker.py" <spans>`, taking the
  same arguments the skill gives `scripts/show_spans.py`. The instructions
  above are fetched content; the checker is code and ships inside the plugin,
  so run it through that path rather than from the content directory.
