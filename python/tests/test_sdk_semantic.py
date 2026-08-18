from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
import threading
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Generator, Iterator
from typing import Any, cast

import pytest
from opentelemetry import baggage, trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import (
    INVALID_SPAN_CONTEXT,
    NonRecordingSpan,
    SpanContext,
    TraceFlags,
)

import convergent
from convergent import _core, _registry, _semantic, _transport


@pytest.fixture
def start_sdk(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[..., InMemorySpanExporter]]:
    providers: list[TracerProvider] = []

    def start(
        *,
        release: str = "r1",
    ) -> InMemorySpanExporter:
        _reset_otel()
        _core._reset_for_tests()
        exporter = InMemorySpanExporter()
        monkeypatch.setattr(
            _registry,
            "post_json",
            lambda *a, **k: {"deployment_id": "dep_test", "is_new": True},
        )
        monkeypatch.setattr(
            _transport,
            "build_processor",
            lambda **_: SimpleSpanProcessor(exporter),
        )
        convergent.init(
            api_key="test-key",  # pragma: allowlist secret
            endpoint="https://example.test",
            release=release,
        )
        provider = _core.active_provider()
        assert provider is not None
        providers.append(provider)
        return exporter

    yield start
    for provider in providers:
        provider.shutdown()
    _core._reset_for_tests()
    _reset_otel()


@pytest.fixture
def disabled_sdk(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv("CONVERGENT_API_KEY", raising=False)
    monkeypatch.delenv("CONVERGENT_ENDPOINT", raising=False)
    _reset_otel()
    _core._reset_for_tests()
    yield
    _core._reset_for_tests()
    _reset_otel()


def _reset_otel() -> None:
    trace_module = trace
    trace_module._TRACER_PROVIDER_SET_ONCE._done = False  # type: ignore[attr-defined]
    trace_module._TRACER_PROVIDER = None  # type: ignore[attr-defined]


def test_observe_preserves_sync_signature_result_and_exception(
    start_sdk: Callable[..., InMemorySpanExporter],
) -> None:
    exporter = start_sdk()

    def work(value: int, suffix: str = "!") -> str:
        return f"{value}{suffix}"

    observed = convergent.observe(name="work", operation="tool_call")(work)
    assert inspect.signature(observed) == inspect.signature(work)
    assert observed(3) == "3!"

    def broken() -> None:
        raise RuntimeError("application failure")

    with pytest.raises(RuntimeError, match="application failure") as error:
        convergent.observe(name="broken", operation="tool_call")(broken)()
    spans = exporter.get_finished_spans()
    assert error.value.args == ("application failure",)
    assert spans[-1].status.is_ok is False


def test_observe_supports_async_functions_and_preserves_result(
    start_sdk: Callable[..., InMemorySpanExporter],
) -> None:
    exporter = start_sdk()

    async def work(value: int) -> int:
        return value + 1

    observed = convergent.observe(name="work", operation="model_call")(work)
    assert inspect.iscoroutinefunction(observed)
    assert asyncio.run(observed(4)) == 5
    attributes = exporter.get_finished_spans()[-1].attributes
    assert attributes is not None
    assert attributes["gen_ai.operation.name"] == "chat"


def test_observe_keeps_sync_and_async_generators_open_during_iteration(
    start_sdk: Callable[..., InMemorySpanExporter],
) -> None:
    exporter = start_sdk()

    @convergent.observe(name="stream", operation="model_call")
    def stream() -> Iterator[int]:
        yield 1
        yield 2

    async def async_source() -> AsyncIterator[int]:
        yield 3
        yield 4

    observed_async = convergent.observe(name="async-stream", operation="model_call")(async_source)
    assert list(stream()) == [1, 2]

    async def consume() -> list[int]:
        return [item async for item in observed_async()]

    assert asyncio.run(consume()) == [3, 4]
    spans = exporter.get_finished_spans()
    assert [span.name for span in spans] == ["stream", "async-stream"]


def test_generator_closed_early_does_not_mark_span_as_error(
    start_sdk: Callable[..., InMemorySpanExporter],
) -> None:
    exporter = start_sdk()

    @convergent.observe(name="stream", operation="model_call")
    def stream() -> Generator[int, None, None]:
        yield 1
        yield 2

    generator = stream()
    next(generator)
    generator.close()

    spans = exporter.get_finished_spans()
    assert spans[-1].name == "stream"
    assert spans[-1].status.is_ok is True


def test_async_generator_closed_early_does_not_mark_span_as_error(
    start_sdk: Callable[..., InMemorySpanExporter],
) -> None:
    exporter = start_sdk()

    @convergent.observe(name="async-stream", operation="model_call")
    async def stream() -> AsyncGenerator[int, None]:
        yield 1
        yield 2

    async def consume_one_then_abandon() -> None:
        generator = stream()
        await generator.__anext__()
        await generator.aclose()

    asyncio.run(consume_one_then_abandon())

    spans = exporter.get_finished_spans()
    assert spans[-1].name == "async-stream"
    assert spans[-1].status.is_ok is True


def test_cancelled_task_does_not_mark_span_as_error(
    start_sdk: Callable[..., InMemorySpanExporter],
) -> None:
    exporter = start_sdk()

    @convergent.observe(name="work", operation="model_call")
    async def work() -> None:
        await asyncio.sleep(10)

    async def run_and_cancel() -> None:
        task = asyncio.ensure_future(work())
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run_and_cancel())

    spans = exporter.get_finished_spans()
    assert spans[-1].name == "work"
    assert spans[-1].status.is_ok is True


def test_two_agents_in_one_process_share_one_tracer_provider(
    start_sdk: Callable[..., InMemorySpanExporter],
) -> None:
    """One provider per process, which is OpenTelemetry's own model.

    Agent identity rides the span, not a Resource per agent, so two agents need
    no second provider and no second Resource.
    """
    exporter = start_sdk(release="v9")

    @convergent.observe(name="billing-agent", operation="agent_run")
    def billing() -> None: ...

    @convergent.observe(name="support-agent", operation="agent_run")
    def support() -> None: ...

    billing()
    support()

    spans = exporter.get_finished_spans()
    assert {span.resource for span in spans} == {_core.active_provider().resource}  # type: ignore[union-attr]
    by_name = {span.name: dict(span.attributes or {}) for span in spans}
    assert by_name["invoke_agent billing-agent"]["gen_ai.agent.name"] == "billing-agent"
    assert by_name["invoke_agent support-agent"]["gen_ai.agent.name"] == "support-agent"


