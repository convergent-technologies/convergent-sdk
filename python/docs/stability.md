---
title: Stability
description: What counts as public API, what a version number promises, and what to pin.
---

The SDK is on a 0.x version line. This page says what you can rely on across
upgrades, what a version bump may change, and what range to pin. The policy
applies from 0.0.5 on.

## What is public API

The public API is what the [API reference](reference/api.md) documents:

- The names `import convergent` exports. There are twenty of them,
  `__version__` through `Note`, and a test in the repository pins the exact
  list, so a release cannot drop or rename one silently.
- The `convergent.otel` submodule with `install()` and
  `ConvergentSpanProcessor`.
- The documented signatures of those calls: their keyword arguments, return
  types, and the exceptions the reference names.
- The environment variables listed in
  [Configuration](configuration.md#convergent-variables), `CONVERGENT_API_KEY`
  through `CONVERGENT_STRICT`.
- The file destination's format: one OTLP/JSON export request per line, which
  a test also holds in place.

## What a version number promises

Version numbers follow [Semantic Versioning](https://semver.org/#spec-item-4).
SemVer allows anything to change while the major version is 0, and this SDK
narrows that:

- A patch release (0.0.5 to 0.0.6) keeps the public API compatible. It fixes
  bugs and updates documentation.
- A minor release (0.0.x to 0.1.0) may change or remove public API. Every
  such change is named in the
  [changelog](https://github.com/convergent-technologies/convergent-sdk/blob/main/python/CHANGELOG.md)
  under the version that made it.
- Pre-releases such as `0.1.0rc1` carry no compatibility promise. pip only
  installs them when asked.

When the SDK reaches 1.0, the standard rules take over: breaking changes only
with a new major version. [pydantic's version
policy](https://docs.pydantic.dev/latest/version-policy/) is the shape this
policy grows into.

## Deprecations

When a public name is going to change or go away, the old name keeps working
for at least one more minor version. Calling it raises a `DeprecationWarning`
that names the replacement, and the changelog entry for the release says the
same. There are no deprecated names today.

## What to pin

```
convergent-sdk>=0.0.5,<0.1
```

The upper bound holds you on the 0.0.x line, where every release is
compatible. When 0.1.0 arrives, read its changelog entry and move the bound
up deliberately.

## What is not covered

- Anything with a leading underscore. Modules such as `convergent._core` are
  internal and may change in any release.
- The exact wording of log lines, console output, and the `check()` report.
  Read the structured values, and treat the text as for people.
- The HTTP details of the Convergent receiver the SDK talks to. Point the SDK
  at your own collector with `CONVERGENT_ENDPOINT` instead of imitating the
  receiver.
- The exact set of packages an install brings. The declared dependencies in
  the package metadata are the contract; their own dependencies move on their
  own schedules.
- The recorded attribute names follow the OpenTelemetry GenAI semantic
  conventions at the version the SDK pins, and those conventions are not yet
  stable upstream. When a pin bump changes an emitted attribute, the
  changelog names it.
