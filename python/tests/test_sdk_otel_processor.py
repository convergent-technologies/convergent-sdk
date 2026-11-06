"""``ConvergentSpanProcessor`` on a tracer provider the SDK never created."""

from __future__ import annotations

import logging
import tomllib
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path
from typing import Any, NamedTuple

import pytest
from opentelemetry import trace
from opentelemetry.attributes import BoundedAttributes
from opentelemetry.context import Context
from opentelemetry.sdk.trace import ReadableSpan, Span, SpanLimits, SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.util import BoundedList
from opentelemetry.trace import format_span_id

import convergent
import convergent.otel
from convergent.otel import ConvergentSpanProcessor
from convergent import _core, _otel, _processors, _transport, _version

KEY = "test-key"  # pragma: allowlist secret
ENDPOINT = "https://example.test"


class Installed(NamedTuple):
    provider: TracerProvider
    processor: ConvergentSpanProcessor


@pytest.fixture(autouse=True)
def reset_sdk() -> Iterator[None]:
    _reset_otel()
    _core._reset_for_tests()
    yield
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


def _serve(
    monkeypatch: pytest.MonkeyPatch, exporter: InMemorySpanExporter, *, batch: bool = False
) -> None:
    """Send every export to ``exporter``, through the SDK's own Convergent exporter."""
    convergent_exporter = _transport._LossCountingExporter(exporter, None, None)
    # Only a real BatchSpanProcessor carries the private queue state flush() reads.
    processor = (
        BatchSpanProcessor(convergent_exporter)
        if batch
        else SimpleSpanProcessor(convergent_exporter)
    )
    monkeypatch.setattr(_transport, "build_processor", lambda **_: processor)


def _record(span: ReadableSpan) -> tuple[str, Mapping[str, object]]:
    """One span as the pair the parity test compares."""
    attributes: dict[str, object] = dict(span.attributes or {})
    context = span.get_span_context()
    # IDs and microsecond clocks are expected to differ between the two runs;
    # compare the relationships and the value shapes instead.
    attributes["convergent.invocation.span_id"] = context is not None and attributes.get(
        "convergent.invocation.span_id"
    ) == format_span_id(context.span_id)
    attributes["convergent.root_invocation.span_id"] = isinstance(
        attributes.get("convergent.root_invocation.span_id"), str
    )
    attributes["convergent.parent_invocation.span_id"] = isinstance(
        attributes.get("convergent.parent_invocation.span_id"), str
    )
    for key in (
        "convergent.invocation.start_time_unix_micros",
        "convergent.root_invocation.start_time_unix_micros",
    ):
        value = attributes.get(key)
        attributes[key] = isinstance(value, int) and value > 0
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
    install()
    _run_one_agent()

    assert expected, "the init() path recorded nothing, so the comparison proves nothing"
    assert [_record(span) for span in through_processor.get_finished_spans()] == expected


_STAMP_PREFIXES = (
    "convergent.invocation.",
    "convergent.root_invocation.",
    "convergent.parent_invocation.",
)


#: What a span names when both distributions are installed: convergent-sdk wins.
_SDK_VERSION = "python/7.7.7"