def test_agent_runs_carry_their_name_and_version_as_span_attributes(
    start_sdk: Callable[..., InMemorySpanExporter],
) -> None:
    """The standard GenAI keys are where agent identity lives, and the process
    Resource carries the deployment rather than any agent name."""
    exporter = start_sdk(release="v9")

    @convergent.observe(name="support-agent", operation="agent_run")
    def support() -> None: ...

    support()

    span = exporter.get_finished_spans()[-1]
    attributes = dict(span.attributes or {})
    assert attributes["gen_ai.agent.name"] == "support-agent"
    assert attributes["gen_ai.agent.version"] == "v9"
    resource = dict(span.resource.attributes)
    assert resource["convergent.deployment.id"] == "dep_test"
    assert "convergent.agent.name" not in resource


def test_nested_calls_stay_on_the_process_provider_and_share_the_trace(
    start_sdk: Callable[..., InMemorySpanExporter],
) -> None:
    """A model_call inside an agent_run carries no agent name of its own -- it
    inherits the enclosing agent by tree inference at ingest.
    """
    exporter = start_sdk(release="v9")

    @convergent.observe(name="support-agent", operation="agent_run")
    def support() -> None:
        with convergent.span(name="gpt", operation="model_call"):
            pass

    support()

    spans = {span.name: span for span in exporter.get_finished_spans()}
    agent_span = spans["invoke_agent support-agent"]
    model_span = spans["gpt"]

    assert "convergent.agent.name" not in dict(model_span.resource.attributes)
    assert agent_span.context is not None and model_span.context is not None
    assert model_span.context.trace_id == agent_span.context.trace_id
    assert model_span.parent is not None
    assert model_span.parent.span_id == agent_span.context.span_id


def test_manual_spans_follow_otel_genai_names_and_attributes(
    start_sdk: Callable[..., InMemorySpanExporter],
) -> None:
    exporter = start_sdk(release="2026.07.24")

    with convergent.span(name="agent", operation="agent_run") as handle:
        handle.set_attribute("custom.key", "value")
        handle.set_input({"prompt": "always captured"})
        handle.set_output({"answer": "always captured"})
    with convergent.span(name="lookup_account", operation="tool_call"):
        pass

    agent_span, tool_span = exporter.get_finished_spans()
    agent_attributes = agent_span.attributes
    tool_attributes = tool_span.attributes
    assert agent_attributes is not None
    assert tool_attributes is not None
    assert {"set_attribute", "set_input", "set_output"} <= set(dir(handle))
    assert agent_span.name == "invoke_agent agent"
    assert agent_attributes["gen_ai.operation.name"] == "invoke_agent"
    assert agent_attributes["gen_ai.agent.name"] == "agent"
    assert agent_attributes["gen_ai.agent.version"] == "2026.07.24"
    assert agent_attributes["convergent.semantic.version"]
    # Content goes to the standard GenAI fields, which is what our own ingest
    # reads. There is no off switch and nothing is filtered.
    assert "always captured" in str(agent_attributes["gen_ai.input.messages"])
    assert "always captured" in str(agent_attributes["gen_ai.output.messages"])
    assert tool_span.name == "execute_tool lookup_account"
    assert tool_attributes["gen_ai.operation.name"] == "execute_tool"
    assert tool_attributes["gen_ai.tool.name"] == "lookup_account"


def test_the_release_never_overwrites_an_agent_version_the_span_already_has(
    start_sdk: Callable[..., InMemorySpanExporter],
) -> None:
    """A framework agent that versions itself keeps its own answer. The SDK's
    release only fills the gap for one that does not."""
    exporter = start_sdk(release="2026.07.24")
    provider = _core.active_provider()
    assert provider is not None
    tracer = provider.get_tracer("test")

    with tracer.start_as_current_span(
        "invoke_agent theirs",
        attributes={"gen_ai.operation.name": "invoke_agent", "gen_ai.agent.version": "theirs-1.0"},
    ):
        pass
    with tracer.start_as_current_span(
        "invoke_agent unversioned",
        attributes={"gen_ai.operation.name": "invoke_agent"},
    ):
        pass

    by_name = {span.name: dict(span.attributes or {}) for span in exporter.get_finished_spans()}
    assert by_name["invoke_agent theirs"]["gen_ai.agent.version"] == "theirs-1.0"
    assert by_name["invoke_agent unversioned"]["gen_ai.agent.version"] == "2026.07.24"
    assert "gen_ai.agent.name" not in by_name["invoke_agent unversioned"], (
        "a nameless framework agent stays nameless; ingest infers it"
    )


def test_spans_carry_the_registered_deployment_id_as_an_attribute(
    start_sdk: Callable[..., InMemorySpanExporter],
) -> None:
    """The deployment identity rides every GenAI span, not only the Resource.
    An attached provider's Resource is the caller's and frozen, so the span
    attribute is the only carrier that survives attach mode."""
    exporter = start_sdk(release="1.2.3")
    with convergent.span(name="gpt-5.5", operation="model_call"):
        pass
    (span,) = exporter.get_finished_spans()
    assert span.attributes is not None
    assert span.attributes["convergent.deployment.id"] == "dep_test"


def test_the_handle_reports_the_ids_the_exported_span_carries(
    start_sdk: Callable[..., InMemorySpanExporter],
) -> None:
    exporter = start_sdk()

    with convergent.span(name="gpt", operation="model_call") as handle:
        trace_id, span_id = handle.trace_id, handle.span_id
        assert handle.permalink is None

    exported = exporter.get_finished_spans()[-1].context
    assert exported is not None
    assert trace_id == f"{exported.trace_id:032x}"
    assert span_id == f"{exported.span_id:016x}"
    assert re.fullmatch(r"[0-9a-f]{32}", trace_id or ""), f"32 lowercase hex, got {trace_id!r}"
    assert re.fullmatch(r"[0-9a-f]{16}", span_id or ""), f"16 lowercase hex, got {span_id!r}"


def test_current_trace_is_none_outside_a_span_and_the_active_ids_inside_one(
    start_sdk: Callable[..., InMemorySpanExporter],
) -> None:
    exporter = start_sdk()
    assert convergent.current_trace() is None

    with convergent.span(name="gpt", operation="model_call") as handle:
        active = convergent.current_trace()
        assert active is not None
        assert (active.trace_id, active.span_id) == (handle.trace_id, handle.span_id)
        assert active.permalink is None

    assert convergent.current_trace() is None, "the span is over"
    exported = exporter.get_finished_spans()[-1].context
    assert exported is not None
    assert active.trace_id == f"{exported.trace_id:032x}"
    assert active.span_id == f"{exported.span_id:016x}"


