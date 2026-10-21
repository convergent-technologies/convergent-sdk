---
title: Versioning
description: What counts as public API, what a version number promises, and what to pin.
---

The SDK is on a 0.x version line. This page covers what you can rely on across
upgrades, what a version bump may change, and which range to pin. The policy
applies from 0.0.5 on.

## What is public API

The public API is what the [API reference](reference/api.md) documents:

- The names `import convergent` exports, from `__version__` through `Note`.
  A test in the repository pins the exact list, so a release cannot drop or
  rename one silently.
- The `convergent.otel` submodule with `install()` and
  `ConvergentSpanProcessor`.
- The documented signatures of those calls: their keyword arguments, return
  types, and the exceptions the reference names.
- The environment variables listed in
  [Configuration](configuration.md#convergent-variables), `CONVERGENT_API_KEY`
  through `CONVERGENT_STRICT`.
- The file destination's format: one OTLP/JSON export request per line. A test
  also pins this format.

## What a version number promises

Version numbers follow [Semantic Versioning](https://semver.org/#spec-item-4).
SemVer allows any change while the major version is 0. This SDK makes narrower
promises:

- A patch release (0.0.5 to 0.0.6) keeps the public API compatible. It fixes
  bugs and updates documentation. The one exception is the session context-key
  restriction in 0.0.9, described below.
- A minor release (0.0.x to 0.1.0) may change or remove public API. The
  [changelog](https://github.com/convergent-technologies/convergent-sdk/blob/main/python/CHANGELOG.md)
  names every such change under the version that made it.
- Pre-releases such as `0.1.0rc1` carry no compatibility promise. pip installs
  them only when asked.

At 1.0, the standard rules apply: breaking changes come only with a new major
version. The policy will then follow the shape of
[pydantic's version policy](https://docs.pydantic.dev/latest/version-policy/).

## Deprecations

When a public name is going to change or go away, the old name keeps working
for at least one more minor version. Calling it raises a `DeprecationWarning`
that names the replacement, and the release's changelog entry says the same.
No names are deprecated today.

## What to pin

```text
convergent-sdk>=0.0.11,<0.1
```

The upper bound keeps you on the 0.0.x line. Keep the exact version you test
in your lockfile, and read the release notes before you update it. `session()`
and the agent configuration arguments require 0.0.9 or newer. File
attachments (`input_artifact()` and `output_artifact()`) require 0.0.10 or
newer. `model=` outside an agent run requires 0.0.11 or newer.

## Upgrading from 0.0.8

Version 0.0.9 rejects `session.id`, `gen_ai.conversation.id`, and
`convergent.session.id` in `context_attributes=` and
`set_context_attributes()`. It drops the entry and logs an error once per
reason. This is an exception to the patch-release compatibility policy.

If you filter on a session context key, rename the context key and its
matching `require_span_attributes` or `reject_span_attributes` filter together.
Use `session()` separately to group turns:

| Setting | 0.0.8 | 0.0.9 |
| --- | --- | --- |
| Required filter | `{"session.id": "chat-42"}` | `{"app.conversation_id": "chat-42"}` |
| Context attribute | `{"session.id": "chat-42"}` | `{"app.conversation_id": "chat-42"}` |
| Grouping | A session attribute on the span | `with convergent.session("chat-42"):` around each turn |

If a required key goes missing, the filter withholds spans. If a rejected key
goes missing, it stops matching, and the filter can let through spans it used
to withhold. Other span or resource attributes can still satisfy a filter.
After you migrate, record one request that should pass and one that should be
withheld, and inspect the complete recording.

In 0.0.9, direct `session.id` and `gen_ai.conversation.id` span attributes keep
their original keys and values. The SDK does not rename them or add a
`convergent.session.id` alias. Update any readers that relied on the rewritten
keys. The service accepts all three spellings. See
[session()](reference/api.md#session) for
the precedence rules.

## What is not covered

- Anything with a leading underscore. Modules such as `convergent._core` are
  internal and may change in any release.
- The exact wording of log lines, console output, and the `check()` report.
  Read the structured values. The text is for people.
- The HTTP details of the Convergent receiver. To use your own collector,
  point the SDK at it with `CONVERGENT_ENDPOINT` instead of imitating the
  receiver.
- The exact set of packages an install brings. The contract is the declared
  dependencies in the package metadata. Their own dependencies change on their
  own schedules.
- The recorded attribute names. They follow the OpenTelemetry GenAI semantic
  conventions at the version the SDK pins, and those conventions are not yet
  stable upstream. When a pin bump changes an emitted attribute, the changelog
  names it.