@pytest.fixture
def pinned_sdk_version(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    installed = {"convergent-sdk": "7.7.7", "convergent": "1.1.1"}

    def fake_version(distribution: str) -> str:
        if distribution not in installed:
            raise PackageNotFoundError(distribution)
        return installed[distribution]

    monkeypatch.setattr(_version, "version", fake_version)
    _transport._sdk_version_label.cache_clear()
    yield
    _transport._sdk_version_label.cache_clear()


@pytest.mark.usefixtures("pinned_sdk_version")
def test_real_sdk_stamps_sibling_and_nested_invocations_at_start(
    monkeypatch: pytest.MonkeyPatch, install: Callable[..., Installed]
) -> None:
    """Each span carries its current invocation, root, and parent invocation."""
    exporter = InMemorySpanExporter()
    _serve(monkeypatch, exporter)
    provider = install(agents=["planner", "researcher"]).provider
    tracer = provider.get_tracer("convergent.sdk")

    with tracer.start_as_current_span(
        "planner",
        attributes={"gen_ai.operation.name": "invoke_agent", "gen_ai.agent.name": "planner"},
    ):
        with tracer.start_as_current_span("planner child"):
            pass
        with tracer.start_as_current_span("late LangChain chain") as chain:
            chain.set_attribute("gen_ai.operation.name", "invoke_agent")
            with tracer.start_as_current_span("late chain child"):
                pass
        with tracer.start_as_current_span(
            "researcher",
            attributes={
                "gen_ai.operation.name": "invoke_agent",
                "gen_ai.agent.name": "researcher",
            },
        ):
            with tracer.start_as_current_span("researcher child"):
                pass
        with tracer.start_as_current_span("planner sibling"):
            pass

    by_name = {span.name: span for span in exporter.get_finished_spans()}
    assert set(by_name) == {
        "planner",
        "planner child",
        "late LangChain chain",
        "late chain child",
        "researcher",
        "researcher child",
        "planner sibling",
    }

    def own_id(name: str) -> str:
        span_context = by_name[name].get_span_context()
        assert span_context is not None
        return format_span_id(span_context.span_id)

    def attrs(name: str) -> Mapping[str, object]:
        return by_name[name].attributes or {}

    planner_start_ns = by_name["planner"].start_time
    researcher_start_ns = by_name["researcher"].start_time
    assert planner_start_ns is not None
    assert researcher_start_ns is not None
    planner_start = planner_start_ns // 1_000
    researcher_start = researcher_start_ns // 1_000
    planner_spans = (
        "planner",
        "planner child",
        "late LangChain chain",
        "late chain child",
        "planner sibling",
    )
    for name in planner_spans:
        assert attrs(name)["convergent.invocation.span_id"] == own_id("planner")
        assert attrs(name)["convergent.invocation.start_time_unix_micros"] == planner_start
        assert attrs(name)["convergent.root_invocation.span_id"] == own_id("planner")
        assert attrs(name)["convergent.root_invocation.start_time_unix_micros"] == planner_start
        assert "convergent.parent_invocation.span_id" not in attrs(name)
    for name in by_name:
        assert "convergent.invocation.stamp_failed" not in attrs(name)
    assert attrs("researcher")["convergent.invocation.span_id"] == own_id("researcher")
    assert attrs("researcher")["convergent.invocation.start_time_unix_micros"] == researcher_start
    assert attrs("researcher")["convergent.root_invocation.span_id"] == own_id("planner")
    assert attrs("researcher")["convergent.root_invocation.start_time_unix_micros"] == planner_start
    assert attrs("researcher")["convergent.parent_invocation.span_id"] == own_id("planner")
    assert attrs("researcher child")["convergent.invocation.span_id"] == own_id("researcher")
    assert "convergent.parent_invocation.span_id" not in attrs("researcher child")
    for name in by_name:
        assert attrs(name)["convergent.sdk.version"] == _SDK_VERSION


def test_spans_ingest_links_as_agent_runs_without_an_operation_are_invocations(
    monkeypatch: pytest.MonkeyPatch, install: Callable[..., Installed]
) -> None:
    """Ingest's linker treats a span that names an agent but sets no operation
    as an agent run, and so does pydantic-ai's ``agent <name>`` span. Stamps
    must split trajectories the same way. A span with another operation that
    also names its agent stays inside the current invocation."""
    exporter = InMemorySpanExporter()
    _serve(monkeypatch, exporter)
    provider = install().provider
    tracer = provider.get_tracer("convergent.sdk")
    framework = provider.get_tracer("some.framework")
    pydantic_ai = provider.get_tracer("pydantic-ai")

    with tracer.start_as_current_span(
        "outer", attributes={"gen_ai.operation.name": "invoke_agent"}
    ):
        with framework.start_as_current_span(
            "named agent", attributes={"gen_ai.agent.name": "inner"}
        ):
            with framework.start_as_current_span(
                "model call",
                attributes={"gen_ai.operation.name": "chat", "gen_ai.agent.name": "inner"},
            ):
                pass
        with pydantic_ai.start_as_current_span(
            "agent support", attributes={"agent.name": "support"}
        ):
            pass

    by_name = {span.name: span for span in exporter.get_finished_spans()}

    def own_id(name: str) -> str:
        span_context = by_name[name].get_span_context()
        assert span_context is not None
        return format_span_id(span_context.span_id)

    def attrs(name: str) -> Mapping[str, object]:
        return by_name[name].attributes or {}

    for name in ("named agent", "agent support"):
        assert attrs(name)["convergent.invocation.span_id"] == own_id(name)
        assert attrs(name)["convergent.parent_invocation.span_id"] == own_id("outer")
        assert attrs(name)["convergent.root_invocation.span_id"] == own_id("outer")
    assert attrs("model call")["convergent.invocation.span_id"] == own_id("named agent")


def test_other_instrumentors_agent_markings_start_no_invocation(
    monkeypatch: pytest.MonkeyPatch, install: Callable[..., Installed]
) -> None:
    """Ingest's canonical path reads neither OpenInference's span kinds nor
    OpenLLMetry's, so a span marked as an agent by either stays inside the
    current invocation, as ingest places it."""
    exporter = InMemorySpanExporter()
    _serve(monkeypatch, exporter)
    provider = install().provider
    tracer = provider.get_tracer("convergent.sdk")
    framework = provider.get_tracer("some.framework")

    with tracer.start_as_current_span(
        "outer", attributes={"gen_ai.operation.name": "invoke_agent"}
    ):
        for name, attributes in (
            ("openinference agent", {"openinference.span.kind": "AGENT"}),
            ("openinference chain", {"openinference.span.kind": "CHAIN"}),
            ("traceloop agent", {"traceloop.span.kind": "agent"}),
            ("traceloop entity", {"traceloop.entity.name": "support"}),
        ):
            with framework.start_as_current_span(name, attributes=attributes):
                pass

    by_name = {span.name: span for span in exporter.get_finished_spans()}
    outer = by_name["outer"].get_span_context()
    assert outer is not None
    outer_id = format_span_id(outer.span_id)
    for name in (
        "openinference agent",
        "openinference chain",
        "traceloop agent",
        "traceloop entity",
    ):
        attributes = by_name[name].attributes or {}
        assert attributes["convergent.invocation.span_id"] == outer_id, name
        assert "convergent.parent_invocation.span_id" not in attributes, name


@pytest.mark.usefixtures("pinned_sdk_version")
def test_spans_under_a_remote_parent_without_a_received_stamp_stay_unstamped(
    monkeypatch: pytest.MonkeyPatch, install: Callable[..., Installed]
) -> None:
    """A caller on an older SDK, another language, or a hop that drops
    ``tracestate`` sends no stamp. The receiving agent cannot know where it sits
    in the caller's trajectory, so its whole subtree is left for ingest to link."""
    exporter = InMemorySpanExporter()
    _serve(monkeypatch, exporter)
    provider = install().provider
    tracer = provider.get_tracer("convergent.sdk")
    remote = trace.NonRecordingSpan(
        trace.SpanContext(
            trace_id=0x0AF7651916CD43DD8448EB211C80319C,
            span_id=0xB7AD6B7169203331,
            is_remote=True,
            trace_flags=trace.TraceFlags(trace.TraceFlags.SAMPLED),
        )
    )

    with tracer.start_as_current_span(
        "receiving agent",
        context=trace.set_span_in_context(remote),
        attributes={"gen_ai.operation.name": "invoke_agent"},
    ):
        with tracer.start_as_current_span("child"):
            pass
        with tracer.start_as_current_span(
            "nested agent", attributes={"gen_ai.operation.name": "invoke_agent"}
        ):
            with tracer.start_as_current_span("nested child"):
                pass

    spans = exporter.get_finished_spans()
    assert {span.name for span in spans} == {
        "receiving agent",
        "child",
        "nested agent",
        "nested child",
    }
    for span in spans:
        assert not [key for key in span.attributes or {} if key.startswith(_STAMP_PREFIXES)], (
            span.name
        )
        assert (span.attributes or {})["convergent.sdk.version"] == _SDK_VERSION, span.name


@pytest.mark.usefixtures("pinned_sdk_version")
def test_spans_under_a_local_parent_started_before_install_stay_unstamped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An agent already running when ``install`` attaches to the caller's provider
    never passed through the stamper, so where its children sit is as unknown as
    under a remote parent. A span started after install with no agent above it is
    still a known root."""
    exporter = InMemorySpanExporter()
    _serve(monkeypatch, exporter)
    provider = TracerProvider()
    tracer = provider.get_tracer("customer")
    try:
        with tracer.start_as_current_span(
            "early agent", attributes={"gen_ai.operation.name": "invoke_agent"}
        ):
            convergent.otel.install(provider, api_key=KEY, endpoint=ENDPOINT, release="r1")
            with tracer.start_as_current_span(
                "nested agent", attributes={"gen_ai.operation.name": "invoke_agent"}
            ):
                with tracer.start_as_current_span("nested child"):
                    pass
        with tracer.start_as_current_span("request"):
            with tracer.start_as_current_span(
                "later agent", attributes={"gen_ai.operation.name": "invoke_agent"}
            ):
                pass
    finally:
        provider.shutdown()

    by_name = {span.name: span for span in exporter.get_finished_spans()}
    for name in ("early agent", "nested agent", "nested child", "request"):
        attributes = by_name[name].attributes or {}
        assert not [key for key in attributes if key.startswith(_STAMP_PREFIXES)], name
    later = by_name["later agent"]
    later_context = later.get_span_context()
    assert later_context is not None
    attributes = later.attributes or {}
    assert attributes["convergent.root_invocation.span_id"] == format_span_id(later_context.span_id)
    assert "convergent.parent_invocation.span_id" not in attributes


@pytest.mark.usefixtures("pinned_sdk_version")
def test_a_span_with_no_agent_above_it_names_the_sdk_without_a_stamp(
    monkeypatch: pytest.MonkeyPatch, install: Callable[..., Installed]
) -> None:
    """Ingest files a leftover span under its trace's run only when it can tell
    the span came from a stamping SDK, so every exported span names the SDK."""
    exporter = InMemorySpanExporter()
    _serve(monkeypatch, exporter)
    tracer = install().provider.get_tracer("convergent.sdk")

    with tracer.start_as_current_span("request"):
        with tracer.start_as_current_span("db query"):
            pass

    spans = exporter.get_finished_spans()
    assert {span.name for span in spans} == {"request", "db query"}
    for span in spans:
        attributes = span.attributes or {}
        assert not [key for key in attributes if key.startswith(_STAMP_PREFIXES)], span.name
        assert attributes["convergent.sdk.version"] == _SDK_VERSION, span.name


@pytest.mark.usefixtures("pinned_sdk_version")
def test_every_span_sent_to_convergent_names_the_sdk(
    monkeypatch: pytest.MonkeyPatch, install: Callable[..., Installed]
) -> None:
    """Catches an export path that stops adding the version, or adds it only to
    stamped spans: ingest tells a stamping SDK's spans apart by this key, so a
    span without it is filed as if an old SDK sent it."""
    exporter = InMemorySpanExporter()
    _serve(monkeypatch, exporter)
    tracer = install().provider.get_tracer("convergent.sdk")

    with tracer.start_as_current_span(
        "agent", attributes={"gen_ai.operation.name": "invoke_agent"}
    ):
        with tracer.start_as_current_span("tool"):
            pass
    with tracer.start_as_current_span("no agent above"):
        pass

    spans = {span.name: span.attributes or {} for span in exporter.get_finished_spans()}
    assert set(spans) == {"agent", "tool", "no agent above"}
    for name, attributes in spans.items():
        assert attributes["convergent.sdk.version"] == _SDK_VERSION, name
    for name in ("agent", "tool"):
        assert "convergent.invocation.span_id" in spans[name], name


def test_every_exported_span_names_the_python_sdk_that_is_installed(
    monkeypatch: pytest.MonkeyPatch, install: Callable[..., Installed]
) -> None:
    """Catches a wrong or missing ``python/`` version on the real lookup, unpinned.

    The expected version is the installed version of the distribution this
    checkout's ``pyproject.toml`` names, not what the SDK's own lookup answers,
    so the test cannot pass by the SDK repeating its own mistake (edge case 17:
    in the monorepo that is the checkout's dev version, which no shared scenario
    lists).
    """
    # The nearest pyproject.toml: the repo root in the monorepo, ``python/`` in
    # the public SDK copy, where these tests sit one level lower.
    pyproject = next(
        parent / "pyproject.toml"
        for parent in Path(__file__).resolve().parents
        if (parent / "pyproject.toml").is_file()
    )
    project = tomllib.loads(pyproject.read_text())["project"]["name"]
    expected = f"python/{distribution(project).version}"
    exporter = InMemorySpanExporter()
    _serve(monkeypatch, exporter)
    tracer = install().provider.get_tracer("convergent.sdk")

    _transport._sdk_version_label.cache_clear()
    try:
        _run_one_agent()
        with tracer.start_as_current_span("no agent above"):
            pass
    finally:
        _transport._sdk_version_label.cache_clear()

    spans = exporter.get_finished_spans()
    assert len(spans) == 3
    for span in spans:
        assert (span.attributes or {})["convergent.sdk.version"] == expected, span.name


@pytest.mark.usefixtures("pinned_sdk_version")
def test_a_customer_exporter_on_the_same_provider_never_sees_the_sdk_version(
    monkeypatch: pytest.MonkeyPatch, install: Callable[..., Installed]
) -> None:
    """Catches the version going back onto the shared span object, where every
    exporter the customer attached to the provider would ship it too."""
    ours = InMemorySpanExporter()
    _serve(monkeypatch, ours)
    provider = install().provider
    theirs = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(theirs))
    tracer = provider.get_tracer("convergent.sdk")

    with tracer.start_as_current_span(
        "agent", attributes={"gen_ai.operation.name": "invoke_agent"}
    ):
        with tracer.start_as_current_span("tool"):
            pass

    assert {span.name for span in ours.get_finished_spans()} == {"agent", "tool"}
    for span in ours.get_finished_spans():
        assert (span.attributes or {})["convergent.sdk.version"] == _SDK_VERSION, span.name
    assert {span.name for span in theirs.get_finished_spans()} == {"agent", "tool"}
    for span in theirs.get_finished_spans():
        assert "convergent.sdk.version" not in (span.attributes or {}), span.name
        assert "convergent.invocation.span_id" in (span.attributes or {}), span.name


@pytest.mark.usefixtures("pinned_sdk_version")
def test_a_span_at_the_attribute_limit_still_names_the_sdk_on_export(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches a version that is added only when it fits under the span
    attribute limit, or that evicts one of the caller's attributes to fit."""
    ours = InMemorySpanExporter()
    _serve(monkeypatch, ours)
    provider = TracerProvider(span_limits=SpanLimits(max_attributes=2))
    convergent.otel.install(provider, api_key=KEY, endpoint=ENDPOINT, release="r1")
    theirs = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(theirs))
    tracer = provider.get_tracer("convergent.sdk")

    with tracer.start_as_current_span("full", attributes={"a": 1, "b": 2}):
        pass

    (sent,) = ours.get_finished_spans()
    assert dict(sent.attributes or {}) == {"a": 1, "b": 2, "convergent.sdk.version": _SDK_VERSION}
    (shared,) = theirs.get_finished_spans()
    assert dict(shared.attributes or {}) == {"a": 1, "b": 2}
    provider.shutdown()


