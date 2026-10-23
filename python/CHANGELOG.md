What changed in each version of `convergent-sdk`, newest first. The top
section can describe a version that is not on PyPI yet.

## 0.0.11

- `model=` records the model on any span, not only agent runs.
- Agent runs send the model as `gen_ai.request.model`, not `convergent.agent.model`.
- Works with OpenTelemetry 1.45.1, which fixes CVE-2026-114603.
- With OpenTelemetry 1.45.1, timed-out and HTTP 429 exports retry within the same flush timeout.

## 0.0.10

- Attach files to a span with `input_artifact()` and `output_artifact()` ([guide](https://github.com/convergent-technologies/convergent-sdk/blob/main/docs/attach-files.md)).
- `flush(uploads=True)` waits for attached files to finish uploading.
- New dependency: `zstandard`.

## 0.0.9

- Works with `opentelemetry-exporter-otlp-proto-http` 1.45 and later.
- Record an agent's model, instructions, and tools with `agent()`.
- Group the turns of one conversation with `session(session_id)`.
- Bundled coding-agent skills work without the Convergent CLI.
- Breaking: use `session()`, not `context_attributes`, for session ids ([upgrade](https://github.com/convergent-technologies/convergent-sdk/blob/main/python/docs/stability.md#upgrading-from-008)).
- Breaking: `session.id` is no longer renamed or copied to `convergent.session.id`.

## 0.0.8

- Tag a run with a per-request value by passing a function to `context_attributes=`.
- Tag a running span mid-request with `set_context_attributes()`.

## 0.0.7

- Spans started on another thread keep the run's `context_attributes`.

## 0.0.6

- `check()` shows the span filters in effect.

## 0.0.5

- Choose which spans are sent with `require_span_attributes=` and `reject_span_attributes=`.
- Tag a span and everything inside it with `context_attributes=`.
- Instrument and verify skills for coding agents.
- A LiteLLM integration guide.

## 0.0.5b1

- Pre-release for testing. No changes from 0.0.4.

## 0.0.4

- `flush()` reports batches the server refused as dropped.
- `convergent.__version__` reports the installed version.

## 0.0.3

- First public release.

## 0.0.2

- Early packaging release, replaced by 0.0.3.
