# Sessions

Record two concurrent conversations with two turns each. The example uses the
SDK and OpenTelemetry only. It makes no model calls and needs no credentials.

From the public SDK repository root, with Python 3.12 or newer:

```bash
python -m pip install 'convergent-sdk>=0.0.9,<0.1'
env -u CONVERGENT_API_KEY -u CONVERGENT_REQUIRE_SPAN_ATTRIBUTES \
  -u CONVERGENT_REJECT_SPAN_ATTRIBUTES -u CONVERGENT_SPANS_DIR \
  python python/examples/sessions/main.py
python skills/convergent-verify/scripts/show_spans.py ./traces --agent support-agent
```

Use a fresh `traces` directory. The reader shows four agent runs in four traces.
Each run has a tool span and an OpenTelemetry child span. Two runs carry
`convergent.session.id=chat-1` and two carry `convergent.session.id=chat-2`.
The file has one additional span, `after sessions`, with no session ID.

See [session grouping](../../docs/instrument.md#link-the-turns-of-a-conversation)
for nested sessions, async tasks, and propagation between services.