@pytest.mark.usefixtures("pinned_sdk_version")
def test_a_stamp_pushed_out_after_start_is_written_back_on_export(
    monkeypatch: pytest.MonkeyPatch, install: Callable[..., Installed]
) -> None:
    """Catches a span that fills the attribute limit after it starts, such as a
    model span flattening its messages: OpenTelemetry evicts the oldest keys,
    the stamp among them, while the export copy still names the SDK, so ingest
    would take the span for a stamping SDK's span that has no stamp."""
    exporter = InMemorySpanExporter()
    _serve(monkeypatch, exporter)
    tracer = install().provider.get_tracer("convergent.sdk")

    with tracer.start_as_current_span(
        "agent", attributes={"gen_ai.operation.name": "invoke_agent"}
    ) as agent:
        with tracer.start_as_current_span("model") as model:
            for index in range(200):
                model.set_attribute(f"llm.input_messages.{index}.content", "x")
        for index in range(200):
            agent.set_attribute(f"llm.output_messages.{index}.content", "y")

    spans = {span.name: span for span in exporter.get_finished_spans()}
    assert set(spans) == {"agent", "model"}
    agent_context = spans["agent"].context
    assert agent_context is not None
    agent_id = format_span_id(agent_context.span_id)
    agent_start = (spans["agent"].start_time or 0) // 1_000
    expected = {
        "convergent.invocation.span_id": agent_id,
        "convergent.invocation.start_time_unix_micros": agent_start,
        "convergent.root_invocation.span_id": agent_id,
        "convergent.root_invocation.start_time_unix_micros": agent_start,
    }
    for name, span in spans.items():
        attributes = span.attributes or {}
        assert span.dropped_attributes > 0, name
        assert {key: attributes.get(key) for key in expected} == expected, name
        assert "convergent.parent_invocation.span_id" not in attributes, name
        assert "convergent.invocation.stamp_failed" not in attributes, name
        assert attributes["convergent.sdk.version"] == _SDK_VERSION, name


