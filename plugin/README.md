# convergent-instrument plugin

A Claude Code plugin that carries the [instrument skill](../skills/instrument/)
and enforces its verification loop. With the plugin installed, "instrument my
agent" ends only when the recording passes its checks: a Stop hook runs the
skill's checker over the recorded spans and blocks completion until a pass
ends clean, a bound is hit, or you cancel.

## Install

```
/plugin marketplace add convergent-technologies/convergent-sdk
/plugin install convergent-instrument@convergent-sdk
```

The skill also works without the plugin: clone the repository and copy
`skills/instrument/` into your project's skills directory. That tier has the
same instructions and no enforcement; the verification loop is written into
the skill and Claude follows it itself.

## What the hooks do

Be aware before installing: this plugin blocks completion. Its hooks are:

- **SessionStart** fetches the current skill content (instructions and
  criteria, listed in `skills/instrument/manifest.json`) from this
  repository's `stable` ref, with one conditional request and a three second
  timeout. Offline or on any failure it keeps the last cached copy, and before
  any fetch it has the snapshot built into the plugin. The manifest names
  markdown only, and no fetched file is ever executed.
- **Stop** is the enforced loop. It is inert until the skill arms it by
  writing `.convergent-instrument/state.json` in your project. Armed, it runs
  the checker over the spans file that state names and blocks the stop with
  the findings and one instruction: dispatch the `convergent-instrument:verifier`
  subagent and route its verdicts. It releases, loudly marked NOT verified,
  after five passes, after three stops that show the same findings over the
  same recording, or when the checker itself cannot run.
- **SubagentStop** attests the verifier's verdict. When a subagent finishes and
  the event names it as this plugin's verifier, the hook reads the
  `findings-<n>.json` file the verifier wrote and records the verdict in
  `.convergent-instrument/verdict.json`, together with the id of the current
  arming and a digest of the recorded spans as they stand at that moment.
- **PostToolUse** on Edit and Write reruns the checker between stops, at most
  every 30 seconds, so failures reach Claude next to the edit that should fix
  them.

## What is enforced and what is asked for

The Stop hook releases the session on two conditions together. The checker has
to exit clean over the spans file, and `verdict.json` has to carry a clean
verdict for the current arming whose spans digest matches the recording on
disk. The digest is what ties a verdict to what it judged: a verdict recorded
over an earlier recording does not release a later one, so a re-record after a
clean pass sends the loop around again, and the verdict needs no other scoping
to the armed cycle.

Both halves are machine-written. The checker is a script the hook runs, and the
verdict is written by the SubagentStop hook from the verifier's findings file.
Claude cannot release the gate by writing a verdict line into the ledger, which
is what an earlier version of this plugin accepted.

This raises the cost of a faked verdict rather than removing the possibility.
An agent with shell access can write any file in the project, including
`verdict.json`, and nothing here signs it or puts it out of reach. What changed
is that finishing without dispatching the verifier is no longer the path of
least resistance: it takes a deliberate act aimed at the gate, rather than one
more line appended to a file the agent already writes.

Where the environment cannot spawn a subagent at all, no attestation is
written. The gate does not hold the session open forever for that. It releases
at its existing bounds, writes a line in the ledger saying the release was
unverified and whether a verifier attested anything, and tells Claude to say
the same in its final report.

## Every way this ends without a verified recording

Enforcement is real, and it is not a trap. These all end a session with the
recording unverified:

- `/convergent-instrument:cancel` disarms the loop for the current project and
  leaves the plugin installed.
- `/convergent-instrument:waive <criteria> <why>` leaves one finding open on
  purpose. The gate stops holding for that finding at the next stop rather than
  waiting for another verifier pass to read the ledger, and every release names
  each waiver it applied with the reason given. Add `--match "<substring>"` where
  one criteria raised several findings and only one is being waived. A release
  carrying waivers says so and is not a clean verification.
- `/plugin disable convergent-instrument` turns the whole plugin off, and
  uninstalling it does the same.
- `python3 scripts/disarm.py` disarms the loop from a shell.
- Taking the bare skill instead of the plugin. `skills/instrument/` copied into
  a project has the same instructions and no hooks at all.
- Deleting `.convergent-instrument/state.json`, or clearing its `spans` key,
  disarms it: with no spans path to check, the gate cannot run and takes the
  fail-open path below.
- The fail-open path itself. Where the checker cannot run at all, the gate
  disarms, says so, and allows the stop. A missing checker, a checker that
  times out, and a state file naming no spans path all land here.
- The bounds. Five blocked passes, or three stops showing the same findings
  over the same recording, release the stop.
- An environment that cannot spawn a subagent. Nothing attests a verdict there,
  so the loop runs to one of the bounds above and the release says the
  verification was self-run.

Every one of these prints a message saying the recording was NOT verified. None
of them is silent.

## Layout

- `hooks/` — the four hooks above; Python, standard library only.
- `agents/verifier.md` — the verifier subagent. It reads evidence and reports;
  it has no edit tools, and the one file it writes is its findings file.
- `commands/cancel.md` — the cancel command.
- `commands/waive.md` and `scripts/waive.py` — the waive command. Both are
  user-invoked only (`disable-model-invocation`), and the waiver they record is
  named in every release that applies it.
- `skills/instrument/SKILL.md` — a loader: the skill's instructions are read
  from the freshest content copy at use time.
- `scripts/run_checker.py` — the checker entry point. It is not a fork of the
  skill's `scripts/show_spans.py`: it invokes the copy under
  `content-snapshot/`, inside this plugin.
- `content-snapshot/` — the whole skill directory as of this plugin build. The
  instructions here are the fallback until a fetch succeeds; the code here is
  what runs, always.

Code ships in this versioned plugin; instructions and criteria are content,
refreshed from the repository. The two are kept apart on purpose. The content
manifest names markdown and nothing else, and the hooks resolve an executable
only inside the plugin, so whoever can move the published content ref cannot
make this plugin run code. The consequence is that the checker's behavior
changes only when a new plugin version is released: the content manifest's
`requires-plugin` floor is what keeps newer instructions from assuming a newer
checker, and the plugin warns at session start when its own version falls below
that floor.

## Future work

A `type: "agent"` Stop hook could judge the recording directly instead of
blocking with an instruction to dispatch the verifier. This version keeps the
command hook, which is deterministic and inspectable.
