"""What leaves the process under ``init(require_span_attributes=...)`` and
``init(reject_span_attributes=...)``, and how ``context_attributes=`` marks a
span's whole subtree."""

from __future__ import annotations

import asyncio
import contextvars
import functools
import logging
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Any

import pytest
from opentelemetry import baggage as otel_baggage
from opentelemetry import context as otel_context
from opentelemetry import propagate, trace
from opentelemetry.context import Context
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import (
    ReadableSpan,
    Span,
    SpanProcessor,
    TracerProvider,
)
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import convergent
from convergent import _core, _policy, _processors, _registry, _transport


def _built(
    require_span_attributes: Mapping[str, object] | None = None,
    reject_span_attributes: Mapping[str, object] | None = None,
) -> _policy.Policy:
    policy = _policy.build(require_span_attributes, reject_span_attributes)
    assert policy is not None
    return policy


CUSTOMER_IS_ACME = _built(require_span_attributes={"customer.id": ["acme"]})


@contextmanager
def _marked(pairs: Mapping[str, Any]) -> Iterator[None]:
    """The context carrier around a block, the way ``span()`` attaches it.

    The raw-provider tests below exercise the processors without the SDK's own
    ``span()``, so they attach the pairs through the same internal calls it uses.
    """
    token = _processors.attach_context(pairs)
    try:
        yield
    finally:
        _processors.detach_context(token)


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

    def start(**kwargs: object) -> InMemorySpanExporter:
        exporter = InMemorySpanExporter()
        monkeypatch.setattr(
            _registry,
            "post_json",
            lambda *a, **k: {"deployment_id": "dep_test", "is_new": True},
        )
        monkeypatch.setattr(
            _transport, "build_processor", lambda **_: SimpleSpanProcessor(exporter)
        )
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


def _span_named(exporter: InMemorySpanExporter, name: str) -> ReadableSpan:
    return next(span for span in exporter.get_finished_spans() if span.name == name)


def _filtered_provider(
    policy: _policy.Policy = CUSTOMER_IS_ACME,
    resource: Resource | None = None,
) -> tuple[TracerProvider, InMemorySpanExporter]:
    """A provider wired the way ``init()`` wires one: the context stamper on
    the provider, the filter in front of the destination."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider(resource=resource or Resource.create({}))
    wrapped = _processors.wrap(policy, None, [SimpleSpanProcessor(exporter)])
    for processor in [_processors._STAMPER, *wrapped]:
        provider.add_span_processor(processor)
    return provider, exporter


class _Recorder(SpanProcessor):
    """A destination that writes down every call, for the delegation tests."""

    def __init__(self, flush_answer: bool = True) -> None:
        self.calls: list[str] = []
        self._flush_answer = flush_answer

    def on_start(self, span: Span, parent_context: Context | None = None) -> None:
        self.calls.append(f"on_start:{span.name}")

    def on_end(self, span: ReadableSpan) -> None:
        self.calls.append(f"on_end:{span.name}")

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        self.calls.append("force_flush")
        return self._flush_answer

    def shutdown(self) -> None:
        self.calls.append("shutdown")


# --- the context carrier marks a subtree ---------------------------------------


def test_a_mark_around_a_request_keeps_its_spans_library_spans_included() -> None:
    """A caller can mark the span their own code opened but not the span litellm
    opens underneath it, and that span has to be sent too or the trace arrives
    with one span in it. The pairs live in the context every child inherits, and
    the stamped key is visible on each exported span."""
    provider, exporter = _filtered_provider()
    tracer = provider.get_tracer("their.framework")

    with _marked({"customer.id": "acme"}):
        with tracer.start_as_current_span("invoke_agent Chat"):
            with tracer.start_as_current_span("chat gpt-4"):
                pass
            with tracer.start_as_current_span("GET api.foo.com"):
                pass
    provider.force_flush()

    assert _names(exporter) == {"invoke_agent Chat", "chat gpt-4", "GET api.foo.com"}
    for span in exporter.get_finished_spans():
        assert (span.attributes or {})["convergent.attributes.customer.id"] == "acme"


def test_a_span_parented_by_hand_inherits_the_parent_marks() -> None:
    """litellm starts model spans under ``set_span_in_context(parent)`` -- a
    context with no mark scope. The child inherits the parent's stamped marks,
    so ``require_span_attributes=`` keeps it with the rest of the run."""
    provider, exporter = _filtered_provider()
    tracer = provider.get_tracer("their.framework")

    with _marked({"customer.id": "acme"}):
        parent = tracer.start_span("invoke_agent lead")
    child = tracer.start_span("chat gpt-4o-mini", context=trace.set_span_in_context(parent))
    child.end()
    parent.end()
    provider.force_flush()

    assert _names(exporter) == {"invoke_agent lead", "chat gpt-4o-mini"}
    kept = _span_named(exporter, "chat gpt-4o-mini")
    assert (kept.attributes or {})["convergent.attributes.customer.id"] == "acme"


def test_reject_withholds_a_hand_parented_child_of_a_rejected_run() -> None:
    """The leak the litellm trials proved: under ``reject_span_attributes=`` an
    unmarked child of an excluded run was sent, prompts included. With
    inheritance the child carries the mark and is withheld with its run."""
    provider, exporter = _filtered_provider(
        _built(reject_span_attributes={"customer.id": ["initech"]})
    )
    tracer = provider.get_tracer("their.framework")

    with _marked({"customer.id": "initech"}):
        parent = tracer.start_span("invoke_agent lead")
    child = tracer.start_span("chat gpt-4o-mini", context=trace.set_span_in_context(parent))
    child.end()
    parent.end()
    provider.force_flush()

    assert _names(exporter) == set()


def test_a_mark_scope_in_the_context_wins_over_parent_inheritance() -> None:
    """Inheritance is the fallback for a context with no mark scope. A context
    that carries one keeps its own pairs, whatever the parent holds."""
    provider, exporter = _filtered_provider(
        _built(require_span_attributes={"customer.id": ["acme", "globex"]})
    )
    tracer = provider.get_tracer("their.framework")

    with _marked({"customer.id": "acme"}):
        parent = tracer.start_span("invoke_agent lead")
    with _marked({"customer.id": "globex"}):
        scoped = trace.set_span_in_context(parent, otel_context.get_current())
        child = tracer.start_span("chat gpt-4o-mini", context=scoped)
        child.end()
    parent.end()
    provider.force_flush()

    kept = _span_named(exporter, "chat gpt-4o-mini")
    assert (kept.attributes or {})["convergent.attributes.customer.id"] == "globex"


def test_a_remote_parent_stamps_nothing() -> None:
    """A propagated parent is a ``SpanContext`` with no attributes to read, so a
    span started under it carries no mark, the way it always did."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    wrapped = _processors.wrap(None, None, [SimpleSpanProcessor(exporter)])
    for processor in [_processors._STAMPER, *wrapped]:
        provider.add_span_processor(processor)
    remote = trace.NonRecordingSpan(
        trace.SpanContext(
            trace_id=0x1, span_id=0x2, is_remote=True, trace_flags=trace.TraceFlags(0x1)
        )
    )

    span = provider.get_tracer("their.framework").start_span(
        "chat", context=trace.set_span_in_context(remote)
    )
    span.end()
    provider.force_flush()

    attributes = exporter.get_finished_spans()[0].attributes or {}
    assert not any(key.startswith("convergent.attributes.") for key in attributes)