@pytest.mark.usefixtures("pinned_sdk_version")
def test_a_span_too_full_for_its_stamp_still_carries_it_on_export() -> None:
    """Catches a span that starts too full for its stamp failing itself and its
    subtree. With 125 attributes under the default limit of 128, the four stamp
    keys do not fit, so only the write to the shared span is skipped: the span
    keeps every caller attribute, and the export copy, which has no limit,
    carries the stamp for the span and its child.

    Only the stampers run, so the span holds exactly its start attributes.
    """
    ours, theirs = InMemorySpanExporter(), InMemorySpanExporter()
    provider = TracerProvider()
    convergent_export = SimpleSpanProcessor(_transport._LossCountingExporter(ours, None, None))
    for processor in [_processors._STAMPER, *_processors.wrap(None, None, (convergent_export,))]:
        provider.add_span_processor(processor)
    provider.add_span_processor(SimpleSpanProcessor(theirs))
    tracer = provider.get_tracer("convergent.sdk")
    start = {"gen_ai.operation.name": "invoke_agent"} | {f"caller.{i}": i for i in range(124)}

    with tracer.start_as_current_span("full agent", attributes=start):
        with tracer.start_as_current_span("child"):
            pass

    shared = {span.name: dict(span.attributes or {}) for span in theirs.get_finished_spans()}
    assert shared["full agent"] == start
    sent = {span.name: span for span in ours.get_finished_spans()}
    assert set(sent) == {"full agent", "child"}
    agent_context = sent["full agent"].context
    assert agent_context is not None
    agent_id = format_span_id(agent_context.span_id)
    agent_start = (sent["full agent"].start_time or 0) // 1_000
    expected = {
        "convergent.invocation.span_id": agent_id,
        "convergent.invocation.start_time_unix_micros": agent_start,
        "convergent.root_invocation.span_id": agent_id,
        "convergent.root_invocation.start_time_unix_micros": agent_start,
    }
    for name, span in sent.items():
        attributes = dict(span.attributes or {})
        stamp = {key: value for key, value in attributes.items() if key.startswith(_STAMP_PREFIXES)}
        assert stamp == expected, name
        assert attributes["convergent.sdk.version"] == _SDK_VERSION, name
    assert start.items() <= dict(sent["full agent"].attributes or {}).items()
    provider.shutdown()


