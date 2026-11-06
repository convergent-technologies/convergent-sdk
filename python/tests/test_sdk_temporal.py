from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from collections.abc import Awaitable, Callable, Iterator, Sequence
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from opentelemetry import baggage, propagate, trace
from opentelemetry import context as otel_context
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from temporalio import activity, workflow
from temporalio.client import Client, Interceptor, Plugin
from temporalio.common import RetryPolicy

import convergent
from convergent import _core, _tag_fetch, _transport

_TIMEOUT = timedelta(seconds=30)
_NO_RETRY = RetryPolicy(maximum_attempts=1)


def _reset_otel() -> None:
    trace._TRACER_PROVIDER_SET_ONCE._done = False  # type: ignore[attr-defined]
    trace._TRACER_PROVIDER = None  # type: ignore[attr-defined]


def _stub_workspace_tag(monkeypatch: pytest.MonkeyPatch) -> None:
    # As if GET /v1/check had answered; the fetch itself is stubbed out in tests.
    monkeypatch.setattr(_tag_fetch._state, "tag", "0a1b2c3d")


@activity.defn
async def run_pydantic_agent() -> str:
    from pydantic_ai import Agent
    from pydantic_ai.capabilities.instrumentation import Instrumentation
    from pydantic_ai.models.instrumented import InstrumentationSettings
    from pydantic_ai.models.test import TestModel

    provider = _core.active_provider()
    assert provider is not None
    agent = Agent(
        TestModel(),
        name="temporal-agent",
        capabilities=[Instrumentation(settings=InstrumentationSettings(tracer_provider=provider))],
    )
    return (await agent.run("hello from Temporal")).output


@activity.defn
async def read_baggage() -> str | None:
    value = baggage.get_baggage("caller.team")
    return value if isinstance(value, str) else None


@workflow.defn
class BaggageWorkflow:
    @workflow.run
    async def run(self) -> str | None:
        return await workflow.execute_activity(
            read_baggage, start_to_close_timeout=_TIMEOUT, retry_policy=_NO_RETRY
        )


@workflow.defn
class AgentWorkflow:
    @workflow.run
    async def run(self) -> str:
        return await workflow.execute_activity(
            run_pydantic_agent, start_to_close_timeout=_TIMEOUT, retry_policy=_NO_RETRY
        )


@workflow.defn
class ContinueOnceWorkflow:
    @workflow.run
    async def run(self, continued: bool) -> str:
        output = await workflow.execute_activity(
            run_pydantic_agent, start_to_close_timeout=_TIMEOUT, retry_policy=_NO_RETRY
        )
        if not continued:
            workflow.continue_as_new(True)
        return output


@workflow.defn
class ParentWorkflow:
    @workflow.run
    async def run(self) -> str:
        return await workflow.execute_child_workflow(
            AgentWorkflow.run, id=f"{workflow.info().workflow_id}-child"
        )


@pytest.fixture
def temporal_sdk(monkeypatch: pytest.MonkeyPatch) -> Iterator[InMemorySpanExporter]:
    _stub_workspace_tag(monkeypatch)
    exporter = InMemorySpanExporter()
    monkeypatch.setattr(
        _transport,
        "build_processor",
        lambda **_: SimpleSpanProcessor(exporter),
    )
    _reset_otel()
    _core._reset_for_tests()
    convergent.init(
        api_key="key",  # pragma: allowlist secret
        endpoint="https://example.test",
        release="r1",
    )
    yield exporter
    _core._reset_for_tests()
    _reset_otel()


