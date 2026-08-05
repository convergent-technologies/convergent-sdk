"""What leaves the process when ``init(agents=...)`` names the agents we may see."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator

import pytest
from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags

import convergent
from convergent import _core, _egress, _registry, _transport


@pytest.fixture(autouse=True)
def reset_sdk() -> Iterator[None]:
    _reset_otel()
    _core._reset_for_tests()
    yield
    _core._reset_for_tests()
    _reset_otel()


@pytest.fixture
def start_sdk(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[..., InMemorySpanExporter]]:
    providers: list[TracerProvider] = []

    def start(*, batch: bool = False, **kwargs: object) -> InMemorySpanExporter:
        exporter = InMemorySpanExporter()
        monkeypatch.setattr(
            _registry,
            "post_json",
            lambda *a, **k: {"deployment_id": "dep_test", "is_new": True},
        )
        # Only a real BatchSpanProcessor carries the private queue state flush()
        # reads, so a test about flush() has to ask for one.
        processor = BatchSpanProcessor(exporter) if batch else SimpleSpanProcessor(exporter)
        monkeypatch.setattr(_transport, "build_processor", lambda **_: processor)
        convergent.init(
            api_key="test-key",  # pragma: allowlist secret
            endpoint="https://example.test",
            release="r1",
            **kwargs,  # type: ignore[arg-type]
        )
        provider = _core.active_provider()
        assert provider is not None
        providers.append(provider)
        return exporter

    yield start
    for provider in providers:
        provider.shutdown()


def _reset_otel() -> None:
    trace._TRACER_PROVIDER_SET_ONCE._done = False  # type: ignore[attr-defined]
    trace._TRACER_PROVIDER = None  # type: ignore[attr-defined]


def _names(exporter: InMemorySpanExporter) -> set[str]:
    return {span.name for span in exporter.get_finished_spans()}


def _remote_parent(trace_id: int = 0x1234, span_id: int = 0x5678) -> Context:
    """A context holding a parent that arrived over the wire, as an ASGI server
    would have extracted it from ``traceparent``."""
    parent = SpanContext(
        trace_id=trace_id, span_id=span_id, is_remote=True, trace_flags=TraceFlags(0x01)
    )
    return trace.set_span_in_context(NonRecordingSpan(parent))


def test_a_declared_agents_run_is_sent_including_a_plain_child(
    start_sdk: Callable[..., InMemorySpanExporter],
) -> None:
    """Work inside a declared run is sent by descent, even when the span itself
    says nothing about GenAI."""
    exporter = start_sdk(agents=["support-agent"])
    provider = _core.active_provider()
    assert provider is not None

    with convergent.span(name="support-agent", operation="agent_run"):
        with provider.get_tracer("their.orm").start_as_current_span("SELECT users"):
            pass

    assert _names(exporter) == {"invoke_agent support-agent", "SELECT users"}


def test_an_undeclared_agents_whole_subtree_is_dropped(
    start_sdk: Callable[..., InMemorySpanExporter],
) -> None:
    exporter = start_sdk(agents=["support-agent"])

    with convergent.span(name="billing-agent", operation="agent_run"):
        with convergent.span(name="gpt-4", operation="model_call"):
            pass

    assert _names(exporter) == set()


def test_without_agents_every_span_is_sent(
    start_sdk: Callable[..., InMemorySpanExporter],
) -> None:
    exporter = start_sdk()
    provider = _core.active_provider()
    assert provider is not None

    with convergent.span(name="billing-agent", operation="agent_run"):
        pass
    with provider.get_tracer("their.orm").start_as_current_span("SELECT users"):
        pass

    assert _names(exporter) == {"invoke_agent billing-agent", "SELECT users"}


def test_a_genai_span_under_a_remote_parent_is_sent(
    start_sdk: Callable[..., InMemorySpanExporter],
) -> None:
    """The second process in a two-process agent. Nothing marks the trace, so the
    span is judged on what it carries."""
    exporter = start_sdk(agents=["support-agent"])
    provider = _core.active_provider()
    assert provider is not None

    tracer = provider.get_tracer("their.framework")
    with tracer.start_as_current_span(
        "chat", context=_remote_parent(), attributes={"gen_ai.request.model": "gpt-4"}
    ):
        pass

    assert _names(exporter) == {"chat"}


def test_an_empty_declaration_drops_a_genai_span_under_a_remote_parent(
    start_sdk: Callable[..., InMemorySpanExporter],
) -> None:
    """agents=[] means nothing is sent. The remote-parent shape rule above must not
    reopen that: an empty declaration is a fail-closed egress control, and a span
    arriving under propagated context is still nobody's declared work."""
    exporter = start_sdk(agents=[])
    provider = _core.active_provider()
    assert provider is not None

    tracer = provider.get_tracer("their.framework")
    with tracer.start_as_current_span(
        "chat", context=_remote_parent(), attributes={"gen_ai.request.model": "gpt-4"}
    ):
        pass

    assert _names(exporter) == set()


