"""``ConvergentSpanProcessor`` on a tracer provider the SDK never created."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from typing import Any, NamedTuple

import pytest
from opentelemetry import trace
from opentelemetry.instrumentation.utils import is_instrumentation_enabled
from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import convergent
import convergent.otel
from convergent.otel import ConvergentSpanProcessor
from convergent import _core, _otel, _processors, _registry, _transport

KEY = "test-key"  # pragma: allowlist secret
ENDPOINT = "https://example.test"
DEPLOYMENTS = f"{ENDPOINT}/v1/deployments"


class Installed(NamedTuple):
    provider: TracerProvider
    processor: ConvergentSpanProcessor


@pytest.fixture(autouse=True)
def reset_sdk() -> Iterator[None]:
    _reset_otel()
    _core._reset_for_tests()
    yield
    # Registration runs on a thread of its own, so a test that ends while one is in
    # flight would otherwise leave it to land inside the next test, against that
    # test's monkeypatched registry.
    for processor in list(_otel._instances):
        _join_registration(processor)
    _core._reset_for_tests()
    _reset_otel()


@pytest.fixture
def install() -> Iterator[Callable[..., Installed]]:
    """Build the provider a caller owns, with only our processor on it."""
    built: list[Installed] = []

    def build(*, globally: bool = True, **kwargs: Any) -> Installed:
        provider = TracerProvider()
        processor = convergent.otel.install(
            provider, api_key=KEY, endpoint=ENDPOINT, release="r1", **kwargs
        )
        if globally:
            trace.set_tracer_provider(provider)
        installed = Installed(provider, processor)
        built.append(installed)
        return installed

    yield build
    for installed in built:
        installed.provider.shutdown()


def _reset_otel() -> None:
    trace._TRACER_PROVIDER_SET_ONCE._done = False  # type: ignore[attr-defined]
    trace._TRACER_PROVIDER = None  # type: ignore[attr-defined]


def _join_registration(processor: ConvergentSpanProcessor, timeout: float = 5.0) -> None:
    thread = processor._registration
    if thread is not None:
        thread.join(timeout=timeout)


def _settle(processor: ConvergentSpanProcessor) -> None:
    """Register the deployment and wait for it, the way the first span starts it."""
    processor._start_registration()
    _join_registration(processor)
    assert processor._registered, "registration did not land"


def _serve(
    monkeypatch: pytest.MonkeyPatch, exporter: InMemorySpanExporter, *, batch: bool = False
) -> None:
    """Answer registration, and send every export to ``exporter``."""
    monkeypatch.setattr(
        _registry, "post_json", lambda *a, **k: {"deployment_id": "dep_test", "is_new": True}
    )
    # Only a real BatchSpanProcessor carries the private queue state flush() reads.
    processor = BatchSpanProcessor(exporter) if batch else SimpleSpanProcessor(exporter)
    monkeypatch.setattr(_transport, "build_processor", lambda **_: processor)


def _record(span: ReadableSpan) -> tuple[str, Mapping[str, object]]:
    """One span as the pair the parity test compares.

    ``convergent.execution.id`` is the span's own trace id, so two runs of the same
    code never agree on its value. It is replaced by whether it holds that trace id,
    which is the property the stamp is there for.
    """
    attributes: dict[str, object] = dict(span.attributes or {})
    context = span.get_span_context()
    attributes["convergent.execution.id"] = (
        context is not None
        and attributes.get("convergent.execution.id") == f"{context.trace_id:032x}"
    )
    return span.name, attributes


def _run_one_agent() -> None:
    with convergent.span(name="support-agent", operation="agent_run") as run:
        run.set_input("where is my order")
        with convergent.span(name="gpt", operation="model_call") as call:
            call.set_attribute("gen_ai.request.model", "gpt-4")
            call.set_output("it shipped")


def _names(exporter: InMemorySpanExporter) -> set[str]:
    return {span.name for span in exporter.get_finished_spans()}


@contextmanager
def _marked(pairs: Mapping[str, str]) -> Iterator[None]:
    """The context carrier around a block, the way ``span(context_attributes=)``
    attaches it, for library spans no ``span()`` call wraps."""
    token = _processors.attach_context(pairs)
    try:
        yield
    finally:
        _processors.detach_context(token)


# --- the acceptance bar ------------------------------------------------------


def test_install_defaults_to_the_managed_ingest_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CONVERGENT_ENDPOINT", raising=False)
    configured: dict[str, str] = {}
    exporter = InMemorySpanExporter()

    def build_processor(*, api_key: str, endpoint: str) -> SpanProcessor:
        configured.update(api_key=api_key, endpoint=endpoint)
        return SimpleSpanProcessor(exporter)

    monkeypatch.setattr(_transport, "build_processor", build_processor)
    provider = TracerProvider()
    convergent.otel.install(provider, api_key=KEY, release="r1")

    assert configured["endpoint"] == "https://ingest.convergent.dev"
    provider.shutdown()


def test_a_caller_owned_provider_produces_the_same_spans_as_init(
    monkeypatch: pytest.MonkeyPatch, install: Callable[..., Installed]
) -> None:
    """The acceptance test. Same names, same stamps, same values on both paths."""
    through_init = InMemorySpanExporter()
    _serve(monkeypatch, through_init)
    convergent.init(api_key=KEY, endpoint=ENDPOINT, release="r1")
    _run_one_agent()
    expected = [_record(span) for span in through_init.get_finished_spans()]

    _core._reset_for_tests()
    _reset_otel()

    through_processor = InMemorySpanExporter()
    _serve(monkeypatch, through_processor)
    _settle(install().processor)
    _run_one_agent()

    assert expected, "the init() path recorded nothing, so the comparison proves nothing"
    assert expected[-1][1]["convergent.deployment.id"] == "dep_test"
    assert expected[-1][1]["convergent.semantic.version"] == "1"
    assert expected[-1][1]["convergent.execution.id"] is True
    assert [_record(span) for span in through_processor.get_finished_spans()] == expected


def test_declared_agents_filter_through_the_processor(
    monkeypatch: pytest.MonkeyPatch, install: Callable[..., Installed]
) -> None:
    """The same fail-closed rules ``init(agents=...)`` applies. A plain child of a
    declared run is kept because it descends from one, and a root span that names
    nobody is dropped."""
    exporter = InMemorySpanExporter()
    _serve(monkeypatch, exporter)
    provider = install(agents=["support-agent"]).provider

    with convergent.span(name="support-agent", operation="agent_run"):
        with provider.get_tracer("their.orm").start_as_current_span("SELECT users"):
            pass
    with convergent.span(name="billing-agent", operation="agent_run"):
        pass
    with provider.get_tracer("their.web").start_as_current_span("GET /health"):
        pass

    assert _names(exporter) == {"invoke_agent support-agent", "SELECT users"}


def test_flush_drains_the_processor_and_reports_its_queue(
    monkeypatch: pytest.MonkeyPatch, install: Callable[..., Installed]
) -> None:
    """``flush()`` reaches a processor the caller added, and reads the queue state
    from the exporter rather than from the filter in front of it."""
    exporter = InMemorySpanExporter()
    _serve(monkeypatch, exporter, batch=True)
    install(agents=["support-agent"])

    with convergent.span(name="support-agent", operation="agent_run"):
        pass
    assert not exporter.get_finished_spans(), "a batch processor holds spans until flushed"

    result = convergent.flush()

    assert result.ok
    assert result.pending == 0
    assert result.dropped == 0
    assert _names(exporter) == {"invoke_agent support-agent"}


# --- registration stays off the span path ------------------------------------


def test_spans_before_registration_carry_the_fingerprint_and_later_ones_the_id(
    monkeypatch: pytest.MonkeyPatch, install: Callable[..., Installed]
) -> None:
    """No span waits for registration, so the early ones carry the release as a
    fingerprint. The server resolves that against the same key registration upserts
    on, so both kinds reach one deployment."""
    exporter = InMemorySpanExporter()
    answer = threading.Event()

    def wait_then_answer(*_: object, **__: object) -> dict[str, object]:
        answer.wait(timeout=5)
        return {"deployment_id": "dep_test", "is_new": True}

    monkeypatch.setattr(_registry, "post_json", wait_then_answer)
    monkeypatch.setattr(_transport, "build_processor", lambda **_: SimpleSpanProcessor(exporter))
    installed = install()

    with convergent.span(name="support-agent", operation="agent_run"):
        pass
    early = exporter.get_finished_spans()[-1].attributes or {}

    answer.set()
    _join_registration(installed.processor)

    with convergent.span(name="support-agent", operation="agent_run"):
        pass
    late = exporter.get_finished_spans()[-1].attributes or {}

    assert early["convergent.deployment.fingerprint"] == "r1"
    assert "convergent.deployment.id" not in early
    assert late["convergent.deployment.id"] == "dep_test"
    assert "convergent.deployment.fingerprint" not in late
    assert _core.live_status().deployment == "dep_test"


def test_concurrent_first_spans_do_not_wait_for_registration(
    monkeypatch: pytest.MonkeyPatch, install: Callable[..., Installed]
) -> None:
    """Holding a lock across the registration request made every concurrent span
    start queue behind it, and each one paid the whole request. Nothing on the span
    path may hold a lock across that call, which is the rule ``init()`` follows."""
    threads = 32
    exporter = InMemorySpanExporter()
    reached = threading.Event()
    answer = threading.Event()

    def slow(*_: object, **__: object) -> dict[str, object]:
        reached.set()
        answer.wait(timeout=5)
        return {"deployment_id": "dep_test", "is_new": True}

    monkeypatch.setattr(_registry, "post_json", slow)
    monkeypatch.setattr(_transport, "build_processor", lambda **_: SimpleSpanProcessor(exporter))
    install()

    barrier = threading.Barrier(threads)
    waited: list[float] = []

    def record() -> None:
        barrier.wait(timeout=5)
        began = time.monotonic()
        with convergent.span(name="support-agent", operation="agent_run"):
            pass
        waited.append(time.monotonic() - began)

    workers = [threading.Thread(target=record) for _ in range(threads)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=10)
    # The spans are already done, so this waits on the registration thread rather
    # than the other way round, which is the whole point.
    started_registration = reached.wait(timeout=5)
    answer.set()

    assert started_registration, "the first span started the registration"
    assert len(waited) == threads
    assert max(waited) < 0.5, f"a span waited {max(waited):.3f}s for registration"
    assert len(exporter.get_finished_spans()) == threads


def test_the_registration_request_records_no_span_and_runs_once(
    monkeypatch: pytest.MonkeyPatch, install: Callable[..., Installed]
) -> None:
    """A caller who traces their HTTP client must not get a span for our own
    registration request. Our processor would then be handed that span, which under
    ``agents=None`` means exporting a request that carries our Authorization header.
    The request runs with instrumentation suppressed, which is what every
    instrumentation library reads before it starts a span."""
    exporter = InMemorySpanExporter()
    posts: list[str] = []

    def instrumented_post(url: str, *_: object, **__: object) -> dict[str, object]:
        posts.append(url)
        if is_instrumentation_enabled():
            tracer = trace.get_tracer_provider().get_tracer("their.http")
            with tracer.start_as_current_span("POST /v1/deployments"):
                pass
        return {"deployment_id": "dep_test", "is_new": True}

    monkeypatch.setattr(_registry, "post_json", instrumented_post)
    monkeypatch.setattr(_transport, "build_processor", lambda **_: SimpleSpanProcessor(exporter))
    _settle(install().processor)

    with convergent.span(name="support-agent", operation="agent_run"):
        pass

    assert posts == [DEPLOYMENTS], "one registration, and no request about a request"
    assert _names(exporter) == {"invoke_agent support-agent"}


def test_a_failed_registration_never_raises_and_is_not_tried_again(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    install: Callable[..., Installed],
) -> None:
    """Construction reaches nobody, so it cannot fail on a box with no route out.
    A control plane that is down costs one request, not one per span, and every
    span keeps the fingerprint it already carried."""
    exporter = InMemorySpanExporter()
    posts: list[str] = []

    def refuse(url: str, *_: object, **__: object) -> dict[str, object]:
        posts.append(url)
        raise _registry.RegistrationError("URLError")

    monkeypatch.setattr(_registry, "post_json", refuse)
    monkeypatch.setattr(_transport, "build_processor", lambda **_: SimpleSpanProcessor(exporter))
    with caplog.at_level(logging.WARNING, logger="convergent.sdk"):
        installed = install()
        assert not caplog.records, "construction makes no network call"
        _run_one_agent()
        _join_registration(installed.processor)
        _run_one_agent()

    attributes = exporter.get_finished_spans()[-1].attributes or {}
    assert attributes["convergent.deployment.fingerprint"] == "r1"
    assert "convergent.deployment.id" not in attributes
    assert posts == [DEPLOYMENTS]
    assert len([r for r in caplog.records if "registration failed" in r.message]) == 1


# --- one configuration per process -------------------------------------------


def test_a_processor_added_after_init_sends_nothing_and_names_what_it_dropped(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """``init()`` keeps the first setup. A processor left armed would keep exporting
    on its own key, past the running configuration's ``agents=``, while ``Status``
    named only the running one."""
    theirs = InMemorySpanExporter()
    _serve(monkeypatch, theirs)
    convergent.init(api_key=KEY, endpoint=ENDPOINT, release="r1")
    drained = len(_core._drain)

    ours = InMemorySpanExporter()
    exporter = BatchSpanProcessor(ours)
    built: list[BatchSpanProcessor] = []
    monkeypatch.setattr(
        _transport, "build_processor", lambda **_: built.append(exporter) or exporter
    )
    provider = TracerProvider()
    with caplog.at_level(logging.WARNING, logger="convergent.sdk"):
        convergent.otel.install(
            provider,
            api_key="other-key",  # pragma: allowlist secret
            endpoint="https://other.test",
            release="r1",
            agents=["support-agent"],
        )

    # A processor that has already lost must not build a session, a batch thread,
    # and an exporter just to shut them down again.
    shut_down_on_its_own = not built
    # A span its own agents= would keep, so this fails if it is still armed.
    with provider.get_tracer("their.app").start_as_current_span(
        "invoke_agent support-agent",
        attributes={
            "gen_ai.operation.name": "invoke_agent",
            "gen_ai.agent.name": "support-agent",
            "gen_ai.input.messages": "secret",
        },
    ):
        pass
    exporter.force_flush(1_000)
    provider.shutdown()

    assert ours.get_finished_spans() == (), "a dropped processor exports nothing"
    assert shut_down_on_its_own, "and it does not leave an exporter running"
    assert len(_core._drain) == drained, "and it is never added to the drain"
    assert _core.live_status().destinations == ["convergent"]
    dropped = [r for r in caplog.records if "sends nothing" in r.message]
    assert len(dropped) == 1
    assert "agents it declared" in dropped[0].message, "a dropped privacy control is named"
    assert "api key or endpoint" in dropped[0].message


def test_a_second_processor_sends_nothing_even_with_the_same_settings(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    install: Callable[..., Installed],
) -> None:
    """Two processors with identical settings used to export every span twice, and
    said nothing about it."""
    first = InMemorySpanExporter()
    posts: list[str] = []

    def count(url: str, *_: object, **__: object) -> dict[str, object]:
        posts.append(url)
        return {"deployment_id": "dep_test", "is_new": True}

    monkeypatch.setattr(_registry, "post_json", count)
    monkeypatch.setattr(_transport, "build_processor", lambda **_: SimpleSpanProcessor(first))
    installed = install()
    _settle(installed.processor)

    second = InMemorySpanExporter()
    monkeypatch.setattr(_transport, "build_processor", lambda **_: SimpleSpanProcessor(second))
    with caplog.at_level(logging.WARNING, logger="convergent.sdk"):
        convergent.otel.install(installed.provider, api_key=KEY, endpoint=ENDPOINT, release="r1")

    with convergent.span(name="support-agent", operation="agent_run"):
        pass

    assert len(first.get_finished_spans()) == 1, "one span out, not two"
    assert second.get_finished_spans() == ()
    assert posts == [DEPLOYMENTS], "and one registration"
    assert [r for r in caplog.records if "sends nothing" in r.message]


def test_init_after_a_processor_keeps_the_processors_configuration(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    install: Callable[..., Installed],
) -> None:
    """The other order. ``init()`` is the one that loses, and the processor's
    setup keeps running."""
    exporter = InMemorySpanExporter()
    posts: list[str] = []

    def count(url: str, *_: object, **__: object) -> dict[str, object]:
        posts.append(url)
        return {"deployment_id": "dep_test", "is_new": True}

    monkeypatch.setattr(_registry, "post_json", count)
    monkeypatch.setattr(_transport, "build_processor", lambda **_: SimpleSpanProcessor(exporter))
    _settle(install().processor)

    with caplog.at_level(logging.WARNING, logger="convergent.sdk"):
        status = convergent.init(api_key=KEY, endpoint=ENDPOINT, release="r1")

    with convergent.span(name="gpt", operation="model_call") as call:
        call.set_input("what is my account number")

    assert status.mode == "attached"
    assert len(exporter.get_finished_spans()) == 1, "one span out, not two"
    assert posts == [DEPLOYMENTS], "and one registration"
    assert [r for r in caplog.records if "already configured" in r.message]


# --- the provider span() and observe() record through ------------------------


def test_a_provider_never_installed_globally_says_so_rather_than_looking_healthy(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    install: Callable[..., Installed],
) -> None:
    """A caller who builds the processor by hand and keeps their provider to
    themselves cannot be found by ``span()``. Spans from their own tracers still
    flow, and ``check()`` names the half that does not."""
    exporter = InMemorySpanExporter()
    _serve(monkeypatch, exporter)
    provider = TracerProvider()
    provider.add_span_processor(
        ConvergentSpanProcessor(api_key=KEY, endpoint=ENDPOINT, release="r1")
    )

    with caplog.at_level(logging.WARNING, logger="convergent.sdk"):
        with convergent.span(name="support-agent", operation="agent_run"):
            pass
    with provider.get_tracer("their.app").start_as_current_span(
        "chat", attributes={"gen_ai.operation.name": "chat"}
    ):
        pass
    provider.shutdown()

    assert _names(exporter) == {"chat"}, "their own tracer still reaches us"
    assert _core.live_status().reason == "no_provider"
    assert [r for r in caplog.records if "cannot find the tracer provider" in r.message]


def test_a_third_partys_global_provider_is_not_mistaken_for_ours(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Somebody else owns the global provider. Recording ``span()`` into it would
    put the caller's agent spans in a pipeline that never reaches us, and report
    healthy while doing it."""
    ours = InMemorySpanExporter()
    theirs = InMemorySpanExporter()
    _serve(monkeypatch, ours)

    unrelated = TracerProvider()
    unrelated.add_span_processor(SimpleSpanProcessor(theirs))
    trace.set_tracer_provider(unrelated)

    mine = TracerProvider()
    mine.add_span_processor(ConvergentSpanProcessor(api_key=KEY, endpoint=ENDPOINT, release="r1"))

    with caplog.at_level(logging.WARNING, logger="convergent.sdk"):
        with convergent.span(name="support-agent", operation="agent_run"):
            pass
    mine.shutdown()
    unrelated.shutdown()

    assert theirs.get_finished_spans() == (), "no span leaked into the other pipeline"
    assert ours.get_finished_spans() == ()
    assert _core.live_status().reason == "no_provider"
    assert [r for r in caplog.records if "cannot find the tracer provider" in r.message]