def test_a_disabled_sdk_reports_no_trace_rather_than_junk(disabled_sdk: None) -> None:
    with convergent.span(name="gpt", operation="model_call") as call:
        assert call.trace_id is None
        assert call.span_id is None
        assert call.permalink is None
        assert convergent.current_trace() is None
    assert convergent.current_trace() is None


def test_start_time_attributes_reach_the_exported_span(
    start_sdk: Callable[..., InMemorySpanExporter],
) -> None:
    exporter = start_sdk()

    @convergent.observe(
        name="support-agent", operation="agent_run", attributes={"session.id": "s-1"}
    )
    def answer() -> None:
        with convergent.span(
            name="gpt", operation="model_call", attributes={"turn": 3, "cached": True}
        ):
            pass

    answer()

    spans = {span.name: span.attributes or {} for span in exporter.get_finished_spans()}
    assert spans["invoke_agent support-agent"]["convergent.session.id"] == "s-1"
    assert spans["gpt"]["turn"] == 3
    assert spans["gpt"]["cached"] is True


def test_a_bad_start_time_attribute_is_dropped_and_the_span_still_lands(
    start_sdk: Callable[..., InMemorySpanExporter],
    caplog: pytest.LogCaptureFixture,
) -> None:
    exporter = start_sdk()
    hostile: dict[Any, Any] = {
        "gen_ai.agent.name": "attacker",
        "convergent.execution.id": "attacker",
        1: "not a string key",
        "nested": {"not": "a scalar"},
        "fine": "kept",
    }

    with caplog.at_level(logging.ERROR, logger="convergent.sdk"):
        with convergent.span(name="agent", operation="agent_run", attributes=cast(Any, hostile)):
            pass

    attributes = exporter.get_finished_spans()[-1].attributes or {}
    assert attributes["gen_ai.agent.name"] == "agent", "identity is not overwritable"
    assert attributes["convergent.execution.id"] != "attacker"
    assert 1 not in attributes
    assert "nested" not in attributes
    assert attributes["fine"] == "kept"
    assert [r for r in caplog.records if "ignored the attribute" in r.getMessage()]


def test_execution_id_is_shared_by_nested_agent_work_but_not_concurrent_runs(
    start_sdk: Callable[..., InMemorySpanExporter],
) -> None:
    exporter = start_sdk()

    @convergent.observe(name="child", operation="tool_call")
    def child() -> None:
        return None

    @convergent.observe(name="run", operation="agent_run")
    def run() -> None:
        child()

    run()
    first = exporter.get_finished_spans()
    child_attributes = first[0].attributes
    run_attributes = first[1].attributes
    assert child_attributes is not None
    assert run_attributes is not None
    assert child_attributes["convergent.execution.id"] == run_attributes["convergent.execution.id"]
    assert "convergent.execution.id" not in first[0].resource.attributes
    assert first[0].context is not None
    assert child_attributes["convergent.execution.id"] == f"{first[0].context.trace_id:032x}", (
        "the execution id is the trace id, which is what carries it between processes"
    )

    async def concurrent() -> None:
        await asyncio.gather(
            convergent.observe(name="one", operation="agent_run")(asyncio.sleep)(0),
            convergent.observe(name="two", operation="agent_run")(asyncio.sleep)(0),
        )

    asyncio.run(concurrent())
    ids = []
    for finished_span in exporter.get_finished_spans()[-2:]:
        attributes = finished_span.attributes
        assert attributes is not None
        ids.append(attributes["convergent.execution.id"])
    assert ids[0] != ids[1]


def test_nothing_convergent_is_left_in_otel_baggage(
    start_sdk: Callable[..., InMemorySpanExporter],
) -> None:
    """Baggage is sent on outbound HTTP headers by default, so a Convergent key in
    it would reach every third-party host the caller's instrumented client calls.
    Cross-process grouping uses the trace id in ``traceparent`` instead."""
    start_sdk()

    with convergent.span(name="agent", operation="agent_run"):
        with convergent.span(name="gpt", operation="model_call"):
            inside = dict(baggage.get_all())
    after = dict(baggage.get_all())

    assert [key for key in inside if key.startswith("convergent.")] == []
    assert [key for key in after if key.startswith("convergent.")] == []


@pytest.mark.parametrize("key", ["session.id", "gen_ai.conversation.id"])
def test_setting_either_conversation_id_spelling_writes_both(
    start_sdk: Callable[..., InMemorySpanExporter], key: str
) -> None:
    exporter = start_sdk()

    with convergent.span(name="agent", operation="agent_run") as handle:
        handle.set_attribute(key, "conv-1")
    with convergent.span(name="agent", operation="agent_run", attributes={key: "conv-2"}):
        pass

    from_handle, from_attributes = (span.attributes for span in exporter.get_finished_spans())
    assert from_handle is not None and from_attributes is not None
    assert from_handle["gen_ai.conversation.id"] == "conv-1"
    assert from_handle["convergent.session.id"] == "conv-1"
    assert from_attributes["gen_ai.conversation.id"] == "conv-2"
    assert from_attributes["convergent.session.id"] == "conv-2"
    assert "session.id" not in from_handle and "session.id" not in from_attributes


def test_reserved_attributes_cannot_be_overwritten(
    start_sdk: Callable[..., InMemorySpanExporter],
) -> None:
    exporter = start_sdk()

    with convergent.span(name="agent", operation="agent_run") as handle:
        handle.set_attribute("convergent.execution.id", "attacker")
        handle.set_attribute("convergent.semantic.version", "attacker")
        handle.set_attribute("gen_ai.operation.name", "attacker")
    span = exporter.get_finished_spans()[-1]
    attributes = span.attributes
    assert attributes is not None
    assert attributes["convergent.execution.id"] != "attacker"
    assert attributes["convergent.semantic.version"] != "attacker"
    assert attributes["gen_ai.operation.name"] == "invoke_agent"

    with convergent.span(name="lookup", operation="tool_call") as tool:
        tool.set_input({"query": "real"})
        tool.set_attribute("gen_ai.tool.call.arguments", "attacker")
        tool.set_attribute("gen_ai.tool.call.result", "attacker")
    tool_attributes = exporter.get_finished_spans()[-1].attributes
    assert tool_attributes is not None
    assert tool_attributes["gen_ai.tool.call.arguments"] == '{"query":"real"}'
    assert "gen_ai.tool.call.result" not in tool_attributes