def _run_temporal(
    interceptors: list[Interceptor],
    body: Callable[[Client, str], Awaitable[None]],
    *,
    starter_interceptors: list[Interceptor] | None = None,
    workflows: Sequence[type] = (),
    plugins: Sequence[Plugin] = (),
    **worker_options: Any,
) -> None:
    """Run ``body`` against a local Temporal server and a worker on its task queue.

    With ``starter_interceptors``, ``body`` gets a client of its own built with
    them, as a separate starting service would have; the worker keeps ``interceptors``.
    """
    from _temporal_worker import local_worker

    async def exercise() -> None:
        async with local_worker(
            task_queue="convergent-temporal",
            workflows=[
                AgentWorkflow,
                BaggageWorkflow,
                ContinueOnceWorkflow,
                ParentWorkflow,
                *workflows,
            ],
            activities=[run_pydantic_agent, read_baggage],
            interceptors=interceptors,
            plugins=plugins,
            **worker_options,
        ) as client:
            starter = client
            if starter_interceptors is not None:
                starter = await Client.connect(
                    client.service_client.config.target_host,
                    namespace=client.namespace,
                    interceptors=starter_interceptors,
                )
            await body(starter, "convergent-temporal")

    asyncio.run(exercise())


def _trace(span: ReadableSpan) -> int:
    assert span.context is not None
    return span.context.trace_id


def _span_id(span: ReadableSpan) -> int:
    assert span.context is not None
    return span.context.span_id


def _attrs(span: ReadableSpan) -> dict[str, Any]:
    return dict(span.attributes or {})


def _named(spans: list[ReadableSpan], prefix: str) -> list[ReadableSpan]:
    return [span for span in spans if span.name.startswith(prefix)]


def _agents(spans: list[ReadableSpan], name: str) -> list[ReadableSpan]:
    """The invocation spans of agent ``name``, leaving out its model calls."""
    return [
        span
        for span in spans
        if _attrs(span).get("gen_ai.agent.name") == name
        and _attrs(span).get("convergent.invocation.span_id") == f"{_span_id(span):016x}"
    ]


def _assert_own_root(agent: ReadableSpan) -> None:
    attributes = _attrs(agent)
    own = attributes["convergent.invocation.span_id"]
    assert attributes["convergent.root_invocation.span_id"] == own, attributes
    assert "convergent.parent_invocation.span_id" not in attributes, attributes


def test_sdk_import_does_not_import_optional_temporal_dependency() -> None:
    script = """
import importlib.abc
import sys

class BlockTemporal(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'temporalio' or fullname.startswith('temporalio.'):
            raise ModuleNotFoundError('temporalio intentionally unavailable')

sys.meta_path.insert(0, BlockTemporal())
import convergent
assert 'temporalio' not in sys.modules
"""
    # Not at module level: the workflow sandbox re-imports this module and
    # refuses Path.resolve.
    repo = Path(__file__).resolve().parents[2]
    env = {**os.environ, "PYTHONPATH": str(repo / "python")}
    subprocess.run([sys.executable, "-c", script], check=True, env=env, cwd=repo)


@pytest.mark.pydantic_ai
def test_each_continued_run_starts_its_own_trajectory(
    temporal_sdk: InMemorySpanExporter,
) -> None:
    """continue-as-new writes the run's context into the next run's header. Kept
    as the caller's entry, it would file both runs' agents under the caller agent."""

    async def body(client: Client, task_queue: str) -> None:
        @convergent.observe(name="caller-agent", operation="agent_run")
        async def start_workflow() -> None:
            await client.execute_workflow(
                ContinueOnceWorkflow.run, False, id="continue-once", task_queue=task_queue
            )

        await start_workflow()

    _run_temporal([convergent.temporal.TracingInterceptor()], body)
    agents = _agents(list(temporal_sdk.get_finished_spans()), "temporal-agent")
    assert len(agents) == 2
    for agent in agents:
        _assert_own_root(agent)


@pytest.mark.pydantic_ai
def test_each_cron_run_starts_its_own_trajectory(temporal_sdk: InMemorySpanExporter) -> None:
    """Every cron run gets the first run's start header, which names the caller agent."""

    async def body(client: Client, task_queue: str) -> None:
        @convergent.observe(name="caller-agent", operation="agent_run")
        async def start_workflow() -> Any:
            return await client.start_workflow(
                AgentWorkflow.run, id="cron", task_queue=task_queue, cron_schedule="@every 1s"
            )

        handle = await start_workflow()
        try:
            for _ in range(200):
                if len(_agents(list(temporal_sdk.get_finished_spans()), "temporal-agent")) >= 2:
                    return
                await asyncio.sleep(0.1)
            raise AssertionError("the second cron run did not finish")
        finally:
            await handle.terminate()

    _run_temporal([convergent.temporal.TracingInterceptor()], body)
    for agent in _agents(list(temporal_sdk.get_finished_spans()), "temporal-agent")[:2]:
        _assert_own_root(agent)


