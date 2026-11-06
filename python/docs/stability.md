---
title: Versioning
description: What counts as public API, what a version number promises, and what to pin.
---

The SDK is on a 0.x version line. This page covers what you can rely on across
upgrades, what a version bump may change, and which range to pin. The policy
applies from 0.0.5 on.

## What is public API

The public API is what the [API reference](reference/api.md) documents:

- The names `import convergent` exports, from `__version__` through
  `propagator`.
  A test in the repository pins the exact list, so a release cannot drop or
  rename one silently.
- The `convergent.otel` submodule with `install()` and
  `ConvergentSpanProcessor`.
- The `convergent.carrier` submodule with `inject()` and `extract()`.
- The `convergent.temporal` submodule with `TracingInterceptor`, installed with
  the `temporal` extra.
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
  bugs and updates documentation. The exceptions are the session context-key
  restriction in 0.0.9 and the changes in 0.0.12, both described below.
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
convergent-sdk>=0.0.12,<0.1
```

The upper bound keeps you on the 0.0.x line. Keep the exact version you test
in your lockfile, and read the release notes before you update it. `session()`
and the agent configuration arguments require 0.0.9 or newer. File
attachments (`input_artifact()` and `output_artifact()`) require 0.0.10 or
newer. `model=` outside an agent run requires 0.0.11 or newer.
`convergent.propagator()`, `convergent.carrier`, `init(session_hosts=...)`,
and `convergent.temporal` require 0.0.12 or newer.

## Upgrading to 0.0.12

Version 0.0.12 changes what some spans carry. These changes are an exception to
the patch-release compatibility policy:

- `Status.deployment` is removed, and spans no longer carry the deployment
  labels `init()` used to add. The release still rides every trace as
  `service.version` and `gen_ai.agent.version`. `init()` no longer registers
  a deployment, so it no longer waits on Convergent at startup, and it no
  longer sends `OTEL_SERVICE_NAME` as deployment registration metadata.
  It still maps `OTEL_SERVICE_NAME` to `service.name` on the spans it
  exports.
- Spans no longer carry `convergent.semantic.version` or
  `convergent.execution.id`, including the framework spans your own
  OpenTelemetry exporters receive. Convergent read neither one. If your own
  backend groups by `convergent.execution.id`, group by trace id instead,
  which held the same value. A `require_span_attributes` or
  `reject_span_attributes` rule naming either key no longer matches.
- `set_input()`, `set_output()`, and `tools=` drop a value that is not portable
  JSON instead of recording its `repr`, and the first one logs a warning. A
  pydantic model is recorded as `model_dump(mode="json")` gives it. Convert a
  `datetime`, a dataclass, or another object first, such as `when.isoformat()`.
  See
  [the span handle](reference/api.md#span) for the full rule.
- An empty or blank `model=` is no longer recorded as `gen_ai.request.model`,
  and the first one logs a warning.
- A span that ends with an exception or a cancellation no longer carries the
  output `set_output()` recorded on it. The input and the span's status are
  recorded as before. A decorated generator that its reader stops after a
  chunk still keeps its output, as in 0.0.11. An async one whose `finally`
  raises when the reader stops it now makes the reader's `aclose()` raise,
  marks the span as an error, and drops the output. If you read
  `gen_ai.output.messages` or `gen_ai.tool.call.result` from a failed span,
  record it under a key of your own with `set_attribute()`, such as
  `app.partial_output`. Convergent does not show that key as the span's
  output. See [the span handle](reference/api.md#span) for the full rule.

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
