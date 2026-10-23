# convergent-sdk

The Convergent SDKs for Python and TypeScript. They record the runs of your AI
agents as OpenTelemetry spans and send them to your Convergent workspace.

## Start

| Language | Install | Start here |
| --- | --- | --- |
| Python | `pip install convergent-sdk` | [python/docs/index.md](python/docs/index.md) |
| TypeScript | `npm install convergent-sdk @opentelemetry/api` | [typescript/docs/index.md](typescript/docs/index.md) |

To let a coding agent such as Claude Code or Codex instrument your agent, give
it the prompt in [Instrument with a coding agent](docs/coding-agent.md).

## Where things are

| Folder | Holds |
| --- | --- |
| `docs/` | Guides for both languages: [recording by hand](docs/record-by-hand.md), [files](docs/attach-files.md), [span filters](docs/span-filters.md), [local files](docs/local-files.md), [attributes](docs/attributes.md), and [troubleshooting](docs/troubleshooting.md) |
| `python/docs/`, `typescript/docs/` | Each language's install, configuration, API reference, and OpenTelemetry pages |
| `skills/` | The `convergent-instrument` and `convergent-verify` skills for coding agents |
| `python/`, `typescript/` | The SDK source, tests, examples, and changelog |

[llms.txt](llms.txt) lists every docs file and says when to read it. The docs
folders are the source of truth for how the SDKs work. The
[Convergent docs site](https://app.convergent.dev/docs) shows the same files.
The skills hold only the steps a coding agent follows. For every SDK detail,
they send the agent to these docs, at the release tag of the installed SDK.

## Releases

Python releases are tagged `python-v<version>` (`v<version>` up to 0.0.11).
TypeScript releases are tagged `typescript-v<version>`. Each language's
`CHANGELOG.md` lists the changes.

[Contributing](CONTRIBUTING.md) · [Security](SECURITY.md) · [License](LICENSE)