def test_the_message_keys_are_reserved(
    start_sdk: Callable[..., InMemorySpanExporter],
) -> None:
    """``set_input``/``set_output`` own the message keys, so a caller may not
    write a scalar onto them.

    ``decode_messages`` returns ``None`` for anything that is not a list, so a
    string here would be dropped downstream with a warning -- the silent
    content loss this change exists to remove. The message keys stay the place
    a chat span's content goes; only a tool call moved off them.
    """
    exporter = start_sdk()

    with convergent.span(name="gpt", operation="model_call") as handle:
        handle.set_input({"question": "real"})
        handle.set_attribute("gen_ai.input.messages", "scalar")
        handle.set_attribute("gen_ai.output.messages", "scalar")

    attributes = exporter.get_finished_spans()[-1].attributes
    assert attributes is not None
    assert attributes["gen_ai.input.messages"] != "scalar"
    assert json.loads(str(attributes["gen_ai.input.messages"]))[0]["role"] == "user"
    assert "gen_ai.output.messages" not in attributes


def test_tool_content_goes_to_the_standard_tool_keys(
    start_sdk: Callable[..., InMemorySpanExporter],
) -> None:
    """A tool call's arguments and result have GenAI keys of their own.

    Recording them as chat messages meant only a reader that already knew the
    span was ours could read them back, so every other producer's tool call
    rendered as "Invalid arguments". A chat span still writes the message keys,
    which is correct for it.
    """
    exporter = start_sdk()

    with convergent.span(name="lookup", operation="tool_call") as tool:
        tool.set_input({"query": "invoice"})
        tool.set_output({"found": True})
    with convergent.span(name="gpt", operation="model_call") as call:
        call.set_input({"question": "hi"})
        call.set_output({"answer": "there"})

    by_name = {span.name: span.attributes or {} for span in exporter.get_finished_spans()}
    tool_attributes = by_name["execute_tool lookup"]
    chat_attributes = by_name["gpt"]

    assert tool_attributes["gen_ai.tool.call.arguments"] == '{"query":"invoice"}'
    assert tool_attributes["gen_ai.tool.call.result"] == '{"found":true}'
    assert "gen_ai.input.messages" not in tool_attributes
    assert "gen_ai.output.messages" not in tool_attributes
    assert [key for key in tool_attributes if key.startswith("convergent.content.")] == []

    assert json.loads(str(chat_attributes["gen_ai.input.messages"]))[0]["role"] == "user"
    assert json.loads(str(chat_attributes["gen_ai.output.messages"]))[0]["role"] == "assistant"
    assert "gen_ai.tool.call.arguments" not in chat_attributes