def test_a_provider_installed_globally_after_the_processor_is_found(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """``add_span_processor`` before ``set_tracer_provider`` is the normal order, so
    the provider is looked for again on each read until it answers."""
    exporter = InMemorySpanExporter()
    _serve(monkeypatch, exporter)
    provider = TracerProvider()
    provider.add_span_processor(
        ConvergentSpanProcessor(api_key=KEY, endpoint=ENDPOINT, release="r1")
    )
    assert _core.live_status().reason == "no_provider", "nothing to record through yet"

    trace.set_tracer_provider(provider)
    caplog.clear()

    with caplog.at_level(logging.WARNING, logger="convergent.sdk"):
        with convergent.span(name="support-agent", operation="agent_run"):
            pass
    provider.shutdown()

    assert _names(exporter) == {"invoke_agent support-agent"}
    assert _core.live_status().reason is None
    assert not [r for r in caplog.records if "cannot find the tracer provider" in r.message]


def test_the_deployment_is_registered_once_however_many_spans_run(
    monkeypatch: pytest.MonkeyPatch, install: Callable[..., Installed]
) -> None:
    posts: list[str] = []

    def count(url: str, *_: object, **__: object) -> dict[str, object]:
        posts.append(url)
        return {"deployment_id": "dep_test", "is_new": True}

    monkeypatch.setattr(_registry, "post_json", count)
    monkeypatch.setattr(
        _transport, "build_processor", lambda **_: SimpleSpanProcessor(InMemorySpanExporter())
    )
    installed = install()
    assert posts == [], "construction makes no network call"

    _run_one_agent()
    _join_registration(installed.processor)
    _run_one_agent()
    convergent.flush()

    assert posts == [DEPLOYMENTS]
    status = _core.live_status()
    assert status.deployment == "dep_test"
    assert status.mode == "attached"
    assert status.destinations == ["convergent"]


def test_two_lost_processors_warn_once_each(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A second processor that loses with different agents or endpoint gets its own
    warning, so a caller can see every dropped privacy or routing choice."""
    _serve(monkeypatch, InMemorySpanExporter())
    convergent.init(api_key=KEY, endpoint=ENDPOINT, release="r1")
    drained = len(_core._drain)

    with caplog.at_level(logging.WARNING, logger="convergent.sdk"):
        convergent.otel.install(
            TracerProvider(),
            api_key="other-key",  # pragma: allowlist secret
            endpoint="https://other.test",
            release="r1",
        )
        convergent.otel.install(
            TracerProvider(),
            api_key="other-key",  # pragma: allowlist secret
            endpoint="https://other.test",
            release="r1",
            agents=["support-agent"],
        )

    dropped = [r for r in caplog.records if "sends nothing" in r.message]
    assert len(dropped) == 2, "each different loser warns once"
    assert len(_core._drain) == drained, "no exporter is added to the drain"


def test_an_init_that_loses_to_a_processor_says_what_it_dropped(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A library that installs a processor first makes the application's init() the
    loser. That init() must name the privacy controls it did not get and report a
    reason, because every other field on Status describes the configuration that
    won and reads healthy."""
    _serve(monkeypatch, InMemorySpanExporter())
    convergent.otel.install(
        TracerProvider(),
        api_key="lib-key",  # pragma: allowlist secret
        endpoint="https://lib.test",
        release="lib-1",
    )

    with caplog.at_level(logging.WARNING, logger="convergent.sdk"):
        status = convergent.init(
            api_key=KEY,
            endpoint=ENDPOINT,
            release="app-1",
            agents=["support-agent"],
        )

    assert status.reason == "already_configured"
    kept = [r.message for r in caplog.records if "kept the first setup" in r.message]
    assert len(kept) == 1
    assert "agents it declared" in kept[0]
    assert "api key or endpoint" in kept[0]
    running = _core._running_config()
    assert running is not None
    assert running.endpoint == "https://lib.test"


def test_a_failed_install_leaves_no_processor_stamping_spans(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A processor whose provider refused it is disarmed, not left half-armed: it
    stamps nothing onto a span it is handed, the way a processor that lost the claim
    stamps nothing."""
    _serve(monkeypatch, InMemorySpanExporter())
    provider = TracerProvider()
    monkeypatch.setattr(
        provider, "add_span_processor", lambda *_, **__: (_ for _ in ()).throw(RuntimeError("no"))
    )
    processor = convergent.otel.install(
        provider, api_key=KEY, endpoint=ENDPOINT, release="r1", agents=["support-agent"]
    )

    assert processor._semantic is None
    assert processor._destinations == ()


def test_install_records_setup_failure_when_add_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the provider refuses the processor, the claim is released and check() reports
    a setup failure instead of leaving the process configured with an unattached
    processor."""
    _serve(monkeypatch, InMemorySpanExporter())
    provider = TracerProvider()

    def raise_on_add(*_: object, **__: object) -> None:
        raise RuntimeError("provider is shut down")

    monkeypatch.setattr(provider, "add_span_processor", raise_on_add)
    convergent.otel.install(provider, api_key=KEY, endpoint=ENDPOINT, release="r1")

    status = _core.live_status()
    assert status.enabled is False
    assert status.reason == "setup_failed"
    assert status.release == "r1"
    assert _core._config is None
    assert _core._startup_failure is not None


def test_install_add_failure_removes_the_exporter_with_declared_agents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When add_span_processor() raises and the processor has a declared-agent filter,
    the raw exporter (not the filter) must be removed from _core._drain so the
    drain list is actually empty."""
    _serve(monkeypatch, InMemorySpanExporter())
    provider = TracerProvider()

    def raise_on_add(*_: object, **__: object) -> None:
        raise RuntimeError("provider is shut down")

    monkeypatch.setattr(provider, "add_span_processor", raise_on_add)
    convergent.otel.install(
        provider,
        api_key=KEY,
        endpoint=ENDPOINT,
        release="r1",
        agents=["support-agent"],
    )

    assert _core._config is None
    assert len(_core._drain) == 0, "the raw exporter is removed from the drain"


def test_first_flush_starts_registration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A short-lived process that flushes before any span still resolves its
    deployment identity, because flush() triggers the same lazy registration that
    the first span does."""
    _serve(monkeypatch, InMemorySpanExporter())
    started: list[bool] = []
    monkeypatch.setattr(
        ConvergentSpanProcessor,
        "_start_registration",
        lambda self: started.append(True),
    )

    provider = TracerProvider()
    convergent.otel.install(provider, api_key=KEY, endpoint=ENDPOINT, release="r1")
    trace.set_tracer_provider(provider)

    assert started == [], "construction does not start registration"
    convergent.flush()
    assert started == [True], "the first flush starts registration"


def test__carries_finds_processor_inside_a_composite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A caller may wrap the ConvergentSpanProcessor in their own composite processor.
    _carries() must still find it so span() and observe() can record through it."""
    _serve(monkeypatch, InMemorySpanExporter())

    provider = TracerProvider()
    processor = convergent.otel.install(provider, api_key=KEY, endpoint=ENDPOINT, release="r1")

    class Wrapper(SpanProcessor):
        _span_processors: tuple[SpanProcessor, ...]

        def __init__(self, wrapped: SpanProcessor) -> None:
            self._span_processors = (wrapped,)

        def on_start(self, span: ReadableSpan, parent_context: Any) -> None:
            pass

        def on_end(self, span: ReadableSpan) -> None:
            pass

        def shutdown(self) -> None:
            pass

        def force_flush(self, timeout_millis: int = 30_000) -> bool:
            return True

    provider.add_span_processor(Wrapper(processor))
    trace.set_tracer_provider(provider)
    assert _otel._carries(provider, processor)


def test_require_filters_through_install(
    monkeypatch: pytest.MonkeyPatch, install: Callable[..., Installed]
) -> None:
    """install() applies the same rule init(require_span_attributes=) does: the context mark
    reaches library spans, and a request marked with an unallowed value or not
    marked at all is withheld."""
    exporter = InMemorySpanExporter()
    _serve(monkeypatch, exporter)
    provider = install(require_span_attributes={"customer.id": ["acme"]}).provider
    tracer = provider.get_tracer("their.framework")

    with _marked({"customer.id": "acme"}):
        with tracer.start_as_current_span("acme request"):
            with tracer.start_as_current_span("acme library work"):
                pass
    with _marked({"customer.id": "initech"}):
        with tracer.start_as_current_span("initech request"):
            pass
    with tracer.start_as_current_span("unmarked request"):
        pass

    assert _names(exporter) == {"acme request", "acme library work"}


def test_require_and_agents_compose_through_install(
    monkeypatch: pytest.MonkeyPatch, install: Callable[..., Installed]
) -> None:
    """Both filters apply on the install() path too: a span is sent only when it
    is a declared agent's work and the request is an allowed customer."""
    exporter = InMemorySpanExporter()
    _serve(monkeypatch, exporter)
    install(agents=["support-agent"], require_span_attributes={"customer.id": ["acme"]})

    with _marked({"customer.id": "acme"}):
        with convergent.span(name="support-agent", operation="agent_run"):
            pass
    with _marked({"customer.id": "initech"}):
        with convergent.span(name="support-agent", operation="agent_run"):
            pass
    with _marked({"customer.id": "acme"}):
        with convergent.span(name="billing-agent", operation="agent_run"):
            pass

    assert _names(exporter) == {"invoke_agent support-agent"}


def test_require_filters_through_a_processor_added_by_hand(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The third entry point: the caller constructs ConvergentSpanProcessor
    themselves and adds it to their own provider."""
    exporter = InMemorySpanExporter()
    _serve(monkeypatch, exporter)
    provider = TracerProvider()
    processor = ConvergentSpanProcessor(
        api_key=KEY,
        endpoint=ENDPOINT,
        release="r1",
        require_span_attributes={"customer.id": ["acme"]},
    )
    provider.add_span_processor(processor)
    trace.set_tracer_provider(provider)
    tracer = provider.get_tracer("their.framework")

    try:
        with _marked({"customer.id": "acme"}):
            with tracer.start_as_current_span("acme request"):
                pass
        with _marked({"customer.id": "initech"}):
            with tracer.start_as_current_span("initech request"):
                pass

        assert _names(exporter) == {"acme request"}
    finally:
        _join_registration(processor)
        provider.shutdown()


def test_a_malformed_require_through_install_raises_under_strict() -> None:
    """A typo in a privacy control is loud on every entry point, not only init().

    No exporter is stubbed: the raise happens at validation, before anything
    is built.
    """
    provider = TracerProvider()

    with pytest.raises((TypeError, ValueError)):
        convergent.otel.install(
            provider,
            api_key=KEY,
            endpoint=ENDPOINT,
            release="r1",
            require_span_attributes={"customer.id": object()},
            strict=True,
        )


def test_reject_filters_through_install(
    monkeypatch: pytest.MonkeyPatch, install: Callable[..., Installed]
) -> None:
    """install() applies the same reject_span_attributes= rule init() does: a span holding a
    rejected pair is withheld, and an unmarked span is sent."""
    exporter = InMemorySpanExporter()
    _serve(monkeypatch, exporter)
    provider = install(reject_span_attributes={"customer.id": ["initech"]}).provider
    tracer = provider.get_tracer("their.framework")

    with _marked({"customer.id": "initech"}):
        with tracer.start_as_current_span("initech request"):
            pass
    with _marked({"customer.id": "acme"}):
        with tracer.start_as_current_span("acme request"):
            pass
    with tracer.start_as_current_span("unmarked request"):
        pass

    assert _names(exporter) == {"acme request", "unmarked request"}


def test_reject_filters_through_a_processor_added_by_hand(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The third entry point takes reject_span_attributes= too."""
    exporter = InMemorySpanExporter()
    _serve(monkeypatch, exporter)
    provider = TracerProvider()
    processor = ConvergentSpanProcessor(
        api_key=KEY,
        endpoint=ENDPOINT,
        release="r1",
        reject_span_attributes={"customer.id": ["initech"]},
    )
    provider.add_span_processor(processor)
    trace.set_tracer_provider(provider)
    tracer = provider.get_tracer("their.framework")

    try:
        with _marked({"customer.id": "initech"}):
            with tracer.start_as_current_span("initech request"):
                pass
        with tracer.start_as_current_span("unmarked request"):
            pass

        assert _names(exporter) == {"unmarked request"}
    finally:
        _join_registration(processor)
        provider.shutdown()


def test_the_reject_variable_configures_install(
    monkeypatch: pytest.MonkeyPatch, install: Callable[..., Installed]
) -> None:
    """The processor path resolves CONVERGENT_REJECT_SPAN_ATTRIBUTES the way
    init() does: the variable fills the direction in when the argument is
    absent."""
    monkeypatch.setenv("CONVERGENT_REJECT_SPAN_ATTRIBUTES", '{"customer.id": ["initech"]}')
    exporter = InMemorySpanExporter()
    _serve(monkeypatch, exporter)
    provider = install().provider
    tracer = provider.get_tracer("their.framework")

    with _marked({"customer.id": "initech"}):
        with tracer.start_as_current_span("initech request"):
            pass
    with tracer.start_as_current_span("unmarked request"):
        pass

    assert _names(exporter) == {"unmarked request"}


def test_a_malformed_reject_through_install_raises_under_strict() -> None:
    """reject_span_attributes= takes the same validation path
    require_span_attributes= does, on every entry point."""
    provider = TracerProvider()

    with pytest.raises(TypeError, match="reject_span_attributes="):
        convergent.otel.install(
            provider,
            api_key=KEY,
            endpoint=ENDPOINT,
            release="r1",
            reject_span_attributes={"customer.id": object()},
            strict=True,
        )


def test_a_malformed_reject_through_a_processor_disables_with_strict_off(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Strict off, the processor logs the exact problem at ERROR and sends
    nothing, the way a malformed require_span_attributes= does."""
    monkeypatch.delenv("CONVERGENT_STRICT", raising=False)
    exporter = InMemorySpanExporter()
    _serve(monkeypatch, exporter)
    provider = TracerProvider()

    with caplog.at_level(logging.ERROR, logger="convergent.sdk"):
        processor = ConvergentSpanProcessor(
            api_key=KEY,
            endpoint=ENDPOINT,
            release="r1",
            reject_span_attributes={"customer.id": object()},
        )
    provider.add_span_processor(processor)
    trace.set_tracer_provider(provider)

    with provider.get_tracer("their.framework").start_as_current_span("anything"):
        pass
    provider.shutdown()

    assert _names(exporter) == set()
    assert _core.live_status().enabled is False
    assert [r for r in caplog.records if "cannot work" in r.message]