def test_a_root_model_call_outside_a_declared_run_is_dropped(
    start_sdk: Callable[..., InMemorySpanExporter],
) -> None:
    """The attribute rule is for spans arriving from another process. A span that
    starts a trace here would name its agent if it had one, so letting a bare
    ``gen_ai.*`` attribute stand in would send work the caller never declared."""
    exporter = start_sdk(agents=["support-agent"])

    with convergent.span(name="gpt-4", operation="model_call") as handle:
        handle.set_input("what is my account number")

    assert _names(exporter) == set()


def test_a_root_span_from_a_model_library_is_dropped(
    start_sdk: Callable[..., InMemorySpanExporter],
) -> None:
    exporter = start_sdk(agents=["support-agent"])
    provider = _core.active_provider()
    assert provider is not None

    with provider.get_tracer("openinference.instrumentation.openai").start_as_current_span("chat"):
        pass

    assert _names(exporter) == set()


def test_declaring_no_agents_sends_nothing(
    start_sdk: Callable[..., InMemorySpanExporter],
) -> None:
    """``agents=[]`` is a declaration that matches no span, which is not the same
    as leaving ``agents`` out."""
    exporter = start_sdk(agents=[])
    provider = _core.active_provider()
    assert provider is not None

    with convergent.span(name="support-agent", operation="agent_run"):
        pass
    with convergent.span(name="gpt-4", operation="model_call"):
        pass
    with provider.get_tracer("their.orm").start_as_current_span("SELECT users"):
        pass

    assert _names(exporter) == set()


def test_a_tuple_of_names_is_a_declaration_too(
    start_sdk: Callable[..., InMemorySpanExporter],
) -> None:
    """Reading a tuple as "no declaration" would turn a privacy control off without
    the caller asking."""
    exporter = start_sdk(agents=("support-agent",))
    with convergent.span(name="support-agent", operation="agent_run"):
        pass
    with convergent.span(name="billing-agent", operation="agent_run"):
        pass

    assert _names(exporter) == {"invoke_agent support-agent"}


def test_a_scope_that_merely_starts_with_a_model_library_name_is_dropped(
    start_sdk: Callable[..., InMemorySpanExporter],
) -> None:
    """``litellm`` is a model library we keep. ``litellm_internal_proxy`` is not."""
    exporter = start_sdk(agents=["support-agent"])
    provider = _core.active_provider()
    assert provider is not None

    tracer = provider.get_tracer("litellm_internal_proxy")
    with tracer.start_as_current_span("proxy", context=_remote_parent()):
        pass

    assert _names(exporter) == set()


def test_a_plain_span_under_a_remote_parent_is_dropped(
    start_sdk: Callable[..., InMemorySpanExporter],
) -> None:
    """Fail closed. Nothing places this span in a declared agent's run, so it stays
    in the caller's process rather than being sent on the chance that it belongs."""
    exporter = start_sdk(agents=["support-agent"])
    provider = _core.active_provider()
    assert provider is not None

    tracer = provider.get_tracer("their.web.server")
    with tracer.start_as_current_span("GET /health", context=_remote_parent()):
        pass

    assert _names(exporter) == set()


def test_a_child_starting_after_its_parent_ended_is_dropped(
    start_sdk: Callable[..., InMemorySpanExporter],
) -> None:
    """A known edge, pinned so it stays a decision rather than a surprise. The
    parent's id leaves the table when the parent ends, so a child that starts
    afterwards has nothing to descend from."""
    exporter = start_sdk(agents=["support-agent"])
    provider = _core.active_provider()
    assert provider is not None

    with convergent.span(name="support-agent", operation="agent_run"):
        parent_context = trace.set_span_in_context(trace.get_current_span())

    with provider.get_tracer("their.orm").start_as_current_span(
        "late child", context=parent_context
    ):
        pass

    assert _names(exporter) == {"invoke_agent support-agent"}