def test_an_explicit_override_propagates_to_its_subtree() -> None:
    """A span's own ``context_attributes=`` overrides what it inherits, per
    key, and its descendants follow the override. Keys the override does not
    name flow through unchanged."""
    provider, exporter = _filtered_provider(
        _built(require_span_attributes={"customer.id": ["acme", "globex"]})
    )
    tracer = provider.get_tracer("their.framework")

    with _marked({"customer.id": "acme", "tier": "pro"}):
        top = tracer.start_span("invoke_agent lead")
    with _marked({"customer.id": "globex"}):
        middle = tracer.start_span(
            "invoke_agent subagent",
            context=trace.set_span_in_context(top, otel_context.get_current()),
        )
    leaf = tracer.start_span("chat gpt-4o-mini", context=trace.set_span_in_context(middle))
    leaf.end()
    middle.end()
    top.end()
    provider.force_flush()

    middle_attrs = _span_named(exporter, "invoke_agent subagent").attributes or {}
    leaf_attrs = _span_named(exporter, "chat gpt-4o-mini").attributes or {}
    for attrs in (middle_attrs, leaf_attrs):
        assert attrs["convergent.attributes.customer.id"] == "globex"
        assert attrs["convergent.attributes.tier"] == "pro"


def test_a_parent_past_the_attribute_limit_still_yields_marks() -> None:
    """The public attribute bag evicts its oldest entry -- the mark -- once the
    span passes its attribute limit. Inheritance reads the private marks field,
    so a bulky parent still passes its marks to a hand-parented child."""
    provider, exporter = _filtered_provider()
    tracer = provider.get_tracer("their.framework")

    with _marked({"customer.id": "acme"}):
        parent = tracer.start_span("invoke_agent lead")
    for index in range(200):
        parent.set_attribute(f"bulk.{index}", index)
    assert isinstance(parent, Span)
    assert "convergent.attributes.customer.id" not in (parent.attributes or {}), (
        "the eviction this test guards against did not fire; raise the churn"
    )

    child = tracer.start_span("chat gpt-4o-mini", context=trace.set_span_in_context(parent))
    child.end()
    parent.end()
    provider.force_flush()

    kept = _span_named(exporter, "chat gpt-4o-mini")
    assert (kept.attributes or {})["convergent.attributes.customer.id"] == "acme"


def test_concurrent_parent_mutation_cannot_unmark_a_child() -> None:
    """Reading the parent's public attributes from another thread raced the
    caller's writes; the swallowed error shipped spans unmarked. The private
    marks field is written once at start, so attribute churn changes nothing."""
    provider, exporter = _filtered_provider()
    tracer = provider.get_tracer("their.framework")

    with _marked({"customer.id": "acme"}):
        parent = tracer.start_span("invoke_agent lead")
    stop = threading.Event()

    def churn() -> None:
        index = 0
        while not stop.is_set():
            parent.set_attribute(f"churn.{index % 50}", index)
            index += 1

    churner = threading.Thread(target=churn)
    churner.start()
    try:
        for index in range(200):
            child = tracer.start_span(f"chat {index}", context=trace.set_span_in_context(parent))
            child.end()
    finally:
        stop.set()
        churner.join()
    parent.end()
    provider.force_flush()

    finished = [s for s in exporter.get_finished_spans() if s.name.startswith("chat ")]
    assert len(finished) == 200
    assert all(
        (span.attributes or {}).get("convergent.attributes.customer.id") == "acme"
        for span in finished
    )