@pytest.mark.pydantic_ai
def test_a_child_workflow_run_starts_its_own_trajectory(
    temporal_sdk: InMemorySpanExporter,
) -> None:
    """A child run's start header comes from the parent run, which the caller
    started. Kept as the caller's entry, it would file the child's agent under
    the caller agent."""

    async def body(client: Client, task_queue: str) -> None:
        @convergent.observe(name="caller-agent", operation="agent_run")
        async def start_workflow() -> str:
            return await client.execute_workflow(
                ParentWorkflow.run, id="parent", task_queue=task_queue
            )

        assert await start_workflow() == "success (no tool calls)"

    _run_temporal([convergent.temporal.TracingInterceptor()], body)
    (agent,) = _agents(list(temporal_sdk.get_finished_spans()), "temporal-agent")
    _assert_own_root(agent)


@pytest.mark.pydantic_ai
@pytest.mark.parametrize(
    ("starter_sends", "worker_sends", "expected"),
    [
        pytest.param(True, False, None, id="starter-only"),
        pytest.param(False, True, None, id="worker-only"),
        pytest.param(True, True, "chat-42", id="both"),
    ],
)
def test_the_session_travels_only_when_both_interceptors_send_it(
    temporal_sdk: InMemorySpanExporter,
    starter_sends: bool,
    worker_sends: bool,
    expected: str | None,
) -> None:
    """The session id is off by default. It reaches the run's agent only when the
    starter's interceptor writes it into the start header and the worker's
    interceptor writes it on into the activity's header."""

    def interceptor(sends: bool) -> convergent.temporal.TracingInterceptor:
        if sends:
            return convergent.temporal.TracingInterceptor(send_session=True)
        return convergent.temporal.TracingInterceptor()

    async def body(client: Client, task_queue: str) -> None:
        @convergent.observe(name="caller-agent", operation="agent_run")
        async def start_workflow() -> str:
            return await client.execute_workflow(
                AgentWorkflow.run, id="session", task_queue=task_queue
            )

        with convergent.session("chat-42"):
            assert await start_workflow() == "success (no tool calls)"

    _run_temporal(
        [interceptor(worker_sends)],
        body,
        starter_interceptors=[interceptor(starter_sends)],
    )
    (agent,) = _agents(list(temporal_sdk.get_finished_spans()), "temporal-agent")
    assert _attrs(agent).get("convergent.session.id") == expected


