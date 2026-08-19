# Changelog

Each released version has a `## <version>` section here, and the release
workflow publishes that section as the GitHub Release notes. The newest
section may describe a version whose tag does not exist yet.

## 0.0.7

- Every span now inherits its parent span's `context_attributes=` pairs. A
  span that starts on another thread, or from a context saved earlier, still
  carries the run's attributes. `require_span_attributes=` keeps a whole run
  and `reject_span_attributes=` withholds a whole run, library spans included.
- A span's own `context_attributes=` adds pairs and wins for a key both hold.
  Its descendants follow the override.
- The deployment registration warning names the endpoint's scheme and host, so
  an endpoint the environment changed is visible in one line.

## 0.0.6

- `Status` echoes the running filter policy in `require_span_attributes` and
  `reject_span_attributes`, normalized to attribute name → value list. The
  printed `check()` report shows the policy as one `filters` row, reject
  first. A wrong filter no longer hides behind `round trip ok`.

## 0.0.5

- `init(require_span_attributes=)` and `init(reject_span_attributes=)` filter
  what is sent by attribute value: a span goes to the destinations the SDK set
  up, Convergent and any `File` or `Console` alike, only when no
  `reject_span_attributes=` pair matches and every `require_span_attributes=`
  key holds one of its listed values. The filter reads each key from the
  stamped `convergent.attributes.<key>` mark first, then from the finished
  span's own attributes, then from the resource attributes. Under
  `require_span_attributes=`, a span that holds the key in no source is not
  sent; under `reject_span_attributes=` alone, it is. Matching is exact by
  type and case. A pair named in both directions logs an ERROR at startup, and
  `reject_span_attributes=` wins at runtime. `otel.install()` and
  `ConvergentSpanProcessor` take the same arguments and apply the same filter.
  `CONVERGENT_REQUIRE_SPAN_ATTRIBUTES` and `CONVERGENT_REJECT_SPAN_ATTRIBUTES`
  fill the two in from the environment, JSON-encoded, when the arguments are
  absent.
- `span()`, `observe()`, `agent()`, and `tool()` take `context_attributes=`,
  the per-request marking channel: the pairs land on that span and on every
  span started inside it, library spans included, as
  `convergent.attributes.<key>`, and stay in the process. Nothing is written
  to outbound requests. A mark overwrites no span attribute; a key named in
  both parameters keeps both values, and the filter reads the mark first. Each
  service marks and filters its own requests; automatic propagation across
  services is a planned follow-up.

## 0.0.5

Finalizes 0.0.5b1. The package's behavior is unchanged from 0.0.4; this
release carries the repository around it forward:

- The instrument and verify agent skills replace the Claude Code plugin. The
  skills live under `skills/` and cover the same instrument-then-verify loop
  the plugin ran.
- A litellm integration guide (`python/docs/integrations/litellm.md`) for
  litellm's OpenTelemetry callback, and attribute reference updates to match.
- Security reports go through GitHub's private vulnerability reporting form
  first (`SECURITY.md`); email stays as the alternative.
- Releases are cut by tag: pushing `v<version>` runs CI, checks the tag
  against `pyproject.toml`, publishes to PyPI with PEP 740 attestations, and
  posts the GitHub Release from this file.

## 0.0.5b1

A pre-release. The package is unchanged from 0.0.4. The version moves so the
repository's instrument skill and plugin can be tested against a published
wheel without disturbing the release people install by default. A plain
`pip install convergent-sdk` still resolves to 0.0.4; `pip install
convergent-sdk==0.0.5b1` or `--pre` installs this one.

## 0.0.4

- `flush()` reports batches the collector refused as unauthorized: the refused
  batch and every batch withheld after it turn `ok` off and count in `dropped`.
  They were previously reported as delivered.
- `convergent.__version__` reports the installed package's version.

## 0.0.3

The first public release of the tracing SDK, tagged `v0.0.3` on this
repository.

## 0.0.2

An early packaging cut, superseded by 0.0.3. It has no tag here.