def test_the_kept_table_is_bounded_and_warns_once(
    start_sdk: Callable[..., InMemorySpanExporter],
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A full table drops its oldest entry rather than refuse the newest, so a
    spike loses the spans least likely to still be open instead of every new one."""
    monkeypatch.setattr(_egress, "_KEPT_LIMIT", 2)
    exporter = start_sdk(agents=["support-agent"])
    provider = _core.active_provider()
    assert provider is not None
    tracer = provider.get_tracer("their.framework")

    with caplog.at_level(logging.WARNING, logger="convergent.sdk"):
        started = [
            tracer.start_span("run", attributes={"gen_ai.agent.name": "support-agent"})
            for _ in range(5)
        ]
        for span in started:
            span.end()

    full = [record for record in caplog.records if "unfinished spans" in record.message]
    assert len(full) == 1
    assert len(exporter.get_finished_spans()) == 2


def test_flush_reads_the_exporter_and_not_the_filter_in_front_of_it(
    start_sdk: Callable[..., InMemorySpanExporter],
) -> None:
    """``flush()`` reads OpenTelemetry's own queue state, and that lives on the
    exporter rather than on the filter wrapping it. A shut-down exporter has
    nothing buffered, so flushing it succeeded. Give ``flush()`` the filter instead
    and the shutdown is hidden, so a clean teardown reports a failure that did not
    happen."""
    start_sdk(agents=["support-agent"], batch=True)
    with convergent.span(name="support-agent", operation="agent_run"):
        pass
    assert convergent.flush().ok

    provider = _core.active_provider()
    assert provider is not None
    provider.shutdown()

    assert convergent.flush().ok, "a shut-down exporter is skipped, not reported failed"


def test_declared_agents_are_registered_and_the_server_answer_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: dict[str, object] = {}

    def capture(url: str, payload: dict[str, object], **_: object) -> dict[str, object]:
        sent.update(payload)
        return {"deployment_id": "dep_1", "is_new": True, "agents": ["support-agent"]}

    monkeypatch.setattr(_registry, "post_json", capture)
    monkeypatch.setattr(
        _transport, "build_processor", lambda **_: SimpleSpanProcessor(InMemorySpanExporter())
    )
    status = convergent.init(
        api_key="test-key",  # pragma: allowlist secret
        endpoint="https://example.test",
        release="r1",
        agents=["support-agent", "billing-agent"],
    )

    assert sent["agents"] == ["support-agent", "billing-agent"]
    assert status.agents == ["support-agent"], "the server says which names took"


def test_status_falls_back_to_the_declared_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A server that does not answer with a linked set is not the same as one that
    linked nothing, so the declared names stand."""
    monkeypatch.setattr(
        _registry, "post_json", lambda *a, **k: {"deployment_id": "dep_1", "is_new": True}
    )
    monkeypatch.setattr(
        _transport, "build_processor", lambda **_: SimpleSpanProcessor(InMemorySpanExporter())
    )
    status = convergent.init(
        api_key="test-key",  # pragma: allowlist secret
        endpoint="https://example.test",
        release="r1",
        agents=["support-agent"],
    )

    assert status.agents == ["support-agent"]


def test_the_filter_leaves_the_callers_own_exporters_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Attach mode. Their pipeline was there first and keeps receiving everything."""
    theirs = InMemorySpanExporter()
    ours = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(theirs))
    trace.set_tracer_provider(provider)

    monkeypatch.setattr(
        _registry, "post_json", lambda *a, **k: {"deployment_id": "dep_1", "is_new": True}
    )
    monkeypatch.setattr(_transport, "build_processor", lambda **_: SimpleSpanProcessor(ours))
    convergent.init(
        api_key="test-key",  # pragma: allowlist secret
        endpoint="https://example.test",
        release="r1",
        agents=["support-agent"],
    )

    with convergent.span(name="billing-agent", operation="agent_run"):
        pass

    assert _names(theirs) == {"invoke_agent billing-agent"}
    assert _names(ours) == set()
    provider.shutdown()


def test_a_span_with_no_context_is_dropped() -> None:
    """Fail closed at the last step too. A span the filter cannot key is not sent."""
    exporter = InMemorySpanExporter()
    span_filter = _egress.DeclaredAgentFilter(["support-agent"], [SimpleSpanProcessor(exporter)])
    span_filter.on_end(ReadableSpan(name="contextless", context=None))
    assert exporter.get_finished_spans() == ()
