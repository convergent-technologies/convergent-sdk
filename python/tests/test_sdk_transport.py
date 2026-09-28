from __future__ import annotations

import asyncio
import gzip
import http.server
import logging
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any

import pytest
from opentelemetry import trace
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import (
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import convergent
from convergent import _core, _registry, _transport


@pytest.fixture(autouse=True)
def reset_sdk(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv("CONVERGENT_API_KEY", raising=False)
    monkeypatch.delenv("CONVERGENT_ENDPOINT", raising=False)
    stub_registration(monkeypatch)
    _reset_otel()
    _core._reset_for_tests()
    yield
    _core._reset_for_tests()
    _reset_otel()


def stub_registration(monkeypatch: pytest.MonkeyPatch, deployment_id: str = "dep_test") -> None:
    """Keep init()'s deployment POST off the network for every unit test."""
    monkeypatch.setattr(
        _registry, "post_json", lambda *a, **k: {"deployment_id": deployment_id, "is_new": True}
    )


def _reset_otel() -> None:
    trace_module = trace
    trace_module._TRACER_PROVIDER_SET_ONCE._done = False  # type: ignore[attr-defined]
    trace_module._TRACER_PROVIDER = None  # type: ignore[attr-defined]


def _trace_id(span: ReadableSpan) -> int:
    assert span.context is not None
    return span.context.trace_id


def _capability() -> Any:
    """Bind pydantic-ai to the one process provider, the way a caller now does."""
    from pydantic_ai.capabilities.instrumentation import Instrumentation
    from pydantic_ai.models.instrumented import InstrumentationSettings

    provider = _core.active_provider()
    assert provider is not None
    return Instrumentation(
        settings=InstrumentationSettings(tracer_provider=provider, include_content=True)
    )


def test_disabled_decorators_keep_sync_and_async_behavior() -> None:
    """No ``init()`` has claimed this process, so the decorators wrap nothing."""

    @convergent.observe(name="sync", operation="tool_call")
    def sync(value: int) -> int:
        return value

    @convergent.observe(name="async", operation="tool_call")
    async def async_(value: int) -> int:
        return value

    assert sync(7) == 7
    assert asyncio.run(async_(8)) == 8


def test_pydantic_spans_share_the_sdk_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pydantic_ai import Agent
    from pydantic_ai.models.test import TestModel

    exporter = InMemorySpanExporter()
    monkeypatch.setattr(
        _transport,
        "build_processor",
        lambda **_: SimpleSpanProcessor(exporter),
    )
    convergent.init(
        api_key="key",  # pragma: allowlist secret
        endpoint="https://example.test",
        release="r1",
    )
    Agent(TestModel(), name="demo", capabilities=[_capability()]).run_sync("hello")

    spans = exporter.get_finished_spans()
    attributes = [span.attributes for span in spans]
    assert all(item is not None for item in attributes)
    complete_attributes = [item for item in attributes if item is not None]
    assert {item["gen_ai.operation.name"] for item in complete_attributes} == {
        "chat",
        "invoke_agent",
    }
    assert len({item["convergent.execution.id"] for item in complete_attributes}) == 1
    agent_attributes = next(
        item for item in complete_attributes if item["gen_ai.operation.name"] == "invoke_agent"
    )
    assert agent_attributes["gen_ai.agent.name"] == "demo"
    trace_id = f"{_trace_id(spans[0]):032x}"
    assert {item["convergent.execution.id"] for item in complete_attributes} == {trace_id}


def test_observed_coordinator_and_instrumented_agent_share_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pydantic_ai import Agent
    from pydantic_ai.models.test import TestModel

    exporter = InMemorySpanExporter()
    monkeypatch.setattr(
        _transport,
        "build_processor",
        lambda **_: SimpleSpanProcessor(exporter),
    )
    convergent.init(
        api_key="key",  # pragma: allowlist secret
        endpoint="https://example.test",
        release="r1",
    )
    support = Agent(TestModel(), name="support-agent", capabilities=[_capability()])

    @convergent.observe(name="benchmark", operation="agent_run")
    def run() -> None:
        support.run_sync("hello")

    run()

    spans = exporter.get_finished_spans()
    semantic_spans = [
        span for span in spans if (span.attributes or {}).get("gen_ai.operation.name") is not None
    ]
    assert {_trace_id(span) for span in semantic_spans} == {_trace_id(semantic_spans[0])}
    assert {(span.attributes or {})["convergent.execution.id"] for span in semantic_spans} == {
        (semantic_spans[0].attributes or {})["convergent.execution.id"]
    }


def test_nested_agents_share_one_execution_and_get_the_release_stamped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two framework agents on the one process provider keep their own names,
    which pydantic-ai puts on the span, and both pick up the release the SDK was
    given because neither names a version of its own."""
    from pydantic_ai import Agent
    from pydantic_ai.models.test import TestModel

    exporter = InMemorySpanExporter()
    monkeypatch.setattr(
        _transport,
        "build_processor",
        lambda **_: SimpleSpanProcessor(exporter),
    )
    convergent.init(
        api_key="key",  # pragma: allowlist secret
        endpoint="https://example.test",
        release="2026.07.24",
    )

    standup = Agent(
        TestModel(),
        name="standup-update-agent",
        capabilities=[_capability()],
    )
    retro = Agent(
        TestModel(call_tools=["get_team_standup"]),
        name="weekly-retro-agent",
        capabilities=[_capability()],
    )

    @retro.tool_plain
    async def get_team_standup() -> str:
        return (await standup.run("write a stand-up")).output

    @convergent.observe(name="weekly-retro-agent", operation="agent_run")
    def run() -> None:
        retro.run_sync("write the weekly retro")

    run()

    spans = exporter.get_finished_spans()
    semantic_spans = [
        span for span in spans if (span.attributes or {}).get("gen_ai.operation.name") is not None
    ]
    assert len({_trace_id(span) for span in semantic_spans}) == 1
    assert len({(span.attributes or {})["convergent.execution.id"] for span in semantic_spans}) == 1
    agent_spans = [
        span
        for span in semantic_spans
        if (span.attributes or {})["gen_ai.operation.name"] == "invoke_agent"
    ]
    assert {(span.attributes or {})["gen_ai.agent.name"] for span in agent_spans} == {
        "standup-update-agent",
        "weekly-retro-agent",
    }
    assert {(span.attributes or {})["gen_ai.agent.version"] for span in agent_spans} == {
        "2026.07.24"
    }
    assert any(
        (span.attributes or {}).get("gen_ai.tool.name") == "get_team_standup"
        for span in semantic_spans
    )


def test_flush_drains_the_configured_exporter(monkeypatch: pytest.MonkeyPatch) -> None:
    exporter = InMemorySpanExporter()
    processor = SimpleSpanProcessor(exporter)
    force_flush = processor.force_flush
    calls: list[int] = []

    def record_flush(timeout_millis: int = 30_000) -> bool:
        calls.append(timeout_millis)
        return force_flush(timeout_millis)

    monkeypatch.setattr(processor, "force_flush", record_flush)
    monkeypatch.setattr(_transport, "build_processor", lambda **_: processor)
    convergent.init(
        api_key="key",  # pragma: allowlist secret
        endpoint="https://example.test",
        release="r1",
    )

    assert convergent.flush(timeout_ms=250).ok is True
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("outcome", "expected_ok", "expected_dropped"),
    [
        (SpanExportResult.SUCCESS, True, 0),
        (SpanExportResult.FAILURE, False, 1),
        (RuntimeError("the receiver closed the connection"), False, 1),
    ],
    ids=["accepted", "refused", "raised"],
)
def test_flush_reports_whether_the_export_delivered(
    outcome: SpanExportResult | Exception,
    expected_ok: bool,
    expected_dropped: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``force_flush`` answers True whatever the exporter said, so ``flush()`` must
    read the export result itself. An exporter that raises loses the same batch, and
    ``flush()`` still answers rather than raising."""

    class Exporter(SpanExporter):
        def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        def shutdown(self) -> None: ...

        def force_flush(self, timeout_millis: int = 30_000) -> bool:
            return True

    monkeypatch.setattr(
        _transport, "build_processor", lambda **_: _transport.batch_processor(Exporter())
    )
    convergent.init(
        api_key="key",  # pragma: allowlist secret
        endpoint="https://example.test",
        release="r1",
    )

    @convergent.observe(name="job", operation="agent_run")
    def work() -> None: ...

    work()
    result = convergent.flush(timeout_ms=2_000)

    assert result.ok is expected_ok
    assert result.dropped == expected_dropped


@contextmanager
def _collector(status: int) -> Iterator[_Collector]:
    collector = _Collector(status)
    try:
        yield collector
    finally:
        collector.close()


class _Collector:
    """A local receiver that answers every span batch with one status code."""

    def __init__(self, status: int) -> None:
        self.batches: list[tuple[str, dict[str, str], bytes]] = []
        batches = self.batches

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                batches.append((self.path, dict(self.headers), body))
                self.send_response(status)
                self.end_headers()

            def log_message(self, *_: object) -> None: ...

        self._server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        self.endpoint = f"http://127.0.0.1:{self._server.server_port}"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


def test_a_recorded_span_reaches_the_collector() -> None:
    with _collector(200) as collector:
        convergent.init(
            api_key="key",  # pragma: allowlist secret
            endpoint=collector.endpoint,
            release="r1",
        )

        @convergent.observe(name="job", operation="agent_run")
        def work() -> None: ...

        work()
        result = convergent.flush(timeout_ms=5_000)

    assert (result.ok, result.dropped) == (True, 0)
    ((path, headers, body),) = collector.batches
    assert path == "/v1/traces"
    assert headers["Authorization"] == "Bearer key"
    request = ExportTraceServiceRequest.FromString(gzip.decompress(body))
    assert [
        span.name
        for resource in request.resource_spans
        for scope in resource.scope_spans
        for span in scope.spans
    ] == ["invoke_agent job"]


@pytest.mark.parametrize("status", [401, 403])
def test_a_rejected_key_stops_sending_and_flush_reports_the_loss(
    status: int, caplog: pytest.LogCaptureFixture
) -> None:
    """The refused batch and every batch withheld after it land in ``dropped``,
    or ``assert flush().ok`` in CI passes on a setup that delivers nothing."""
    with _collector(status) as collector, caplog.at_level(logging.WARNING, logger="convergent.sdk"):
        convergent.init(
            api_key="key",  # pragma: allowlist secret
            endpoint=collector.endpoint,
            release="r1",
        )

        @convergent.observe(name="job", operation="agent_run")
        def work() -> None: ...

        work()
        refused = convergent.flush(timeout_ms=5_000)
        work()
        withheld = convergent.flush(timeout_ms=5_000)

    assert (refused.ok, refused.dropped) == (False, 1)
    assert (withheld.ok, withheld.dropped) == (False, 1)
    assert len(collector.batches) == 1, "nothing is sent after the key is rejected"
    assert caplog.text.count("rejected its credentials") == 1


def test_exporter_setup_failure_disables_tracing(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def fail(**_: object) -> None:
        raise ValueError("no exporter for you")

    monkeypatch.setattr(_transport, "build_processor", fail)
    with caplog.at_level(logging.WARNING, logger="convergent.sdk"):
        status = convergent.init(
            api_key="key",  # pragma: allowlist secret
            endpoint="https://receiver.invalid",
            release="r1",
        )
    assert (status.enabled, status.reason) == (False, "setup_failed")
    assert _core.snapshot().provider is None
    assert not isinstance(trace.get_tracer_provider(), TracerProvider)
    assert "disabled" in caplog.text


def test_init_identity_has_no_execution_resource_attribute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        _transport,
        "build_processor",
        lambda **_: SimpleSpanProcessor(InMemorySpanExporter()),
    )
    convergent.init(
        api_key="key",  # pragma: allowlist secret
        endpoint="https://example.test",
        release="a01dbef",
    )
    provider = _core.snapshot().provider
    assert provider is not None
    resource = provider.resource.attributes
    # Agent identity rides the span, so no Resource carries a name.
    assert "convergent.agent.name" not in resource
    assert resource["service.version"] == "a01dbef"
    assert "convergent.execution.id" not in resource


def test_the_exporter_compresses_by_default() -> None:
    """Agent spans carry whole conversations, and OpenTelemetry's own default is
    no compression, so leaving it unset sends every prompt at full size."""
    from opentelemetry.exporter.otlp.proto.http import Compression

    from convergent._transport import _compression

    assert _compression() is Compression.Gzip


def test_the_standard_compression_variable_still_decides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exporter reads the variable only when the argument is absent, and the
    argument cannot be absent and defaulted at once, so it is read here instead."""
    from opentelemetry.exporter.otlp.proto.http import Compression

    from convergent._transport import _compression

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_COMPRESSION", "none")
    assert _compression() is Compression.NoCompression

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_COMPRESSION", "gzip")
    assert _compression() is Compression.Gzip


def test_the_traces_specific_variable_wins_over_the_generic_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exporter's own resolver prefers it, so reading only the generic one
    would leave the documented off-switch not working for anyone who set this."""
    from opentelemetry.exporter.otlp.proto.http import Compression

    from convergent._transport import _compression

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_COMPRESSION", "gzip")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_COMPRESSION", "none")

    assert _compression() is Compression.NoCompression


def test_the_processor_hands_its_compression_to_the_exporter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resolving the compression and then not passing it leaves the exporter on
    OpenTelemetry's own default of none, which every other test here still
    passes for."""
    from opentelemetry.exporter.otlp.proto.http import Compression
    from opentelemetry.exporter.otlp.proto.http import trace_exporter as exporter_module

    from convergent._transport import build_processor

    seen: dict[str, Any] = {}

    class Recording:
        def __init__(self, **kwargs: Any) -> None:
            seen.update(kwargs)

        def shutdown(self) -> None: ...

    monkeypatch.setattr(exporter_module, "OTLPSpanExporter", Recording)
    build_processor(api_key="k", endpoint="https://example.test").shutdown()

    assert seen["compression"] is Compression.Gzip


def test_deflate_is_not_sent_even_though_the_exporter_supports_it(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The collector inflates gzip and nothing else, so honouring this setting
    would send every batch in an encoding it answers 400 to. Nothing retries past
    a 400 and only 401/403 disables the exporter, so it would be silent."""
    from opentelemetry.exporter.otlp.proto.http import Compression

    from convergent._transport import _compression

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_COMPRESSION", "deflate")
    with caplog.at_level(logging.WARNING, logger="convergent.sdk"):
        assert _compression() is Compression.Gzip
    assert any("deflate" in record.getMessage() for record in caplog.records)


def test_an_unknown_compression_setting_falls_back_to_gzip(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from opentelemetry.exporter.otlp.proto.http import Compression

    from convergent._transport import _compression

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_COMPRESSION", "brotli")
    with caplog.at_level(logging.WARNING, logger="convergent.sdk"):
        assert _compression() is Compression.Gzip
    assert any("brotli" in record.getMessage() for record in caplog.records)