def test_a_process_that_never_marked_takes_the_short_circuit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A process that never attached a mark scope pays nothing at span start:
    the stamper returns before any context or parent read."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(_processors._STAMPER)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("their.framework")

    with _marked({"customer.id": "acme"}):
        parent = tracer.start_span("invoke_agent lead")

    monkeypatch.setattr(_processors, "_ever_attached", False)
    off = tracer.start_span("chat off", context=trace.set_span_in_context(parent))
    off.end()
    monkeypatch.setattr(_processors, "_ever_attached", True)
    on = tracer.start_span("chat on", context=trace.set_span_in_context(parent))
    on.end()
    parent.end()
    provider.force_flush()

    spans = {span.name: (span.attributes or {}) for span in exporter.get_finished_spans()}
    assert "convergent.attributes.customer.id" not in spans["chat off"]
    assert spans["chat on"]["convergent.attributes.customer.id"] == "acme"


def test_every_context_pair_is_stamped_filters_or_not() -> None:
    """The stamper copies every pair, and it runs with no filter configured, so
    ``context_attributes=`` annotates spans on its own."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    wrapped = _processors.wrap(None, None, [SimpleSpanProcessor(exporter)])
    for processor in [_processors._STAMPER, *wrapped]:
        provider.add_span_processor(processor)

    with _marked({"customer.id": "acme", "run.kind": "smoke"}):
        with provider.get_tracer("their.framework").start_as_current_span("kept"):
            pass
    provider.force_flush()

    attributes = exporter.get_finished_spans()[0].attributes or {}
    assert attributes["convergent.attributes.customer.id"] == "acme"
    assert attributes["convergent.attributes.run.kind"] == "smoke"


def test_baggage_is_not_read() -> None:
    """The carrier is a private context value, not baggage. A baggage entry no
    longer stamps a span, so it neither satisfies ``require_span_attributes=`` nor leaks onto
    what is exported."""
    theirs = InMemorySpanExporter()
    provider, ours = _filtered_provider()
    provider.add_span_processor(SimpleSpanProcessor(theirs))
    tracer = provider.get_tracer("their.framework")

    token = otel_context.attach(otel_baggage.set_baggage("customer.id", "acme"))
    try:
        with tracer.start_as_current_span("marked with baggage only"):
            pass
    finally:
        otel_context.detach(token)
    provider.force_flush()

    assert _names(ours) == set()
    their_attributes = theirs.get_finished_spans()[0].attributes or {}
    assert "customer.id" not in their_attributes
    assert "convergent.attributes.customer.id" not in their_attributes


def test_inject_writes_nothing_for_the_context_pairs() -> None:
    """The pairs stay in the process. ``inject()`` writes trace headers and
    baggage; our context value produces no header at all."""
    carrier: dict[str, str] = {}
    with _marked({"customer.id": "acme"}):
        propagate.inject(carrier)

    assert carrier == {}


def test_the_pairs_do_not_follow_a_raw_thread_unless_the_context_is_copied() -> None:
    """The carrier is a context value, and Python starts a thread with an empty
    context. ``contextvars.copy_context()`` is the documented way to hand the
    pairs to a worker thread."""
    seen: dict[str, Mapping[str, Any]] = {}

    def read(label: str) -> None:
        seen[label] = dict(_processors.context_pairs())

    with _marked({"customer.id": "acme"}):
        bare = threading.Thread(target=read, args=("bare",))
        copied_context = contextvars.copy_context()
        copied = threading.Thread(target=lambda: copied_context.run(read, "copied"))
        for worker in (bare, copied):
            worker.start()
            worker.join(timeout=5)

    assert seen["bare"] == {}
    assert seen["copied"] == {"customer.id": "acme"}


def test_the_pairs_follow_an_asyncio_task() -> None:
    """An asyncio task copies the context it was created in, so a span started
    inside the task is stamped and kept."""
    provider, exporter = _filtered_provider()
    tracer = provider.get_tracer("their.framework")

    async def in_a_task() -> None:
        with tracer.start_as_current_span("task work"):
            pass

    async def main() -> None:
        with _marked({"customer.id": "acme"}):
            await asyncio.create_task(in_a_task())

    asyncio.run(main())
    provider.force_flush()

    assert _names(exporter) == {"task work"}


def test_the_pairs_detach_when_the_block_exits() -> None:
    provider, exporter = _filtered_provider()
    tracer = provider.get_tracer("their.framework")

    with _marked({"customer.id": "acme"}):
        pass
    with tracer.start_as_current_span("after the block"):
        pass
    provider.force_flush()

    assert _names(exporter) == set()


# --- every entry point feeds the same context carrier ----------------------------


_Entry = Callable[[trace.Tracer, Mapping[str, str] | None, Callable[[], None]], None]


def _via_span(
    _tracer: trace.Tracer, pairs: Mapping[str, str] | None, body: Callable[[], None]
) -> None:
    with convergent.span(name="agent", operation="agent_run", context_attributes=pairs):
        body()


def _via_decorator(decorate: Any) -> _Entry:
    def entry(
        _tracer: trace.Tracer, pairs: Mapping[str, str] | None, body: Callable[[], None]
    ) -> None:
        @decorate(name="agent", context_attributes=pairs)
        def run() -> None:
            body()

        run()

    return entry


def _via_attach_context(
    tracer: trace.Tracer, pairs: Mapping[str, str] | None, body: Callable[[], None]
) -> None:
    with _marked(pairs) if pairs is not None else nullcontext():
        with tracer.start_as_current_span("agent"):
            body()


@pytest.mark.parametrize(
    ("entry", "own_span"),
    [
        pytest.param(_via_span, "invoke_agent agent", id="span(context_attributes=)"),
        pytest.param(
            _via_decorator(functools.partial(convergent.observe, operation="workflow")),
            "agent",
            id="@observe(context_attributes=)",
        ),
        pytest.param(_via_decorator(convergent.agent), "invoke_agent agent", id="@agent"),
        pytest.param(_via_decorator(convergent.tool), "execute_tool agent", id="@tool"),
        pytest.param(_via_attach_context, "agent", id="attach_context"),
    ],
)
def test_every_entry_point_marks_the_request_and_its_library_children(
    entry: _Entry,
    own_span: str,
    start_sdk: Callable[..., InMemorySpanExporter],
) -> None:
    """Each way of attaching context pairs feeds the same carrier: the request's
    own span and the library span under it are stamped and kept under require_span_attributes=,
    a request marked for another customer is withheld whole, and an unmarked
    request is withheld."""
    exporter = start_sdk(require_span_attributes={"customer.id": ["acme"]})
    provider = _core.active_provider()
    assert provider is not None
    tracer = provider.get_tracer("their.framework")

    def child(name: str) -> Callable[[], None]:
        def open_child() -> None:
            with tracer.start_as_current_span(name):
                pass

        return open_child

    entry(tracer, {"customer.id": "acme"}, child("acme child"))
    entry(tracer, {"customer.id": "initech"}, child("initech child"))
    entry(tracer, None, child("unmarked child"))

    assert _names(exporter) == {own_span, "acme child"}


def test_nested_spans_merge_their_context_attributes_inner_wins(
    start_sdk: Callable[..., InMemorySpanExporter],
) -> None:
    exporter = start_sdk()
    with convergent.span(
        name="outer",
        operation="workflow",
        context_attributes={"customer.id": "initech", "env": "prod"},
    ):
        with convergent.span(
            name="inner", operation="workflow", context_attributes={"customer.id": "acme"}
        ):
            pass

    inner = (_span_named(exporter, "inner")).attributes or {}
    outer = (_span_named(exporter, "outer")).attributes or {}
    assert inner["convergent.attributes.customer.id"] == "acme"
    assert inner["convergent.attributes.env"] == "prod"
    assert outer["convergent.attributes.customer.id"] == "initech"


def test_a_key_in_both_parameters_coexists_and_the_mark_answers_the_filter(
    start_sdk: Callable[..., InMemorySpanExporter],
) -> None:
    """One call, one key in both parameters: the span carries the bare key from
    ``attributes=`` and the stamped key from ``context_attributes=``, and the
    filter reads the stamped key first, so the span is sent under a
    ``require_span_attributes=`` rule the bare value fails."""
    exporter = start_sdk(require_span_attributes={"customer.id": ["acme"]})
    provider = _core.active_provider()
    assert provider is not None
    tracer = provider.get_tracer("their.framework")

    with convergent.span(
        name="collide",
        operation="workflow",
        attributes={"customer.id": "initech"},
        context_attributes={"customer.id": "acme"},
    ):
        with tracer.start_as_current_span("child"):
            pass

    own = (_span_named(exporter, "collide")).attributes or {}
    child = (_span_named(exporter, "child")).attributes or {}
    assert own["customer.id"] == "initech"
    assert own["convergent.attributes.customer.id"] == "acme"
    assert child["convergent.attributes.customer.id"] == "acme"
    assert "customer.id" not in child


def test_an_invalid_context_attribute_is_dropped_and_logged_once(
    start_sdk: Callable[..., InMemorySpanExporter],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The same error path an invalid ``attributes=`` entry takes: the entry is
    dropped, the rest attach, and one ERROR line says so."""
    exporter = start_sdk()
    invalid: dict[str, Any] = {"customer.id": "acme", "payload": object()}
    with caplog.at_level(logging.ERROR, logger="convergent.sdk"):
        with convergent.span(name="kept", operation="workflow", context_attributes=invalid):
            pass
        with convergent.span(
            name="again", operation="workflow", context_attributes={"payload": invalid["payload"]}
        ):
            pass

    attributes = _span_named(exporter, "kept").attributes or {}
    assert attributes["convergent.attributes.customer.id"] == "acme"
    assert "convergent.attributes.payload" not in attributes
    dropped = [r for r in caplog.records if "context_attributes=" in r.message]
    assert len(dropped) == 1