def test_the_callers_baggage_reaches_the_runs_activities(
    temporal_sdk: InMemorySpanExporter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The headers use temporalio's own W3C trace context and baggage, so a
    process-global propagator without baggage (``OTEL_PROPAGATORS=tracecontext``)
    does not drop the caller's baggage."""
    monkeypatch.setattr(propagate, "_HTTP_TEXT_FORMAT", TraceContextTextMapPropagator())
    seen: list[str | None] = []

    async def body(client: Client, task_queue: str) -> None:
        token = otel_context.attach(baggage.set_baggage("caller.team", "support"))
        try:
            seen.append(
                await client.execute_workflow(
                    BaggageWorkflow.run, id="baggage", task_queue=task_queue
                )
            )
        finally:
            otel_context.detach(token)

    _run_temporal([convergent.temporal.TracingInterceptor()], body)
    assert seen == ["support"]


def _raise_runtime_error(*args: object, **kwargs: object) -> None:
    raise RuntimeError("temporalio changed")


def test_an_override_that_raises_falls_back_and_warns_once(
    temporal_sdk: InMemorySpanExporter,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A temporalio upgrade that breaks the override of its private method must not
    stall a customer's workflow: the run keeps temporalio's own context, so its
    RunWorkflow span stays under StartWorkflow and the caller's baggage still
    reaches its activities, with one warning."""
    from convergent import temporal

    monkeypatch.setattr(temporal, "_fresh_carrier", _raise_runtime_error)
    seen: list[str | None] = []

    async def body(client: Client, task_queue: str) -> None:
        token = otel_context.attach(baggage.set_baggage("caller.team", "support"))
        try:
            for run in ("first", "second"):
                seen.append(
                    await client.execute_workflow(
                        BaggageWorkflow.run,
                        id=f"fallback-{run}",
                        task_queue=task_queue,
                        run_timeout=timedelta(seconds=20),
                    )
                )
        finally:
            otel_context.detach(token)

    with caplog.at_level("WARNING", logger="convergent.sdk"):
        _run_temporal([temporal.TracingInterceptor()], body)

    assert seen == ["support", "support"]
    spans = list(temporal_sdk.get_finished_spans())
    starts = {_span_id(span) for span in _named(spans, "StartWorkflow:")}
    assert len(starts) == 2
    runs = _named(spans, "RunWorkflow:")
    assert len(runs) == 2, [span.name for span in spans]
    assert all(run.parent is not None and run.parent.span_id in starts for run in runs)
    warnings = [r.getMessage() for r in caplog.records if "convergent.temporal" in r.getMessage()]
    assert len(warnings) == 1, warnings
    assert "RuntimeError" in warnings[0]


def test_a_temporalio_without_the_overridden_method_warns_once(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A later temporalio 1.x that renames the private method the override replaces
    would never call it, so every later run would nest under its first caller; the
    interceptor says so once instead of failing silently."""
    from temporalio.contrib.opentelemetry import TracingWorkflowInboundInterceptor
    from temporalio.worker import WorkflowInterceptorClassInput

    from convergent import temporal

    monkeypatch.delattr(TracingWorkflowInboundInterceptor, "_load_workflow_context_carrier")
    monkeypatch.setattr(_core, "_warned", set())
    interceptor = temporal.TracingInterceptor()
    with caplog.at_level("WARNING", logger="convergent.sdk"):
        for _ in range(2):
            interceptor.workflow_interceptor_class(
                WorkflowInterceptorClassInput(unsafe_extern_functions={})
            )

    warnings = [r.getMessage() for r in caplog.records if "convergent.temporal" in r.getMessage()]
    assert len(warnings) == 1, warnings
    assert "_load_workflow_context_carrier" in warnings[0]


def test_the_locked_temporalio_has_the_overridden_method(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Goes red on a lock bump to a temporalio that renamed the method."""
    from temporalio.worker import WorkflowInterceptorClassInput

    from convergent import temporal

    monkeypatch.setattr(_core, "_warned", set())
    with caplog.at_level("WARNING", logger="convergent.sdk"):
        temporal.TracingInterceptor().workflow_interceptor_class(
            WorkflowInterceptorClassInput(unsafe_extern_functions={})
        )

    assert [r.getMessage() for r in caplog.records if "convergent.temporal" in r.getMessage()] == []


class _ProxyTracer(trace.Tracer):
    """A tracer that is not the SDK's ``Tracer`` but hands every span to one, as
    Logfire's ``_ProxyTracer`` does."""

    def __init__(self, tracer: trace.Tracer) -> None:
        self._tracer = tracer

    def start_span(self, *args: Any, **kwargs: Any) -> trace.Span:
        return self._tracer.start_span(*args, **kwargs)

    def start_as_current_span(self, *args: Any, **kwargs: Any) -> Any:
        return self._tracer.start_as_current_span(*args, **kwargs)


class _ProxyTracerProvider(trace.TracerProvider):
    def __init__(self, provider: trace.TracerProvider) -> None:
        self._provider = provider

    def get_tracer(self, *args: Any, **kwargs: Any) -> trace.Tracer:
        return _ProxyTracer(self._provider.get_tracer(*args, **kwargs))


@pytest.mark.pydantic_ai
def test_under_a_proxy_tracer_like_logfires_each_run_starts_its_own_trajectory(
    temporal_sdk: InMemorySpanExporter, caplog: pytest.LogCaptureFixture
) -> None:
    """A setup like Logfire's proxy tracer: the provider is a proxy. Each run keeps
    temporalio's RunWorkflow span under the span that started it, and the run's
    agent starts its own trajectory, not the calling agent's."""
    provider = _core.active_provider()
    assert provider is not None
    _reset_otel()
    trace.set_tracer_provider(_ProxyTracerProvider(provider))

    async def body(client: Client, task_queue: str) -> None:
        @convergent.observe(name="caller-agent", operation="agent_run")
        async def start_workflow(run: str) -> str:
            return await client.execute_workflow(
                AgentWorkflow.run, id=f"proxy-{run}", task_queue=task_queue
            )

        for run in ("first", "second"):
            assert await start_workflow(run) == "success (no tool calls)"

    with caplog.at_level("WARNING", logger="convergent.sdk"):
        _run_temporal([convergent.temporal.TracingInterceptor()], body)

    spans = list(temporal_sdk.get_finished_spans())
    starts = {_span_id(span) for span in _named(spans, "StartWorkflow:")}
    assert len(starts) == 2
    runs = _named(spans, "RunWorkflow:")
    assert len(runs) == 2, [span.name for span in spans]
    assert all(run.parent is not None and run.parent.span_id in starts for run in runs)
    agents = _agents(spans, "temporal-agent")
    assert len(agents) == 2, [span.name for span in spans]
    for agent in agents:
        _assert_own_root(agent)
    assert [r.getMessage() for r in caplog.records if "convergent.temporal" in r.getMessage()] == []


@pytest.mark.pydantic_ai
@pytest.mark.parametrize(
    "harness",
    [True, False],
    ids=["interceptor", "interceptor-no-harness"],
)
def test_an_agent_in_an_activity_is_under_the_workflow_agent_that_handed_off(
    temporal_sdk: InMemorySpanExporter,
    monkeypatch: pytest.MonkeyPatch,
    harness: bool,
) -> None:
    """An agent inside the workflow, under the default sandbox plus the documented
    ``convergent`` passthrough, hands off to an agent in an activity. With the
    interceptor the activity agent's root and parent are the workflow agent.

    With ``pydantic_ai_harness`` installed, as in this repo, ``PydanticAIPlugin``
    also passes ``opentelemetry`` through the sandbox. A customer without the
    harness gets only the documented ``convergent`` passthrough, which the
    no-harness case checks.
    """
    from pydantic_ai import Agent
    from pydantic_ai.durable_exec import temporal as pydantic_ai_temporal
    from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
    from pydantic_ai.models.instrumented import InstrumentationSettings

    if not harness:
        real_find_spec = pydantic_ai_temporal.find_spec

        def find_spec(name: str, *args: Any) -> Any:
            return None if name == "pydantic_ai_harness" else real_find_spec(name, *args)

        monkeypatch.setattr(pydantic_ai_temporal, "find_spec", find_spec)

    from _temporal_hand_off import HandOffWorkflow, execute_hand_off
    from _temporal_worker import WORKFLOW_FAILURE_EXCEPTION_TYPES

    async def body(client: Client, task_queue: str) -> None:
        await execute_hand_off(client, id="hand-off", task_queue=task_queue)

    Agent.instrument_all(InstrumentationSettings(tracer_provider=_core.active_provider()))
    try:
        _run_temporal(
            [convergent.temporal.TracingInterceptor()],
            body,
            workflows=[HandOffWorkflow],
            plugins=[PydanticAIPlugin()],
            workflow_failure_exception_types=WORKFLOW_FAILURE_EXCEPTION_TYPES,
        )
    finally:
        Agent.instrument_all(False)
    spans = list(temporal_sdk.get_finished_spans())
    (outer,) = _agents(spans, "workflow-agent")
    (inner,) = _agents(spans, "specialist-agent")

    _assert_own_root(outer)
    outer_id = f"{_span_id(outer):016x}"
    assert _trace(inner) == _trace(outer)
    assert _attrs(inner)["convergent.root_invocation.span_id"] == outer_id
    assert _attrs(inner)["convergent.parent_invocation.span_id"] == outer_id