def test_the_tool_call_id_is_recorded_under_the_standard_key(
    start_sdk: Callable[..., InMemorySpanExporter],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Ingest pairs the model's request with the execution on this id."""
    exporter = start_sdk()

    with convergent.span(name="lookup", operation="tool_call") as tool:
        tool.set_tool_call_id("call_abc123")
    assert (exporter.get_finished_spans()[-1].attributes or {})["gen_ai.tool.call.id"] == (
        "call_abc123"
    )

    with caplog.at_level(logging.ERROR, logger="convergent.sdk"):
        with convergent.span(name="lookup", operation="tool_call") as tool:
            tool.set_tool_call_id("")
    assert "gen_ai.tool.call.id" not in (exporter.get_finished_spans()[-1].attributes or {})
    assert "tool call id" in caplog.text


def test_an_oversized_tool_call_id_does_not_ship(
    start_sdk: Callable[..., InMemorySpanExporter],
) -> None:
    """A call id is model output, so its length is the model's choice until bounded.

    ``set_tool_call_id`` deliberately ignores ``content``, so an unbounded id would
    be a way for a prompt-injected model to move text out of a process that turned
    content capture off.
    """
    exporter = start_sdk()

    with convergent.span(name="lookup", operation="tool_call") as tool:
        tool.set_tool_call_id("x" * (_semantic._CALL_ID_LIMIT + 1))

    assert "gen_ai.tool.call.id" not in (exporter.get_finished_spans()[-1].attributes or {})


@pytest.mark.parametrize("operation", ["tool_call", "execute_tool"])
def test_a_tool_span_declares_what_kind_of_tool_it_ran(
    start_sdk: Callable[..., InMemorySpanExporter],
    operation: str,
) -> None:
    """The conventions recommend ``gen_ai.tool.type`` whenever the writer knows it.

    ``span(operation="tool_call")`` wraps a callable running in the caller's own
    process, which is what the conventions call a ``function``. No other operation
    gets the key, because the conventions only define it for a tool call.
    """
    exporter = start_sdk()

    with convergent.span(name="lookup", operation=cast(Any, operation)):
        pass
    with convergent.span(name="gpt-5.5", operation="model_call"):
        pass

    tool_span, model_span = exporter.get_finished_spans()
    assert (tool_span.attributes or {})["gen_ai.tool.type"] == "function"
    assert "gen_ai.tool.type" not in (model_span.attributes or {})


def test_a_caller_who_names_the_tool_type_keeps_their_own_answer(
    start_sdk: Callable[..., InMemorySpanExporter],
) -> None:
    """The SDK only knows it wrapped a callable, so ``function`` is a default and
    not a claim. A tool that queries a knowledge base is a ``datastore``, and only
    the caller knows that."""
    exporter = start_sdk()

    with convergent.span(
        name="search_docs",
        operation="tool_call",
        attributes={"gen_ai.tool.type": "datastore"},
    ):
        pass

    (span,) = exporter.get_finished_spans()
    assert (span.attributes or {})["gen_ai.tool.type"] == "datastore"


def test_spans_name_the_convention_version_they_are_written_for(
    start_sdk: Callable[..., InMemorySpanExporter],
) -> None:
    """The schema url rides the instrumentation scope, so a consumer reads the
    attribute names against the version we wrote them for rather than guessing."""
    exporter = start_sdk()

    with convergent.span(name="agent", operation="agent_run"):
        pass

    (span,) = exporter.get_finished_spans()
    assert span.instrumentation_scope is not None
    assert span.instrumentation_scope.name == "convergent.sdk"
    assert span.instrumentation_scope.schema_url == "https://opentelemetry.io/schemas/1.40.0"


@pytest.mark.parametrize(
    ("operation", "span_name", "identity_key", "identity_value"),
    [
        ("tool_call", "execute_tool lookup", "gen_ai.tool.name", "lookup"),
        ("execute_tool", "execute_tool lookup", "gen_ai.tool.name", "lookup"),
        ("agent_run", "invoke_agent lookup", "gen_ai.agent.name", "lookup"),
        ("invoke_agent", "invoke_agent lookup", "gen_ai.agent.name", "lookup"),
    ],
)
def test_the_semconv_operation_decides_the_span_shape(
    start_sdk: Callable[..., InMemorySpanExporter],
    caplog: pytest.LogCaptureFixture,
    operation: str,
    span_name: str,
    identity_key: str,
    identity_value: str,
) -> None:
    """A caller who writes the standard name gets the same span as one who writes ours.

    ``set_input`` has to route on the same value ingest classifies on, so ``span()``
    resolves the operation once and every branch reads the resolved name. A caller
    writing ``"execute_tool"`` used to get a span with no tool name, content in the
    message keys, and a warning saying the operation maps to nothing.
    """
    exporter = start_sdk()

    with caplog.at_level(logging.WARNING, logger="convergent.sdk"):
        with convergent.span(name="lookup", operation=cast(Any, operation)) as handle:
            handle.set_input({"query": "invoice"})

    span = exporter.get_finished_spans()[-1]
    attributes = span.attributes or {}
    assert span.name == span_name
    assert attributes[identity_key] == identity_value
    assert "unrecognized operation" not in caplog.text


def test_an_unrecognized_operation_warns_rather_than_errors(
    start_sdk: Callable[..., InMemorySpanExporter],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A custom operation is a documented feature, so it must not log at ERROR.

    ``_semconv_operation`` records it verbatim and ``SemanticSpanProcessor`` still
    joins its execution. Emitting ERROR for a supported call trains callers to
    ignore the level reserved for things that are actually broken.
    """
    start_sdk()

    with caplog.at_level(logging.DEBUG, logger="convergent.sdk"):
        with convergent.span(name="guardrail", operation="guardrail_check"):
            pass

    unrecognized = [r for r in caplog.records if "unrecognized operation" in r.getMessage()]
    assert unrecognized, "a custom operation is still reported"
    assert [r.levelno for r in unrecognized] == [logging.WARNING]

    # An unusable *name* is a different class of mistake and stays at ERROR,
    # because ingest will reject that span outright.
    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="convergent.sdk"):
        with convergent.span(name="", operation="agent_run"):
            pass
    assert [r.levelno for r in caplog.records if "unusable name" in r.getMessage()] == [
        logging.ERROR
    ]


def test_content_is_written_to_the_standard_fields_unfiltered(
    start_sdk: Callable[..., InMemorySpanExporter],
) -> None:
    """set_input/set_output write the standard GenAI message attributes, verbatim.

    Nothing is redacted here: content policy is a collector's job, so the secret
    below deliberately survives. Size is bounded only by the provider's
    ``SpanLimits``, well above this payload -- see the span-limit test below.
    """
    exporter = start_sdk()
    secret = "sk-live-do-record"  # pragma: allowlist secret
    with convergent.span(name="agent", operation="agent_run") as handle:
        handle.set_input({"api_key": secret, "payload": "x" * 20_000})
        handle.set_output({"result": "ok"})

    attributes = exporter.get_finished_spans()[-1].attributes
    assert attributes is not None
    assert "convergent.input" not in attributes
    assert "convergent.output" not in attributes

    encoded = str(attributes["gen_ai.input.messages"])
    assert secret in encoded, "the SDK does not scrub content"
    assert len(encoded) > 8_192, "the SDK does not bound content"

    # The canonical message shape, which is what our own reader decodes. A bare
    # JSON object here would be dropped by decode_messages().
    decoded = json.loads(str(attributes["gen_ai.output.messages"]))
    assert isinstance(decoded, list)
    assert decoded[0]["role"] == "assistant"
    assert json.loads(decoded[0]["parts"][0]["content"]) == {"result": "ok"}


def test_oversized_content_arrives_whole_and_parses(
    start_sdk: Callable[..., InMemorySpanExporter],
) -> None:
    """No cap of ours, because the cap was the thing losing the content.

    OpenTelemetry enforces an attribute cap by cutting the finished string, and a
    messages value is JSON, so the cut landed mid-token and ``json.loads`` raised:
    all of the content lost rather than the overflow, and large content is common
    on real traffic. Content travels whole now, and what cannot stay on a span is
    moved to the blob store at ingest.
    """
    exporter = start_sdk()
    payload = "x" * 900_000

    with convergent.span(name="agent", operation="agent_run") as handle:
        handle.set_input([{"role": "user", "content": payload}])

    attributes = exporter.get_finished_spans()[-1].attributes
    assert attributes is not None
    decoded = json.loads(str(attributes["gen_ai.input.messages"]))
    assert decoded == [{"role": "user", "content": payload}]


def test_an_explicit_otel_length_limit_still_applies(monkeypatch: pytest.MonkeyPatch) -> None:
    """A caller who wants a cap sets OpenTelemetry's own variable and gets one.

    Passing nothing to ``SpanLimits`` is what lets it read the environment, so
    removing our floor must not remove the operator's ability to choose.
    """
    monkeypatch.setenv("OTEL_ATTRIBUTE_VALUE_LENGTH_LIMIT", "64")
    assert _core._span_limits().max_span_attribute_length == 64

    monkeypatch.delenv("OTEL_ATTRIBUTE_VALUE_LENGTH_LIMIT")
    assert _core._span_limits().max_span_attribute_length is None


def test_an_object_whose_repr_raises_does_not_escape_set_input(
    start_sdk: Callable[..., InMemorySpanExporter],
) -> None:
    """The one contract this SDK makes.

    ``json.dumps(default=repr)`` runs the caller's ``__repr__``, and so did the
    fallback meant to catch it, so a raising ``__repr__`` propagated out of
    ``set_input`` into the caller's own code.
    """
    exporter = start_sdk()

    class Hostile:
        def __repr__(self) -> str:
            raise ValueError("this must not reach the caller")

    with convergent.span(name="agent", operation="agent_run") as handle:
        handle.set_input({"payload": Hostile()})
        handle.set_output([{"role": "assistant", "content": Hostile()}])

    assert exporter.get_finished_spans()[-1].attributes is not None


def test_a_caller_supplied_message_list_passes_through(
    start_sdk: Callable[..., InMemorySpanExporter],
) -> None:
    """A value already shaped like a message array is not re-wrapped."""
    exporter = start_sdk()
    messages = [{"role": "user", "parts": [{"type": "text", "content": "hello"}]}]
    with convergent.span(name="agent", operation="agent_run") as handle:
        handle.set_input(messages)

    attributes = exporter.get_finished_spans()[-1].attributes or {}
    assert json.loads(str(attributes["gen_ai.input.messages"])) == messages


@pytest.mark.parametrize(
    ("operation", "expected"),
    [
        ("agent_run", "invoke_agent"),
        ("model_call", "chat"),
        ("tool_call", "execute_tool"),
        ("retrieval", "retrieval"),
        ("embeddings", "embeddings"),
        ("workflow", "invoke_workflow"),
        ("agent_create", "create_agent"),
        ("text_completion", "text_completion"),
        ("generate_content", "generate_content"),
        ("guardrail_check", "guardrail_check"),
    ],
)
def test_every_operation_maps_to_its_semconv_value(
    start_sdk: Callable[..., InMemorySpanExporter],
    operation: str,
    expected: str,
) -> None:
    """The nine standard GenAI operations, plus an arbitrary string recorded as
    given so a guardrail or approval step is traceable."""
    exporter = start_sdk()
    with convergent.span(name="step", operation=cast(Any, operation)):
        pass

    attributes = exporter.get_finished_spans()[-1].attributes or {}
    assert attributes["gen_ai.operation.name"] == expected
    assert attributes["convergent.execution.id"], "a custom operation is still an execution"


@pytest.mark.parametrize(
    "operation",
    [
        "guardrail_check",
        "tool",
        "toolcall",
        "Tool_Call",
        "tool_call ",
        "agent",
        "model",
        "retrieve",
        "Execute_Tool",
    ],
)
def test_an_operation_outside_the_table_is_recorded_word_for_word(
    start_sdk: Callable[..., InMemorySpanExporter],
    operation: str,
) -> None:
    """The lookup is by exact key, so a near miss stays the caller's own word.

    ``tool``, ``toolcall`` and ``Tool_Call`` are each one edit away from
    ``tool_call``, and ``Execute_Tool`` is one case fold away from the semconv
    value itself. Matching any of them would file a caller's step under a concept
    they never named, move their content to the tool keys, and stamp
    ``gen_ai.tool.type`` on work that is not a tool call.
    """
    exporter = start_sdk()

    # The decorator path, deliberately: the neighboring operation tests cover
    # convergent.span().
    @convergent.observe(name="step", operation=cast(Any, operation))
    def step() -> None:
        return None

    step()

    attributes = exporter.get_finished_spans()[-1].attributes or {}
    assert attributes["gen_ai.operation.name"] == operation
    assert "gen_ai.tool.type" not in attributes
    assert "gen_ai.tool.name" not in attributes
    assert "gen_ai.agent.name" not in attributes


@pytest.mark.parametrize(
    "kwargs",
    [
        {"name": "", "operation": "agent_run"},
        {"name": "x" * 200, "operation": "agent_run"},
        {"name": "x", "operation": "unknown"},
    ],
)
def test_bad_arguments_are_reported_and_the_span_is_still_sent(
    start_sdk: Callable[..., InMemorySpanExporter],
    caplog: pytest.LogCaptureFixture,
    kwargs: dict[str, str],
) -> None:
    """Nothing raises. A caller's mistake is logged and the span goes anyway.

    Raising would help a developer at their desk and hurt a production process
    where nobody can act on it. Ingest rejects a bad name instead, where it costs
    the caller nothing.
    """
    exporter = start_sdk()
    with caplog.at_level(logging.WARNING, logger="convergent.sdk"):
        decorated = convergent.observe(
            name=kwargs["name"], operation=cast(Any, kwargs["operation"])
        )(lambda: "result")
        assert decorated() == "result", "the wrapped function is unaffected"

    assert exporter.get_finished_spans(), "the span is still recorded"
    assert any("recorded a span" in record.message for record in caplog.records), (
        "the mistake is reported, not swallowed"
    )


def test_a_repeated_bad_name_is_reported_once(
    start_sdk: Callable[..., InMemorySpanExporter],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """span() re-checks its arguments per call, so an unusable name in a loop
    would bury the signal it exists to give."""
    start_sdk()
    with caplog.at_level(logging.ERROR, logger="convergent.sdk"):
        for _ in range(50):
            with convergent.span(name="x" * 200, operation="agent_run"):
                pass

    invalid = [r for r in caplog.records if "unusable name" in r.getMessage()]
    assert len(invalid) == 1, f"expected one report, got {len(invalid)}"


def test_reporting_is_bounded_by_reason_not_by_caller_input(
    start_sdk: Callable[..., InMemorySpanExporter],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The report-once set must not grow with caller input.

    Keying it on the offending name would leak memory for a caller passing
    ``f"agent-{uuid4()}"`` in a loop — the same high-cardinality mistake our docs
    warn customers about, reproduced inside the SDK, and reachable only because we
    now tolerate bad input rather than rejecting it.
    """
    start_sdk()
    with caplog.at_level(logging.ERROR, logger="convergent.sdk"):
        for index in range(1_000):
            with convergent.span(name=f"{'x' * 200}-{index}", operation="agent_run") as handle:
                handle.set_attribute(f"convergent.forged.{index}", "v")

    assert len(_semantic._reported) <= 5, (
        f"report keys must be bounded by reason, got {len(_semantic._reported)}: "
        f"{sorted(_semantic._reported)}"
    )
    assert len(caplog.records) <= 4, f"1000 bad calls logged {len(caplog.records)} lines"


def test_a_rejected_attribute_is_reported_and_dropped(
    start_sdk: Callable[..., InMemorySpanExporter],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A reserved key or unsupported value type is dropped and logged, not raised
    and not silently discarded — the old behavior was silent."""
    exporter = start_sdk()
    with caplog.at_level(logging.ERROR, logger="convergent.sdk"):
        with convergent.span(name="agent", operation="agent_run") as handle:
            handle.set_attribute("gen_ai.agent.name", "attacker")
            handle.set_attribute("convergent.execution.id", "attacker")
            handle.set_attribute("nested", cast(Any, {"not": "a scalar"}))
            handle.set_attribute("fine", "kept")

    attributes = exporter.get_finished_spans()[-1].attributes or {}
    assert attributes["gen_ai.agent.name"] == "agent", "identity is not overwritable"
    assert attributes["convergent.execution.id"] != "attacker"
    assert "nested" not in attributes
    assert attributes["fine"] == "kept"

    # Two reasons, not three reports: both reserved keys share the
    # ``reserved_attribute`` reason, and reporting is keyed on the reason so the
    # set cannot grow with caller input. Every attribute is still dropped.
    ignored = [r for r in caplog.records if "ignored the set_attribute()" in r.getMessage()]
    reasons = {r.getMessage().split(":")[1].strip() for r in ignored}
    assert len(ignored) == 2, f"one report per reason, got {len(ignored)}"
    assert len(reasons) == 2, "a reserved key and a bad value type are distinct reasons"


def test_no_public_call_raises_on_any_input(
    start_sdk: Callable[..., InMemorySpanExporter],
) -> None:
    """There are no Convergent exception types and nothing propagates one."""
    start_sdk()
    hostile: list[Any] = [None, "", "x" * 500, 0, [], {}, object()]

    for name in hostile:
        for operation in hostile:
            with convergent.span(name=cast(Any, name), operation=cast(Any, operation)) as handle:
                for value in hostile:
                    handle.set_attribute(cast(Any, "k"), value)
                    handle.set_input(value)
                    handle.set_output(value)
                    handle.set_tool_call_id(cast(Any, value))

    for attributes in [{1: "v"}, {"k": object()}, "not a mapping", 0, []]:
        with convergent.span(name="agent", operation="agent_run", attributes=cast(Any, attributes)):
            pass

    # A circular reference defeats json.dumps' ``default`` hook, so it exercises
    # the last-resort fallback rather than the normal encode path.
    circular: dict[str, Any] = {}
    circular["self"] = circular
    with convergent.span(name="agent", operation="agent_run") as handle:
        handle.set_input(circular)
        handle.set_output(circular)

    # The aliases delegate to observe(), so they must tolerate the same abuse.
    for name in hostile:
        assert convergent.agent(name=cast(Any, name))(lambda: "result")() == "result"
        assert convergent.tool(name=cast(Any, name))(lambda: "result")() == "result"
    ambient = convergent.current_span()
    for value in hostile:
        ambient.set_attribute(cast(Any, "k"), value)
        ambient.set_input(value)
        ambient.set_output(value)


def test_agent_records_the_same_span_observe_would(
    start_sdk: Callable[..., InMemorySpanExporter],
) -> None:
    """agent() is an alias for observe(operation="agent_run"), not a reimplementation."""
    exporter = start_sdk(release="v9")

    @convergent.agent(name="support-agent", attributes={"session.id": "s-1"})
    def aliased() -> None: ...

    @convergent.observe(
        name="support-agent", operation="agent_run", attributes={"session.id": "s-1"}
    )
    def spelled_out() -> None: ...

    aliased()
    spelled_out()

    first, second = exporter.get_finished_spans()
    assert first.name == second.name == "invoke_agent support-agent"
    first_attributes = dict(first.attributes or {})
    second_attributes = dict(second.attributes or {})
    for key in (
        "gen_ai.operation.name",
        "gen_ai.agent.name",
        "gen_ai.agent.version",
        "convergent.session.id",
    ):
        assert first_attributes[key] == second_attributes[key]
    assert first_attributes["gen_ai.operation.name"] == "invoke_agent"
    assert first_attributes["gen_ai.agent.name"] == "support-agent"
    assert first_attributes["gen_ai.agent.version"] == "v9", "release stamping still applies"


def test_tool_records_the_same_span_observe_would(
    start_sdk: Callable[..., InMemorySpanExporter],
) -> None:
    exporter = start_sdk()

    @convergent.tool(name="lookup_invoice")
    def aliased() -> None: ...

    @convergent.observe(name="lookup_invoice", operation="tool_call")
    def spelled_out() -> None: ...

    aliased()
    spelled_out()

    first, second = exporter.get_finished_spans()
    assert first.name == second.name == "execute_tool lookup_invoice"
    for attributes in (first.attributes or {}, second.attributes or {}):
        assert attributes["gen_ai.operation.name"] == "execute_tool"
        assert attributes["gen_ai.tool.name"] == "lookup_invoice"


def test_tool_defaults_its_name_to_the_function_name(
    start_sdk: Callable[..., InMemorySpanExporter],
) -> None:
    """A function name is a stable identity, so @tool() may take it as the
    tool's name. An explicit name still wins."""
    exporter = start_sdk()

    @convergent.tool()
    def lookup_invoice() -> str:
        return "found"

    @convergent.tool(name="lookup_invoice")
    def differently_named() -> None: ...

    assert lookup_invoice() == "found"
    differently_named()

    defaulted, explicit = exporter.get_finished_spans()
    assert defaulted.name == explicit.name == "execute_tool lookup_invoice"
    assert (defaulted.attributes or {})["gen_ai.tool.name"] == "lookup_invoice"
    assert (explicit.attributes or {})["gen_ai.tool.name"] == "lookup_invoice"


def test_the_aliases_compose_with_generators_and_async_functions(
    start_sdk: Callable[..., InMemorySpanExporter],
) -> None:
    """agent() and tool() delegate to observe(), so every calling convention
    observe() supports comes with them."""
    exporter = start_sdk()

    @convergent.agent(name="streamer")
    def stream() -> Iterator[int]:
        yield 1
        yield 2

    @convergent.tool()
    async def fetch() -> int:
        return 7

    assert list(stream()) == [1, 2]
    assert asyncio.run(fetch()) == 7
    assert [span.name for span in exporter.get_finished_spans()] == [
        "invoke_agent streamer",
        "execute_tool fetch",
    ]


def test_current_span_writes_to_the_decorated_functions_own_span(
    start_sdk: Callable[..., InMemorySpanExporter],
) -> None:
    """The decorators yield no handle; current_span() is how code inside a
    decorated function reaches the span the decorator opened."""
    exporter = start_sdk()

    @convergent.agent(name="support-agent")
    def run() -> None:
        handle = convergent.current_span()
        handle.set_input({"question": "hi"})
        handle.set_output({"answer": "there"})
        handle.set_attribute("session.id", "s-1")
        active = convergent.current_trace()
        assert active is not None
        assert (active.trace_id, active.span_id) == (handle.trace_id, handle.span_id)

    run()

    span = exporter.get_finished_spans()[-1]
    attributes = span.attributes or {}
    assert span.name == "invoke_agent support-agent"
    assert json.loads(str(attributes["gen_ai.input.messages"]))[0]["role"] == "user"
    assert json.loads(str(attributes["gen_ai.output.messages"]))[0]["role"] == "assistant"
    assert attributes["convergent.session.id"] == "s-1"


def test_current_span_routes_tool_content_to_the_tool_keys(
    start_sdk: Callable[..., InMemorySpanExporter],
) -> None:
    """The handle reads the operation back off the active span, so content
    inside a tool function goes where span(operation="tool_call") puts it."""
    exporter = start_sdk()

    @convergent.tool()
    def lookup_invoice() -> None:
        convergent.current_span().set_input({"query": "invoice"})
        convergent.current_span().set_output({"found": True})

    lookup_invoice()

    attributes = exporter.get_finished_spans()[-1].attributes or {}
    assert attributes["gen_ai.tool.call.arguments"] == '{"query":"invoice"}'
    assert attributes["gen_ai.tool.call.result"] == '{"found":true}'
    assert "gen_ai.input.messages" not in attributes
    assert "gen_ai.output.messages" not in attributes


def test_current_span_enforces_the_reserved_key_guard(
    start_sdk: Callable[..., InMemorySpanExporter],
) -> None:
    exporter = start_sdk()

    @convergent.agent(name="support-agent")
    def run() -> None:
        handle = convergent.current_span()
        handle.set_attribute("gen_ai.agent.name", "attacker")
        handle.set_attribute("convergent.execution.id", "attacker")
        handle.set_attribute("fine", "kept")

    run()

    attributes = exporter.get_finished_spans()[-1].attributes or {}
    assert attributes["gen_ai.agent.name"] == "support-agent", "identity is not overwritable"
    assert attributes["convergent.execution.id"] != "attacker"
    assert attributes["fine"] == "kept"


def test_current_span_outside_any_span_is_a_no_op_not_none(
    start_sdk: Callable[..., InMemorySpanExporter],
) -> None:
    exporter = start_sdk()

    handle = convergent.current_span()
    handle.set_attribute("k", "v")
    handle.set_input("lost")
    handle.set_output("lost")

    assert isinstance(handle, _semantic._NoOpSpanHandle)
    assert handle.trace_id is None
    assert not exporter.get_finished_spans(), "nothing was recorded anywhere"


def test_current_span_is_a_no_op_when_tracing_is_disabled(disabled_sdk: None) -> None:
    """Mirrors span() when disabled: the block still runs, nothing records."""

    @convergent.tool()
    def lookup() -> str:
        handle = convergent.current_span()
        handle.set_input({"query": "x"})
        assert handle.trace_id is None
        return "ok"

    assert lookup() == "ok"
    assert isinstance(convergent.current_span(), _semantic._NoOpSpanHandle)

    # A span someone else's provider opened does not arm the handle either:
    # with the SDK unconfigured there is no provider to record through, so the
    # guarded answer is the no-op one.
    provider = TracerProvider()
    with provider.get_tracer("test").start_as_current_span("theirs"):
        assert isinstance(convergent.current_span(), _semantic._NoOpSpanHandle)
    provider.shutdown()


def _split_reports(caplog: pytest.LogCaptureFixture) -> list[str]:
    reports = (record.getMessage() for record in caplog.records)
    return [report for report in reports if "splitting into separate traces" in report]


def test_a_span_rooting_a_new_trace_inside_an_open_span_is_reported_once(
    start_sdk: Callable[..., InMemorySpanExporter],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The pydantic-evals shape: a span nobody records sits around each case.

    A span started under one of those begins a new trace, so the suite span ends up
    alone in a trace of its own and nothing said so until now. The parent here is
    built from the OpenTelemetry API rather than by running pydantic-evals,
    because the shape is what matters and it is two lines.
    """
    exporter = start_sdk()
    with caplog.at_level(logging.WARNING, logger="convergent.sdk"):
        with convergent.span(name="suite", operation="workflow") as suite:
            for _ in range(100):
                with trace.use_span(NonRecordingSpan(INVALID_SPAN_CONTEXT), end_on_exit=False):
                    with convergent.span(name="case", operation="workflow") as case:
                        assert case.trace_id != suite.trace_id, "the case rooted a new trace"

    assert len(_split_reports(caplog)) == 1, "one line per process, not one per case"
    traces = {span.context.trace_id for span in exporter.get_finished_spans() if span.context}
    assert len(traces) == 101, "one trace per case, and the suite span alone in its own"


def test_parallel_root_spans_on_threads_are_not_reported_as_a_split(
    start_sdk: Callable[..., InMemorySpanExporter],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Two threads each running their own trace is not the symptom.

    A thread starts with an empty context, so the count of open SDK spans reads zero
    on it whether or not the thread that spawned it holds a span open. Both shapes
    stay quiet.
    """
    exporter = start_sdk()

    def run(name: str) -> None:
        with convergent.span(name=name, operation="agent_run"):
            pass

    def run_all(prefix: str) -> None:
        threads = [threading.Thread(target=run, args=(f"{prefix}-{i}",)) for i in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

    with caplog.at_level(logging.WARNING, logger="convergent.sdk"):
        run_all("plain")
        with convergent.span(name="suite", operation="workflow"):
            run_all("inside")

    assert _split_reports(caplog) == []
    traces = {span.context.trace_id for span in exporter.get_finished_spans() if span.context}
    assert len(traces) == 9, "eight thread roots and the suite really are separate traces"


def test_nested_and_sequential_spans_are_not_reported_as_a_split(
    start_sdk: Callable[..., InMemorySpanExporter],
    caplog: pytest.LogCaptureFixture,
) -> None:
    exporter = start_sdk()
    with caplog.at_level(logging.WARNING, logger="convergent.sdk"):
        with convergent.span(name="agent", operation="agent_run") as agent:
            with convergent.span(name="lookup", operation="tool_call") as lookup:
                assert lookup.trace_id == agent.trace_id
        with convergent.span(name="agent", operation="agent_run"):
            pass

    assert _split_reports(caplog) == []
    assert len(exporter.get_finished_spans()) == 3


def test_a_non_recording_parent_with_a_valid_context_is_not_a_split(
    start_sdk: Callable[..., InMemorySpanExporter],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Not every parent nobody records breaks the trace.

    OpenTelemetry only starts a new trace when the span in the context has an
    invalid span context. One that carries a valid one, such as a span a sampler
    dropped, still hands its trace id to the child, so there is nothing to report.
    """
    exporter = start_sdk()
    with caplog.at_level(logging.WARNING, logger="convergent.sdk"):
        with convergent.span(name="suite", operation="workflow") as suite:
            assert suite.trace_id is not None
            dropped = SpanContext(
                trace_id=int(suite.trace_id, 16),
                span_id=0x00FF00FF00FF00FF,
                is_remote=False,
                trace_flags=TraceFlags(TraceFlags.SAMPLED),
            )
            with trace.use_span(NonRecordingSpan(dropped), end_on_exit=False):
                with convergent.span(name="case", operation="workflow") as case:
                    assert case.trace_id == suite.trace_id

    assert _split_reports(caplog) == []
    traces = {span.context.trace_id for span in exporter.get_finished_spans() if span.context}
    assert len(traces) == 1, "the trace held together"