@pytest.mark.usefixtures("pinned_sdk_version")
def test_a_failed_stamp_with_no_room_for_its_flag_is_flagged_on_export(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches a failed stamp whose flag did not fit under the limit: without the
    flag, the export copy names the SDK and ingest files the span as unstamped.

    Only the stampers run, so the span holds exactly its two start attributes;
    the SDK's semantic keys would otherwise evict the operation first. A full
    span alone no longer fails, so the stamper is made to raise.

    Also catches a failed stamp dropping its child, or stamping it: every span
    still exports, with no stamp on the children.
    """

    def broken(span: Span) -> bool:
        raise RuntimeError("stamper failed")

    monkeypatch.setattr(_processors, "_is_invocation", broken)
    ours, theirs = InMemorySpanExporter(), InMemorySpanExporter()
    provider = TracerProvider(span_limits=SpanLimits(max_attributes=2))
    convergent_export = SimpleSpanProcessor(_transport._LossCountingExporter(ours, None, None))
    for processor in [_processors._STAMPER, *_processors.wrap(None, None, (convergent_export,))]:
        provider.add_span_processor(processor)
    provider.add_span_processor(SimpleSpanProcessor(theirs))
    tracer = provider.get_tracer("convergent.sdk")

    with tracer.start_as_current_span(
        "full agent", attributes={"gen_ai.operation.name": "invoke_agent", "a": 1}
    ):
        with tracer.start_as_current_span("child"):
            pass

    shared = {span.name: dict(span.attributes or {}) for span in theirs.get_finished_spans()}
    assert shared["full agent"] == {"gen_ai.operation.name": "invoke_agent", "a": 1}
    assert shared["child"] == {"convergent.invocation.stamp_failed": True}
    sent = {span.name: dict(span.attributes or {}) for span in ours.get_finished_spans()}
    assert set(sent) == {"full agent", "child"}
    assert sent["full agent"]["gen_ai.operation.name"] == "invoke_agent"
    assert sent["full agent"]["a"] == 1
    for name, attributes in sent.items():
        assert attributes["convergent.invocation.stamp_failed"] is True, name
        assert attributes["convergent.sdk.version"] == _SDK_VERSION, name
        assert not [
            key
            for key in attributes
            if key.startswith(_STAMP_PREFIXES) and key != "convergent.invocation.stamp_failed"
        ], name
    provider.shutdown()


def test_a_stamp_that_exactly_fills_the_attribute_limit_is_written() -> None:
    exporter = InMemorySpanExporter()
    # A root invocation adds four placement keys to its operation.
    provider = TracerProvider(span_limits=SpanLimits(max_attributes=5))
    for processor in [_processors._STAMPER, *_processors.wrap(None, None, ())]:
        provider.add_span_processor(processor)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("convergent.sdk")

    with tracer.start_as_current_span(
        "agent", attributes={"gen_ai.operation.name": "invoke_agent"}
    ):
        pass

    (span,) = exporter.get_finished_spans()
    attributes = span.attributes or {}
    assert len(attributes) == 5
    assert attributes["gen_ai.operation.name"] == "invoke_agent"
    assert "convergent.invocation.stamp_failed" not in attributes
    assert (
        attributes["convergent.root_invocation.span_id"]
        == attributes["convergent.invocation.span_id"]
    )
    provider.shutdown()


def test_a_stamp_past_the_attribute_limit_leaves_the_shared_span_alone(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A stamp that would evict the caller's attributes or context marks is not
    written to the shared span, and the span is not failed: the export copy
    carries the stamp instead. Each span here is too full for its stamp."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider(span_limits=SpanLimits(max_attributes=6))
    for processor in [_processors._STAMPER, *_processors.wrap(None, None, ())]:
        provider.add_span_processor(processor)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("convergent.sdk")

    with caplog.at_level(logging.WARNING):
        with _marked(
            {
                "customer.id": "cust-1",
                "tenant.id": "tenant-1",
                "request.id": "request-1",
            }
        ):
            with tracer.start_as_current_span(
                "agent",
                attributes={
                    "gen_ai.operation.name": "invoke_agent",
                    "user.id": "user-1",
                    _processors.CONVERGENT_SESSION_ID: "session-1",
                },
            ):
                with tracer.start_as_current_span("child"):
                    pass
            with tracer.start_as_current_span(
                "roomy agent", attributes={"gen_ai.operation.name": "invoke_agent"}
            ):
                pass

    spans = {span.name: span for span in exporter.get_finished_spans()}
    assert set(spans) == {"agent", "child", "roomy agent"}
    agent_attributes = spans["agent"].attributes or {}
    assert agent_attributes["convergent.attributes.customer.id"] == "cust-1"
    assert agent_attributes["convergent.attributes.tenant.id"] == "tenant-1"
    assert agent_attributes["convergent.attributes.request.id"] == "request-1"
    assert agent_attributes["user.id"] == "user-1"
    assert agent_attributes[_processors.CONVERGENT_SESSION_ID] == "session-1"
    assert (spans["child"].attributes or {})["convergent.attributes.customer.id"] == "cust-1"
    roomy_attributes = spans["roomy agent"].attributes or {}
    assert roomy_attributes["convergent.attributes.customer.id"] == "cust-1"
    for span in spans.values():
        assert not [key for key in span.attributes or {} if key.startswith(_STAMP_PREFIXES)]
    assert not [r for r in caplog.records if "could not stamp trajectory identity" in r.message]
    provider.shutdown()


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
    monkeypatch.setattr(_transport, "build_processor", lambda **_: SimpleSpanProcessor(first))
    installed = install()

    second = InMemorySpanExporter()
    monkeypatch.setattr(_transport, "build_processor", lambda **_: SimpleSpanProcessor(second))
    with caplog.at_level(logging.WARNING, logger="convergent.sdk"):
        convergent.otel.install(installed.provider, api_key=KEY, endpoint=ENDPOINT, release="r1")

    with convergent.span(name="support-agent", operation="agent_run"):
        pass

    assert len(first.get_finished_spans()) == 1, "one span out, not two"
    assert second.get_finished_spans() == ()
    assert [r for r in caplog.records if "sends nothing" in r.message]


def test_init_after_a_processor_keeps_the_processors_configuration(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    install: Callable[..., Installed],
) -> None:
    """The other order. ``init()`` is the one that loses, and the processor's
    setup keeps running."""
    exporter = InMemorySpanExporter()
    monkeypatch.setattr(_transport, "build_processor", lambda **_: SimpleSpanProcessor(exporter))
    install()

    with caplog.at_level(logging.WARNING, logger="convergent.sdk"):
        status = convergent.init(api_key=KEY, endpoint=ENDPOINT, release="r1")

    with convergent.span(name="gpt", operation="model_call") as call:
        call.set_input("what is my account number")

    assert status.mode == "attached"
    assert len(exporter.get_finished_spans()) == 1, "one span out, not two"
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

        def on_start(self, span: Span, parent_context: Context | None = None) -> None:
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


def test_the_opentelemetry_private_span_fields_the_sdk_reads_exist() -> None:
    """The stamp's write-back (``_processors.trajectory_stamp``, ``_fits``) and the
    export copy (``_transport._naming_the_sdk``) read OpenTelemetry's private span
    fields. A release that renames one would turn them into silent no-ops, so this
    fails first."""
    provider = TracerProvider()
    span = provider.get_tracer("their.app").start_span("step")
    assert isinstance(span, Span)
    attributes = span._attributes
    assert isinstance(attributes, BoundedAttributes)
    assert isinstance(attributes.maxlen, int)
    _processors._keep_for_export(span, {"convergent.invocation.span_id": "a" * 16})
    span.end()
    assert isinstance(span._events, BoundedList)
    assert isinstance(span._links, BoundedList)
    assert _processors.trajectory_stamp(span) == {"convergent.invocation.span_id": "a" * 16}
    provider.shutdown()


@pytest.mark.usefixtures("pinned_sdk_version")
def test_a_stamp_written_back_on_export_is_not_counted_dropped(
    monkeypatch: pytest.MonkeyPatch, install: Callable[..., Installed]
) -> None:
    """Catches the export copy keeping the span's drop count after it writes the
    evicted stamp keys back: those keys arrive, so only the others were lost."""
    ours = InMemorySpanExporter()
    _serve(monkeypatch, ours)
    provider = install().provider
    theirs = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(theirs))
    tracer = provider.get_tracer("convergent.sdk")

    with tracer.start_as_current_span(
        "agent", attributes={"gen_ai.operation.name": "invoke_agent"}
    ) as agent:
        for index in range(200):
            agent.set_attribute(f"llm.output_messages.{index}.content", "y")

    (original,) = theirs.get_finished_spans()
    (copy,) = ours.get_finished_spans()
    stamp_keys = {
        "convergent.invocation.span_id",
        "convergent.invocation.start_time_unix_micros",
        "convergent.root_invocation.span_id",
        "convergent.root_invocation.start_time_unix_micros",
    }
    assert not stamp_keys & set(original.attributes or {}), "the stamp was evicted"
    assert stamp_keys <= set(copy.attributes or {}), "and written back on the copy"
    assert copy.dropped_attributes == original.dropped_attributes - len(stamp_keys)
