# Instrument your agent

The Instrument your agent page covers the four calls that open spans: `agent()`
for one run of one agent, `tool()` for one tool call, and `span()` and
`observe()` for everything else. It names the three operations the workspace
renders as their own kind of step and says what becomes of any other one, shows
`set_input()` and `set_output()` recording what went in and out, and gives
`current_span()` a section of its own for reaching the span a decorator opened
with no variable. It also covers
`gen_ai.conversation.id` for linking the turns of a multi-turn conversation, and
`flush()` for a process whose exit skips the interpreter's exit hook.

Page: `python/docs/instrument.md` in this SDK's documentation tree.
