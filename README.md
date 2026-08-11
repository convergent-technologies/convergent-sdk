# Convergent SDKs

Client SDKs for [Convergent](https://convergent.dev).

| Language | Package | Version | Docs |
| --- | --- | --- | --- |
| Python | `pip install convergent-sdk` | 0.0.5b1 | [python/README.md](python/README.md) |
| TypeScript / JavaScript | — | — | — |

## Instrument an agent with a coding agent

The repository also ships an agent skill and a Claude Code plugin that add
Convergent tracing to a codebase and check that the spans arrived.

| | What it is | Start here |
| --- | --- | --- |
| Skill | Instructions a coding agent follows to plan the coverage, wrap the calls, and verify the recording. Copy `skills/instrument/` into your project's `.claude/skills/`. | [python/docs/agent-skill.md](python/docs/agent-skill.md) |
| Plugin | The same loop with the verification enforced: a hook holds the session open until the recording passes its checks and an independent verifier reports no open findings. | [python/docs/plugin.md](python/docs/plugin.md) |

The skill works on its own. The plugin adds enforcement, and installs from this
repository as a marketplace:

```
/plugin marketplace add convergent-technologies/convergent-sdk
/plugin install convergent-instrument@convergent-sdk
```

`plugin/README.md` documents every hook, every command, and every way a session
ends without a verified recording.