def test_a_subclass_attributes_value_is_recorded_and_warned_once(
    start_sdk: Callable[..., InMemorySpanExporter],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An enum member passes isinstance but not the filter's exact-type match,
    so no ``require_span_attributes=`` or ``reject_span_attributes=`` rule can
    ever match it. The value is still recorded, and one WARNING names the
    parameter and the '.value' fix."""
    from enum import IntEnum

    class Tier(IntEnum):
        GOLD = 1

    exporter = start_sdk()
    with caplog.at_level(logging.WARNING, logger="convergent.sdk"):
        with convergent.span(name="kept", operation="workflow", attributes={"tier": Tier.GOLD}):
            pass
        with convergent.span(name="again", operation="workflow", attributes={"tier": Tier.GOLD}):
            pass

    assert (_span_named(exporter, "kept").attributes or {})["tier"] == 1
    warned = [r for r in caplog.records if "subclasses a plain type" in r.message]
    assert len(warned) == 1
    assert "attributes=" in warned[0].message
    assert "'.value'" in warned[0].message


def test_a_stamper_error_marks_nothing_and_reaches_no_caller(
    start_sdk: Callable[..., InMemorySpanExporter],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The provider calls its processors in a bare loop, so an exception in the
    stamper would reach the caller's application code. It is swallowed, warned
    once, and the span is left unmarked, which require_span_attributes= then withholds."""

    def exploding(parent_context: object = None) -> Mapping[str, Any]:
        raise RuntimeError("boom")

    exporter = start_sdk(require_span_attributes={"customer.id": ["acme"]})
    provider = _core.active_provider()
    assert provider is not None
    monkeypatch.setattr(_processors, "context_pairs", exploding)

    with _marked({"customer.id": "acme"}):
        with provider.get_tracer("their.framework").start_as_current_span("undecorated"):
            pass

    assert _names(exporter) == set()


def test_a_reserved_key_in_context_attributes_is_dropped(
    start_sdk: Callable[..., InMemorySpanExporter],
) -> None:
    exporter = start_sdk()
    with convergent.span(
        name="kept",
        operation="workflow",
        context_attributes={"gen_ai.agent.name": "forged", "customer.id": "acme"},
    ):
        pass

    attributes = _span_named(exporter, "kept").attributes or {}
    assert attributes["convergent.attributes.customer.id"] == "acme"
    assert "gen_ai.agent.name" not in attributes
    assert "convergent.attributes.gen_ai.agent.name" not in attributes


def test_interleaved_generators_leave_no_mark_behind(
    start_sdk: Callable[..., InMemorySpanExporter],
) -> None:
    """Two decorated generators drained in creation order detach their context
    tokens out of order. ``context.detach`` then restores a context that still
    names the first generator's scope.

    The liveness guard makes that stale scope read as no live values. A later
    span takes its pairs from its recorded parent span instead. The stamps and
    the parentage always agree."""
    exporter = start_sdk(require_span_attributes={"customer.id": ["acme", "initech"]})
    provider = _core.active_provider()
    assert provider is not None
    tracer = provider.get_tracer("their.framework")

    @convergent.observe(name="a", operation="workflow", context_attributes={"customer.id": "acme"})
    def first() -> Iterator[int]:
        yield 1
        yield 2

    @convergent.observe(
        name="b", operation="workflow", context_attributes={"customer.id": "initech"}
    )
    def second() -> Iterator[int]:
        yield 1
        yield 2

    def drive() -> None:
        a, b = first(), second()
        next(a)
        next(b)
        for _ in a:
            pass
        for _ in b:
            pass
        assert dict(_processors.context_pairs()) == {}
        with tracer.start_as_current_span("after the generators"):
            pass

    # A copied context, because the out-of-order detach also leaves an ended
    # span as OpenTelemetry's current one, and that leak must not reach the
    # other tests. The guard under test is about the mark, which the
    # assertions above read from inside the same context.
    contextvars.copy_context().run(drive)
    after = _span_named(exporter, "after the generators")
    stamped = (after.attributes or {}).get("convergent.attributes.customer.id")
    if after.parent is None:
        assert stamped is None, "no parent, nothing to inherit"
    else:
        parent_id = after.parent.span_id
        parent = next(
            s
            for s in exporter.get_finished_spans()
            if s.context is not None and s.context.span_id == parent_id
        )
        assert stamped == (parent.attributes or {})["convergent.attributes.customer.id"], (
            "a span under a stale context carries its recorded parent's stamps, "
            "never the stale scope's values"
        )


def test_a_suspended_generators_mark_stays_attached_until_its_block_exits(
    start_sdk: Callable[..., InMemorySpanExporter],
) -> None:
    """A generator shares the caller's context, so between next() calls the
    caller's own spans still carry the generator's mark: the guard neutralizes
    only a mark whose block has exited. Once the generator drains, the mark is
    gone and a later span is withheld under require_span_attributes=."""
    exporter = start_sdk(require_span_attributes={"customer.id": ["acme"]})
    provider = _core.active_provider()
    assert provider is not None
    tracer = provider.get_tracer("their.framework")

    @convergent.observe(name="g", operation="workflow", context_attributes={"customer.id": "acme"})
    def generate() -> Iterator[int]:
        yield 1
        yield 2

    g = generate()
    next(g)
    with tracer.start_as_current_span("between next calls"):
        pass
    for _ in g:
        pass
    with tracer.start_as_current_span("after the generator"):
        pass

    assert "between next calls" in _names(exporter)
    assert "after the generator" not in _names(exporter)


# --- what the filter decides -----------------------------------------------------
#
# Row shape: (why, require, reject, resource, span attributes, kept). Three
# sources answer a condition key, in order: the stamped mark
# ``convergent.attributes.<key>``, the span's own bare attribute, the
# per-process resource. The mark is the caller's per-request channel, so it
# outranks both; a bare span attribute outranks the resource. The cost,
# accepted by design: any library that writes the key onto one span overrules
# the resource for that span. The bare-resource rows are the zero-code path,
# where whoever configures the process sets OTEL_RESOURCE_ATTRIBUTES and every
# bare span passes.

_C = "customer.id"
_MARKED_C = "convergent.attributes.customer.id"
_ACME = {_C: ["acme"]}
_NO_DEV = {"env": ["dev"]}
_NO_INTERNAL = {"tier": ["internal"]}
_INTERNAL = {"tier": "internal"}
_REJECT_TWO_KEYS = {_C: ["initech"], "env": ["dev"]}

_SOURCE_ROWS = [
    ("no source anywhere fails closed", _ACME, None, {}, {}, False),
    ("the resource answers a bare span", _ACME, None, {_C: "acme"}, {}, True),
    ("a span attribute beats the resource", _ACME, None, {_C: "acme"}, {_C: "initech"}, False),
    ("a span attribute answers alone", _ACME, None, {_C: "initech"}, {_C: "acme"}, True),
    (
        "the mark beats a bare span attribute",
        _ACME,
        None,
        {},
        {_MARKED_C: "acme", _C: "initech"},
        True,
    ),
    (
        "the mark withholds past a bare span attribute",
        _ACME,
        None,
        {},
        {_MARKED_C: "initech", _C: "acme"},
        False,
    ),
    ("the mark beats the resource", _ACME, None, {_C: "initech"}, {_MARKED_C: "acme"}, True),
    (
        "reject reads the mark first",
        None,
        _NO_INTERNAL,
        {},
        {"convergent.attributes.tier": "internal", "tier": "ext"},
        False,
    ),
    ("reject reads the resource", None, _NO_INTERNAL, _INTERNAL, {}, False),
    ("reject: the span attribute beats it", None, _NO_INTERNAL, _INTERNAL, {"tier": "ext"}, True),
    ("reject ORs its keys: env matches", None, _REJECT_TWO_KEYS, {}, {"env": "dev"}, False),
    ("reject ORs its keys: customer matches", None, _REJECT_TWO_KEYS, {}, {_C: "initech"}, False),
    ("reject ORs its keys: neither matches", None, _REJECT_TWO_KEYS, {}, {"env": "prod"}, True),
    ("together: both satisfied sends", _ACME, _NO_DEV, {}, {_C: "acme", "env": "prod"}, True),
    ("together: reject withholds", _ACME, _NO_DEV, {}, {_C: "acme", "env": "dev"}, False),
    ("together: the unmet require withholds", _ACME, _NO_DEV, {}, {"env": "prod"}, False),
]


@pytest.mark.parametrize(
    ("require", "reject", "resource", "attributes", "kept"),
    [row[1:] for row in _SOURCE_ROWS],
    ids=[row[0] for row in _SOURCE_ROWS],
)
def test_the_mark_answers_first_then_the_span_then_the_resource(
    require: Mapping[str, object] | None,
    reject: Mapping[str, object] | None,
    resource: Mapping[str, str],
    attributes: Mapping[str, str],
    kept: bool,
) -> None:
    provider, exporter = _filtered_provider(
        _built(require_span_attributes=require, reject_span_attributes=reject),
        resource=Resource.create(dict(resource)),
    )
    with provider.get_tracer("their.framework").start_as_current_span(
        "probe", attributes=dict(attributes)
    ):
        pass
    provider.force_flush()

    assert (_names(exporter) == {"probe"}) is kept


def test_on_start_reaches_the_destinations_for_a_span_on_end_withholds() -> None:
    """``on_start`` forwards unconditionally, because the decision belongs to
    ``on_end``, where the finished span carries everything it will ever carry."""
    recorder = _Recorder()
    span_filter = _processors.FilterSpanProcessor(CUSTOMER_IS_ACME, [recorder])
    provider = TracerProvider()
    provider.add_span_processor(span_filter)

    with provider.get_tracer("their.framework").start_as_current_span("unmarked"):
        pass

    assert recorder.calls == ["on_start:unmarked"]


def test_a_context_stamp_beats_a_creation_time_span_attribute() -> None:
    """The stamp lands under ``convergent.attributes.`` and the filter reads
    that key first, so the caller's per-request mark wins over whatever a
    library wrote into the start call, and the library's value stays on the
    span untouched."""
    provider, exporter = _filtered_provider()
    tracer = provider.get_tracer("their.framework")

    with _marked({"customer.id": "acme"}):
        with tracer.start_as_current_span("kept", attributes={"customer.id": "initech"}):
            pass
    with _marked({"customer.id": "initech"}):
        with tracer.start_as_current_span("withheld", attributes={"customer.id": "acme"}):
            pass
    provider.force_flush()

    assert _names(exporter) == {"kept"}
    kept = exporter.get_finished_spans()[0].attributes or {}
    assert kept["convergent.attributes.customer.id"] == "acme"
    assert kept["customer.id"] == "initech"


def test_the_stamper_reads_the_context_the_span_was_started_with() -> None:
    """The stamper reads the context OpenTelemetry hands it, not the ambient one.
    A span started from an explicitly passed context that carries no open mark
    is not stamped, even inside a marked block, so it is withheld under
    require_span_attributes=. A context captured inside a marked block loses the mark when the
    block exits, because the pairs' lifetime is the block."""
    provider, exporter = _filtered_provider()
    tracer = provider.get_tracer("their.framework")

    with _marked({"customer.id": "acme"}):
        captured = otel_context.get_current()
        with tracer.start_as_current_span("handed empty", context=Context()):
            pass

    with tracer.start_as_current_span("handed after exit", context=captured):
        pass
    provider.force_flush()

    assert _names(exporter) == set()


def test_an_error_while_deciding_withholds_the_span(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An exception while deciding must not be a way past the filter: the span is
    withheld, whatever its own attributes say."""

    def exploding(*_: object) -> bool:
        raise RuntimeError("boom")

    monkeypatch.setattr(_policy, "decide", exploding)
    provider, exporter = _filtered_provider()
    tracer = provider.get_tracer("their.framework")

    with tracer.start_as_current_span("undecided", attributes={"customer.id": "acme"}):
        pass
    provider.force_flush()

    assert _names(exporter) == set()


def test_a_kept_span_that_outgrows_its_attribute_limit_is_withheld() -> None:
    """The mark lives on the span, and OpenTelemetry makes room for a new
    attribute by evicting the oldest, so a span that grows past the limit loses
    the mark and is withheld. The loss is deliberate, fail-closed, and
    visible: the span the caller's own processor receives reports
    ``dropped_attributes`` above zero."""
    theirs = InMemorySpanExporter()
    provider, ours = _filtered_provider()
    provider.add_span_processor(SimpleSpanProcessor(theirs))
    tracer = provider.get_tracer("their.framework")

    with _marked({"customer.id": "acme"}):
        with tracer.start_as_current_span("long chat") as span:
            for index in range(200):
                span.set_attribute(f"token.{index}", index)
    provider.force_flush()

    assert _names(ours) == set()
    lost = theirs.get_finished_spans()[0]
    assert "convergent.attributes.customer.id" not in (lost.attributes or {})
    assert lost.dropped_attributes > 0


# --- reject_span_attributes= ---------------------------------------------------------------------


def test_a_contradiction_logs_an_error_at_init_and_reject_wins_at_runtime(
    start_sdk: Callable[..., InMemorySpanExporter],
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.ERROR, logger="convergent.sdk"):
        exporter = start_sdk(
            require_span_attributes={"customer.id": ["acme"]},
            reject_span_attributes={"customer.id": ["acme"]},
        )
    provider = _core.active_provider()
    assert provider is not None

    with _marked({"customer.id": "acme"}):
        with provider.get_tracer("their.framework").start_as_current_span("contradicted"):
            pass

    assert [r for r in caplog.records if "reject_span_attributes= wins" in r.message]
    assert _names(exporter) == set()


@pytest.mark.parametrize("direction", ("require_span_attributes", "reject_span_attributes"))
def test_a_malformed_filter_with_strict_off_disables_tracing(
    direction: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With strict off, a filter value nothing could hold is logged at ERROR
    and tracing is disabled, so the exporter receives nothing. Swallowing the
    build error into "no filter" would instead send every span."""
    monkeypatch.delenv("CONVERGENT_STRICT", raising=False)
    exporter = InMemorySpanExporter()
    monkeypatch.setattr(
        _registry, "post_json", lambda *a, **k: {"deployment_id": "dep_test", "is_new": True}
    )
    monkeypatch.setattr(_transport, "build_processor", lambda **_: SimpleSpanProcessor(exporter))

    malformed: dict[str, Any] = {direction: {"customer.id": object()}}
    status = convergent.init(
        api_key="test-key",  # pragma: allowlist secret
        endpoint="https://example.test",
        release="r1",
        **malformed,
    )

    assert status.enabled is False
    with convergent.span(name="support-agent", operation="agent_run"):
        pass
    assert _names(exporter) == set()


def test_a_malformed_reject_raises_under_strict() -> None:
    with pytest.raises(TypeError, match="reject_span_attributes="):
        convergent.init(
            api_key="test-key",  # pragma: allowlist secret
            endpoint="https://example.test",
            release="r1",
            reject_span_attributes={"customer.id": object()},
            strict=True,
        )


# --- the environment variables -----------------------------------------------------


def test_the_environment_configures_both_filter_directions(
    start_sdk: Callable[..., InMemorySpanExporter],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no filter arguments, the two variables configure the filter, JSON
    decoded, through the same build path the arguments take."""
    monkeypatch.setenv("CONVERGENT_REQUIRE_SPAN_ATTRIBUTES", '{"customer.id": ["acme"]}')
    monkeypatch.setenv("CONVERGENT_REJECT_SPAN_ATTRIBUTES", '{"env": ["dev"]}')
    exporter = start_sdk()
    provider = _core.active_provider()
    assert provider is not None
    tracer = provider.get_tracer("their.framework")

    with _marked({"customer.id": "acme"}):
        with tracer.start_as_current_span("kept"):
            pass
    with _marked({"customer.id": "acme", "env": "dev"}):
        with tracer.start_as_current_span("rejected"):
            pass
    with tracer.start_as_current_span("unmarked"):
        pass

    assert _names(exporter) == {"kept"}


def test_a_filter_argument_beats_its_environment_variable(
    start_sdk: Callable[..., InMemorySpanExporter],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The argument wins the way api_key= wins over CONVERGENT_API_KEY."""
    monkeypatch.setenv("CONVERGENT_REQUIRE_SPAN_ATTRIBUTES", '{"customer.id": ["initech"]}')
    exporter = start_sdk(require_span_attributes={"customer.id": ["acme"]})
    provider = _core.active_provider()
    assert provider is not None
    tracer = provider.get_tracer("their.framework")

    with _marked({"customer.id": "acme"}):
        with tracer.start_as_current_span("argument wins"):
            pass
    with _marked({"customer.id": "initech"}):
        with tracer.start_as_current_span("variable lost"):
            pass

    assert _names(exporter) == {"argument wins"}


def test_a_malformed_filter_variable_disables_tracing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A variable that is not JSON takes the malformed-argument path: logged at
    ERROR, tracing disabled, nothing sent."""
    monkeypatch.delenv("CONVERGENT_STRICT", raising=False)
    monkeypatch.setenv("CONVERGENT_REQUIRE_SPAN_ATTRIBUTES", "customer.id=acme")
    exporter = InMemorySpanExporter()
    monkeypatch.setattr(
        _registry, "post_json", lambda *a, **k: {"deployment_id": "dep_test", "is_new": True}
    )
    monkeypatch.setattr(_transport, "build_processor", lambda **_: SimpleSpanProcessor(exporter))

    status = convergent.init(
        api_key="test-key",  # pragma: allowlist secret
        endpoint="https://example.test",
        release="r1",
    )

    assert status.enabled is False
    with convergent.span(name="support-agent", operation="agent_run"):
        pass
    assert _names(exporter) == set()


def test_a_malformed_filter_variable_raises_under_strict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CONVERGENT_REJECT_SPAN_ATTRIBUTES", "not json")
    with pytest.raises(ValueError, match="CONVERGENT_REJECT_SPAN_ATTRIBUTES"):
        convergent.init(
            api_key="test-key",  # pragma: allowlist secret
            endpoint="https://example.test",
            release="r1",
            strict=True,
        )


# --- the init() wiring -------------------------------------------------------------


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
        require_span_attributes={"customer.id": ["acme"]},
    )

    with convergent.span(
        name="billing-agent", operation="agent_run", context_attributes={"customer.id": "initech"}
    ):
        pass

    assert _names(theirs) == {"invoke_agent billing-agent"}
    assert _names(ours) == set()
    provider.shutdown()


def test_a_marked_child_of_an_unmarked_agent_span_is_sent(
    start_sdk: Callable[..., InMemorySpanExporter],
) -> None:
    """The agent filter wraps the policy filter, so its descent table records
    the agent span even when require_span_attributes= withholds it. A child started inside a
    marked block is then kept by both filters and sent."""
    exporter = start_sdk(
        agents=["support-agent"], require_span_attributes={"customer.id": ["acme"]}
    )
    provider = _core.active_provider()
    assert provider is not None
    tracer = provider.get_tracer("their.framework")

    with convergent.span(name="support-agent", operation="agent_run"):
        with _marked({"customer.id": "acme"}):
            with tracer.start_as_current_span("marked child"):
                pass

    assert _names(exporter) == {"marked child"}


def test_require_and_agents_together_must_both_pass(
    start_sdk: Callable[..., InMemorySpanExporter],
) -> None:
    """The two filters stack, so adding one can only send less."""
    exporter = start_sdk(
        agents=["support-agent"], require_span_attributes={"customer.id": ["acme"]}
    )
    provider = _core.active_provider()
    assert provider is not None

    with _marked({"customer.id": "acme"}):
        with convergent.span(name="support-agent", operation="agent_run"):
            with provider.get_tracer("their.orm").start_as_current_span("SELECT users"):
                pass
        with convergent.span(name="billing-agent", operation="agent_run"):
            pass
    with _marked({"customer.id": "initech"}):
        with convergent.span(name="support-agent", operation="agent_run"):
            pass

    assert _names(exporter) == {"invoke_agent support-agent", "SELECT users"}


def test_force_flush_reaches_every_destination_and_reports_the_worst_answer() -> None:
    refused = _Recorder(flush_answer=False)
    delivered = _Recorder()
    span_filter = _processors.FilterSpanProcessor(CUSTOMER_IS_ACME, [refused, delivered])

    assert span_filter.force_flush() is False
    assert "force_flush" in refused.calls
    assert "force_flush" in delivered.calls, "a False answer must not stop the flush"


def test_shutdown_reaches_every_destination() -> None:
    first, second = _Recorder(), _Recorder()
    span_filter = _processors.FilterSpanProcessor(CUSTOMER_IS_ACME, [first, second])

    span_filter.shutdown()

    assert "shutdown" in first.calls
    assert "shutdown" in second.calls


def test_wrap_returns_only_the_destination_chain() -> None:
    """The stamper is one module-level instance both entry points register, so
    ``wrap()`` returns only the filtered destination chain."""
    wrapped = _processors.wrap(None, ["support-agent"], [_Recorder()])

    assert len(wrapped) == 1
    assert isinstance(_processors._STAMPER, _processors.ContextAttributesSpanProcessor)


def test_a_second_init_with_a_different_filter_names_what_it_dropped(
    start_sdk: Callable[..., InMemorySpanExporter],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Every field on Status describes the configuration that won and reads
    healthy, so the loser's warning is the only place the discarded filter shows."""
    start_sdk(require_span_attributes={"customer.id": ["acme"]})

    with caplog.at_level(logging.WARNING, logger="convergent.sdk"):
        convergent.init(
            api_key="test-key",  # pragma: allowlist secret
            endpoint="https://example.test",
            release="r1",
            reject_span_attributes={"customer.id": ["initech"]},
        )

    kept = [r.message for r in caplog.records if "kept the first setup" in r.message]
    assert len(kept) == 1
    assert "the attributes it required or rejected" in kept[0]


def test_a_second_init_with_the_same_filters_reordered_drops_nothing(
    start_sdk: Callable[..., InMemorySpanExporter],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Conditions compare as a set, so two mappings that name the same rules in a
    different key order are one configuration, and the repeat init() warns about
    no dropped setting."""
    start_sdk(
        require_span_attributes={"customer.id": ["acme"], "env": ["prod"]},
        reject_span_attributes={"tier": ["internal"]},
    )

    with caplog.at_level(logging.WARNING, logger="convergent.sdk"):
        convergent.init(
            api_key="test-key",  # pragma: allowlist secret
            endpoint="https://example.test",
            release="r1",
            require_span_attributes={"env": ["prod"], "customer.id": ["acme"]},
            reject_span_attributes={"tier": ["internal"]},
        )

    assert not [r for r in caplog.records if "kept the first setup" in r.message]


def test_require_gates_a_file_destination(
    start_sdk: Callable[..., InMemorySpanExporter], tmp_path: Path
) -> None:
    """The filter sits in front of every SDK destination, a File included."""
    start_sdk(
        require_span_attributes={"customer.id": ["acme"]},
        destinations=[convergent.File(str(tmp_path))],
    )
    provider = _core.active_provider()
    assert provider is not None
    tracer = provider.get_tracer("their.framework")

    with _marked({"customer.id": "initech"}):
        with tracer.start_as_current_span("initech request"):
            pass
    convergent.flush(timeout_ms=5000)
    files = list(tmp_path.glob("*.jsonl"))
    withheld = all(not f.read_text().strip() for f in files)

    with _marked({"customer.id": "acme"}):
        with tracer.start_as_current_span("acme request"):
            pass
    convergent.flush(timeout_ms=5000)
    content = "".join(f.read_text() for f in tmp_path.glob("*.jsonl"))

    assert withheld
    assert "acme request" in content
    assert "initech request" not in content
