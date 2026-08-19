"""Framework-neutral semantic spans consumed by the OTLP translator.

Where ``set_input`` / ``set_output`` land depends on the span's operation. On a
tool call they write ``gen_ai.tool.call.arguments`` and
``gen_ai.tool.call.result``, the GenAI keys for a tool call's own content, which
hold one value each. On every other span they write ``gen_ai.input.messages`` and
``gen_ai.output.messages``, which hold a message *array*, so a value that is not
already one is wrapped as the text content of a single message.

The message keys replaced this SDK's ``convergent.input`` /
``convergent.output``. Ingest still reads the older keys, so a span that carries
either spelling is understood.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import json
import logging
import os
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal, TypeVar, cast

from opentelemetry import context, trace
from opentelemetry.sdk.trace import Span as SDKSpan
from opentelemetry.sdk.trace import SpanProcessor
from opentelemetry.trace import (
    Span,
    Status,
    StatusCode,
    format_span_id,
    format_trace_id,
)

from . import _policy, _processors

logger = logging.getLogger("convergent.sdk")

#: The OpenTelemetry GenAI operations, keyed by the name a caller writes. Any
#: other string is recorded as given -- see :func:`_semconv_operation`.
Operation = Literal[
    "agent_run",
    "model_call",
    "tool_call",
    "retrieval",
    "embeddings",
    "workflow",
    "agent_create",
    "text_completion",
    "generate_content",
]

#: What ``observe()`` and ``span()`` accept. Deliberately wider than
#: :data:`Operation`: a custom operation is a documented feature -- a guardrail
#: check or an approval step is real work, recorded verbatim -- so narrowing this
#: to the ``Literal`` would make a supported call a type error. The cost is that a
#: typo like ``"tool_cal"`` type-checks; it is reported at runtime instead, and
#: annotating a parameter :data:`Operation` is how a caller opts into the stricter
#: check.
AnyOperation = Operation | str

#: Returned by a context resolver when the pairs could not be worked out.
#: ``span()`` reads it as "withhold under any filter", never as "no pairs".
_RESOLUTION_FAILED: Mapping[str, Any] = MappingProxyType({})

#: What the decorators accept for ``context_attributes=``: the pairs
#: themselves, or a callable resolved on every call from the decorated
#: function's arguments, bound to their parameter names.
ContextAttributes = (
    Mapping[str, str | bool | int | float] | Callable[..., Mapping[str, str | bool | int | float]]
)

#: The tracer name every span the SDK opens carries in its instrumentation
#: scope. ``set_context_attributes`` recognizes the SDK's own spans by it.
_TRACER_NAME = "convergent.sdk"

_OPERATIONS: dict[str, str] = {
    "agent_run": "invoke_agent",
    "model_call": "chat",
    "tool_call": "execute_tool",
    "retrieval": "retrieval",
    "embeddings": "embeddings",
    "workflow": "invoke_workflow",
    "agent_create": "create_agent",
    "text_completion": "text_completion",
    "generate_content": "generate_content",
}
_SEMANTIC_VERSION = "1"
_EXECUTION_KEY = "convergent.execution.id"

#: Marks a span whose message content ``set_input``/``set_output`` wrote.
#:
#: Deliberately *not* ``convergent.semantic.version``, which is the obvious
#: candidate and the wrong one: ``SemanticSpanProcessor`` stamps that on every span
#: declaring a ``gen_ai.operation.name``, and it sits on the process provider every
#: framework emits through -- so a pydantic-ai or OpenLLMetry span carries it
#: whenever that instrumentation supplies its attributes at span creation, which is
#: the normal case. This key is written at exactly one place, so it means what it says.
#:
#: Read by our ingest to decide whether to mirror the
#: standard message keys onto the ``convergent.*`` ones. A caller cannot forge it:
#: ``set_attribute`` rejects every ``convergent.``-prefixed key.
_CONTENT_SOURCE_KEY = "convergent.content.source"
_CONTENT_SOURCE = "sdk"

#: The ``gen_ai.operation.name`` of a tool call. Tool content goes to the GenAI
#: tool keys rather than the message keys, and ingest keys off the same value.
_TOOL_OPERATION = "execute_tool"

#: What a tool span says its tool is when the caller does not say. The
#: conventions' vocabulary is ``function`` for a tool the client runs,
#: ``extension`` for one the agent side runs against an external API, and
#: ``datastore`` for one that queries external data. ``span(operation="tool_call")``
#: wraps a callable running in the caller's own process, so ``function`` is the
#: honest answer for what the SDK can see. A caller wrapping anything else passes
#: ``gen_ai.tool.type`` in ``attributes`` and keeps their own answer.
_DEFAULT_TOOL_TYPE = "function"

#: The OpenTelemetry schema the attribute names above are taken from, carried on
#: the tracer so a consumer reads our spans against the version we wrote them for.
#:
#: 1.40.0 is the version the collector's ``gen_ai_normalizer`` processor stamps on
#: a span it writes, so a span of ours and a span that processor normalized
#: describe themselves the same way. That processor leaves a scope that already
#: names a schema alone, so writing it here also stops it restamping ours.
_GENAI_SCHEMA_URL = "https://opentelemetry.io/schemas/1.40.0"

_NAME_LIMIT = 128
#: Longest tool call id that ships. A provider id runs about thirty characters
#: (``call_`` or ``toolu_`` and a short token), so this is generous for a real one
#: and short enough that the field cannot carry a payload.
_CALL_ID_LIMIT = 256
#: Keys the SDK owns. ``set_attribute`` drops these rather than let a caller write
#: a shape our own reader would reject: the four content keys are written by
#: ``set_input``/``set_output``, and a second value on the same key either
#: overwrites what the caller recorded or, on the message keys, arrives as a
#: scalar that ``decode_messages`` returns ``None`` for and the content is lost.
_RESERVED_ATTRIBUTE_KEYS = frozenset(
    {
        "gen_ai.operation.name",
        "gen_ai.agent.name",
        "gen_ai.agent.version",
        "gen_ai.tool.name",
        "gen_ai.input.messages",
        "gen_ai.output.messages",
        "gen_ai.tool.call.arguments",
        "gen_ai.tool.call.result",
    }
)
_CONVERSATION_ID_INPUT_KEYS = ("gen_ai.conversation.id", "session.id")
_CONVERSATION_ID_WRITE_KEYS = ("gen_ai.conversation.id", "convergent.session.id")
_CONTROL_FLOW_EXCEPTIONS = (GeneratorExit, asyncio.CancelledError)

#: Reasons already reported in this process. Bounded by the number of distinct
#: reasons, never by caller input. See :func:`_report_once`.
_reported: set[str] = set()

#: Spans this SDK has started and not yet ended, in the calling context. Read by
#: ``flush()`` through :func:`has_open_span`.
#:
#: A ContextVar rather than a process-wide counter, so a ``flush()`` on a different
#: thread or task than the span reads zero: a service whose spans and whose flush
#: live on different threads is doing nothing wrong, and a process-wide count would
#: warn at it on every call. The cost is that a thread started *inside* a span also
#: reads zero, so a flush there is a real miss this does not catch.
_open_span_count: ContextVar[int] = ContextVar("convergent_open_span_count", default=0)

F = TypeVar("F", bound=Callable[..., Any])


@dataclass(frozen=True)
class TraceRef:
    trace_id: str
    span_id: str
    permalink: str | None


class SpanHandle:
    """What ``span()`` yields, and how a caller records content on it.

    ``operation`` is the span's ``gen_ai.operation.name``. It decides where
    ``set_input`` and ``set_output`` write, because the GenAI conventions put a
    tool call's arguments and result under keys of their own and chat messages
    under theirs.
    """

    def __init__(self, span: Span, *, operation: str = "") -> None:
        self._span = span
        self._operation = operation
        self._ref = _trace_ref(span)

    @property
    def trace_id(self) -> str | None:
        return self._ref.trace_id if self._ref else None

    @property
    def span_id(self) -> str | None:
        return self._ref.span_id if self._ref else None

    @property
    def permalink(self) -> str | None:
        return self._ref.permalink if self._ref else None

    def set_attribute(self, key: str, value: Any) -> None:
        if not _accepted(key, value, "set_attribute()"):
            return
        if key in _CONVERSATION_ID_INPUT_KEYS:
            for alias in _CONVERSATION_ID_WRITE_KEYS:
                self._span.set_attribute(alias, value)
            return
        self._span.set_attribute(key, value)

    def set_context_attributes(self, attributes: Mapping[str, str | bool | int | float]) -> None:
        """Mark this span and every span started after this call inside it.

        Use it for a value you only know mid-request, such as a customer id
        looked up from a token. Spans started before this call keep what they
        had. Call it inside the span it marks, on the thread the span runs on.
        The span must be one the SDK opened. On any other span the call is
        refused and logged, because nothing releases the pairs when that span
        ends. Each pair passes the same guards as ``context_attributes=``.
        When no usable pair remains, any span filter withholds the span.
        Never raises.
        """
        scope = getattr(self._span, "instrumentation_scope", None)
        if scope is None or scope.name != _TRACER_NAME:
            _report_once(
                "set_context_attributes_foreign_span",
                "Convergent ignored set_context_attributes(): this is not a span "
                "Convergent opened, so nothing releases the pairs when it ends. "
                "Call it on the run's own handle, or open a span() here.",
            )
            return
        if trace.get_current_span() is not self._span or not self._span.is_recording():
            _report_once(
                "set_context_attributes_off_span",
                "Convergent ignored set_context_attributes(): the handle's span "
                "is not the one running here. Call it inside the span it marks, "
                "on the thread that span is running on.",
            )
            return
        pairs: dict[str, Any] = {}
        if isinstance(attributes, Mapping):
            pairs = {
                key: value
                for key, value in attributes.items()
                if _accepted_context(key, value, "set_context_attributes()")
            }
            if attributes and not pairs:
                pairs = {_processors.UNRESOLVED: True}
        else:
            _report_once(
                "set_context_attributes_not_mapping",
                f"Convergent could not use set_context_attributes(): it takes a "
                f"mapping of pairs, not {type(attributes).__name__}. The span is "
                "recorded for your own destinations and withheld under any span "
                "filter.",
            )
            pairs = {_processors.UNRESOLVED: True}
        if pairs:
            _processors.mark_span(self._span, pairs)

    def set_input(self, value: Any) -> None:
        if self._operation == _TOOL_OPERATION:
            self._span.set_attribute("gen_ai.tool.call.arguments", _text(value))
            return
        self._span.set_attribute("gen_ai.input.messages", _messages("user", value))
        self._span.set_attribute(_CONTENT_SOURCE_KEY, _CONTENT_SOURCE)

    def set_output(self, value: Any) -> None:
        if self._operation == _TOOL_OPERATION:
            self._span.set_attribute("gen_ai.tool.call.result", _text(value))
            return
        self._span.set_attribute("gen_ai.output.messages", _messages("assistant", value))
        self._span.set_attribute(_CONTENT_SOURCE_KEY, _CONTENT_SOURCE)

    def set_tool_call_id(self, call_id: str) -> None:
        """Record the id the model issued for this tool call.

        Ingest pairs the model's request for a call with the call itself on this
        id, so a run that omits it shows one tool call as two rows.

        A call id is model output, so a prompt-injected model can put text of
        its choosing in it. The length bound is what keeps that in check -- a
        real id from any provider is around thirty characters, so anything past
        ``_CALL_ID_LIMIT`` is not an id and does not ship.
        """
        if not isinstance(call_id, str) or not call_id or len(call_id) > _CALL_ID_LIMIT:
            _report_once(
                "invalid_tool_call_id",
                f"Convergent ignored the tool call id {call_id!r:.80}: it must be a "
                f"string of 1-{_CALL_ID_LIMIT} characters.",
            )
            return
        self._span.set_attribute("gen_ai.tool.call.id", call_id)


class _NoOpSpanHandle:
    trace_id: str | None = None
    span_id: str | None = None
    permalink: str | None = None

    def set_attribute(self, key: str, value: Any) -> None:
        pass

    def set_context_attributes(self, attributes: Mapping[str, str | bool | int | float]) -> None:
        pass

    def set_input(self, value: Any) -> None:
        pass

    def set_output(self, value: Any) -> None:
        pass

    def set_tool_call_id(self, call_id: str) -> None:
        pass


def observe(
    *,
    name: str,
    operation: AnyOperation,
    attributes: Mapping[str, str | bool | int | float] | None = None,
    context_attributes: ContextAttributes | None = None,
) -> Callable[[F], F]:
    """Record each call of the decorated function as one span.

    For ``agent_run``, ``name`` is the agent's identity. Keep it stable, like a
    class name: ``"support-agent"``, never ``f"support-agent-{user_id}"``. Every
    distinct name becomes its own agent, so an id in the name floods the agent
    list and leaves nothing to compare across traces. The varying part goes in
    ``attributes``.

    ``attributes`` land on this one span. ``context_attributes`` land on this
    span and every span started while the call runs, library spans included,
    and stay in the process -- see :func:`span`.

    ``context_attributes`` may also be a callable. It is called once per call,
    with the decorated function's own arguments, and the mapping it returns
    attaches to this span and to every span started while the call runs. A
    callable that raises, or that returns something that is not a Mapping, is
    logged once and the span is recorded with no context pairs.

    Never raises. A name outside 1-128 characters or an unrecognized operation is
    logged and the span is still recorded -- ingest is where a bad name is
    rejected, because failing there costs the caller nothing while raising here
    would cost them a request.
    """
    _report_invalid(name, operation)

    def decorate(function: F) -> F:
        resolve = _context_resolver(context_attributes, function)

        def opened(args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
            resolved = context_attributes
            if resolve is not None:
                from . import _core

                # Resolved only when a provider exists: with tracing off, the
                # caller's callable must not run at all.
                resolved = resolve(args, kwargs) if _core.snapshot().provider is not None else None
            return span(
                name=name,
                operation=operation,
                attributes=attributes,
                context_attributes=resolved,
            )

        if inspect.isasyncgenfunction(function):

            @functools.wraps(function)
            async def async_generator_wrapper(*args: Any, **kwargs: Any) -> Any:
                with opened(args, kwargs):
                    async for item in function(*args, **kwargs):
                        yield item

            return cast(F, async_generator_wrapper)

        if inspect.isgeneratorfunction(function):

            @functools.wraps(function)
            def generator_wrapper(*args: Any, **kwargs: Any) -> Any:
                with opened(args, kwargs):
                    yield from function(*args, **kwargs)

            return cast(F, generator_wrapper)

        if inspect.iscoroutinefunction(function):

            @functools.wraps(function)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                with opened(args, kwargs):
                    return await function(*args, **kwargs)

            return cast(F, async_wrapper)

        @functools.wraps(function)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            with opened(args, kwargs):
                return function(*args, **kwargs)

        return cast(F, wrapper)

    return decorate


def _context_resolver(
    context_attributes: ContextAttributes | None,
    function: Callable[..., Any],
) -> Callable[[tuple[Any, ...], dict[str, Any]], Mapping[str, Any]] | None:
    """A per-call resolver for a callable ``context_attributes``, else ``None``.

    Built once at decoration, so the mapping form pays nothing and the
    signature is read one time. The resolver binds each call's arguments to
    the decorated function's parameter names and hands the callable keywords,
    so ``lambda customer_id, **_:`` names the parameter it wants wherever the
    caller put it. Never raises: every failure is reported once and resolves
    to :data:`_RESOLUTION_FAILED`, which withholds the span under any filter.
    """
    if isinstance(context_attributes, Mapping) or not callable(context_attributes):
        return None
    try:
        signature = inspect.signature(function)
    except (TypeError, ValueError):

        def unresolvable(*_: object) -> Mapping[str, Any]:
            _report_once(
                "context_attributes_no_signature",
                "Convergent could not read the decorated function's signature to "
                "resolve context_attributes=. The span is recorded for your own "
                "destinations and withheld under any span filter.",
            )
            return _RESOLUTION_FAILED

        return unresolvable

    def resolve(args: tuple[Any, ...], kwargs: dict[str, Any]) -> Mapping[str, Any]:
        try:
            bound = signature.bind(*args, **kwargs)
            bound.apply_defaults()
        except TypeError:
            _report_once(
                "context_attributes_bind_failed",
                "Convergent could not bind the call's arguments to the decorated "
                "function's parameters to resolve context_attributes=. The span "
                "is recorded for your own destinations and withheld under any "
                "span filter.",
            )
            return _RESOLUTION_FAILED
        try:
            resolved = context_attributes(**bound.arguments)
        except Exception as error:
            _report_once(
                "context_attributes_callable_raised",
                f"Convergent could not resolve the context_attributes= callable: "
                f"it raised {type(error).__name__}. The span is recorded for your "
                "own destinations and withheld under any span filter.",
            )
            return _RESOLUTION_FAILED
        if not isinstance(resolved, Mapping):
            _report_once(
                "context_attributes_callable_result",
                f"Convergent could not use the context_attributes= callable's "
                f"result: it must be a Mapping, not {type(resolved).__name__}. "
                "The span is recorded for your own destinations and withheld "
                "under any span filter.",
            )
            return _RESOLUTION_FAILED
        usable = {key: value for key, value in resolved.items() if _accepted_context(key, value)}
        if resolved and not usable:
            _report_once(
                "context_attributes_callable_unusable",
                "Convergent could not use any pair the context_attributes= "
                "callable returned. The span is recorded for your own "
                "destinations and withheld under any span filter.",
            )
            return _RESOLUTION_FAILED
        return usable

    return resolve


def agent(
    *,
    name: str,
    attributes: Mapping[str, str | bool | int | float] | None = None,
    context_attributes: ContextAttributes | None = None,
) -> Callable[[F], F]:
    """Record each call of the decorated function as one agent run.

    An alias for ``observe(name=name, operation="agent_run", attributes=...)``,
    and everything :func:`observe` says holds here. ``name`` is required and
    keyword-only on purpose: it is the agent's workspace identity, and deriving
    it from the function name would let a rename in the code rename the agent.
    """
    return observe(
        name=name,
        operation="agent_run",
        attributes=attributes,
        context_attributes=context_attributes,
    )


def tool(
    *,
    name: str | None = None,
    attributes: Mapping[str, str | bool | int | float] | None = None,
    context_attributes: ContextAttributes | None = None,
) -> Callable[[F], F]:
    """Record each call of the decorated function as one tool call.

    An alias for ``observe(operation="tool_call", ...)``, and everything
    :func:`observe` says holds here. ``name`` may be left out, in which case the
    decorated function's ``__name__`` is the tool's name -- a function name is a
    stable identity, which is what the naming rule asks for. Written as
    ``@tool()`` or ``@tool(name="lookup_invoice")``; the bare ``@tool`` form is
    not supported.
    """

    def decorate(function: F) -> F:
        tool_name = name if name is not None else getattr(function, "__name__", "")
        return observe(
            name=tool_name,
            operation="tool_call",
            attributes=attributes,
            context_attributes=context_attributes,
        )(function)

    return decorate


@contextmanager
def span(
    *,
    name: str,
    operation: AnyOperation,
    attributes: Mapping[str, str | bool | int | float] | None = None,
    context_attributes: ContextAttributes | None = None,
) -> Iterator[SpanHandle | _NoOpSpanHandle]:
    """Record one span for the body of the ``with`` block.

    Same naming rule as :func:`observe`: for ``agent_run``, ``name`` is a stable
    agent identity, never a per-request or per-user string, with the varying part
    in ``attributes``. Never raises, for the same reason :func:`observe` does not.

    ``attributes`` land on this one span. ``context_attributes`` land on this
    span and every span started inside the block, library spans included: the
    pairs live in the OpenTelemetry context for exactly the block's lifetime,
    and a processor stamps each pair onto every span at start as
    ``convergent.attributes.<key>``, so a stamp overwrites no attribute.
    Nested blocks merge, and the inner pair wins for a key both set. When one
    call names a key in both parameters, the span carries both: the bare key
    from ``attributes`` and the stamped key from ``context_attributes``, and
    the filter reads the stamped key first. The pairs stay in the process:
    nothing writes them to outbound requests. A key the SDK owns, or a value
    that is not a plain ``str``, ``bool``, ``int``, or ``float``, is dropped
    and logged once, the way an invalid ``attributes`` entry is.
    """
    _report_invalid(name, operation)
    from . import _core

    state = _core.snapshot()
    if state.provider is None:
        yield _NoOpSpanHandle()
        return

    operation_name = _semconv_operation(operation)
    span_attributes: dict[str, Any] = {
        "gen_ai.operation.name": operation_name,
        "convergent.semantic.version": _SEMANTIC_VERSION,
    }
    if isinstance(attributes, Mapping):
        span_attributes.update({k: v for k, v in attributes.items() if _accepted(k, v)})
        for key in _CONVERSATION_ID_INPUT_KEYS:
            if key in span_attributes:
                value = span_attributes[key]
                span_attributes.pop("session.id", None)
                for alias in _CONVERSATION_ID_WRITE_KEYS:
                    span_attributes[alias] = value
                break
    span_name = name
    if operation_name == "invoke_agent":
        span_attributes["gen_ai.agent.name"] = name
        if state.release is not None:
            span_attributes["gen_ai.agent.version"] = state.release
        span_name = f"invoke_agent {name}"
    elif operation_name == _TOOL_OPERATION:
        span_attributes["gen_ai.tool.name"] = name
        span_attributes.setdefault("gen_ai.tool.type", _DEFAULT_TOOL_TYPE)
        span_name = f"execute_tool {name}"

    context_pairs: dict[str, Any] = {}
    # Checked before the Mapping branch: the sentinel is itself a Mapping.
    if context_attributes is _RESOLUTION_FAILED:
        context_pairs = {_processors.UNRESOLVED: True}
    elif isinstance(context_attributes, Mapping):
        context_pairs = {
            key: value for key, value in context_attributes.items() if _accepted_context(key, value)
        }
    elif context_attributes is not None:
        _report_once(
            "span_context_attributes_not_mapping",
            f"Convergent could not use span()'s context_attributes=: it must be a "
            f"mapping of pairs, not {type(context_attributes).__name__}. A "
            "callable belongs on a decorator, which has call arguments to hand "
            "it. The span is recorded for your own destinations and withheld "
            "under any span filter.",
        )
        context_pairs = {_processors.UNRESOLVED: True}

    tracer = state.provider.get_tracer(_TRACER_NAME, schema_url=_GENAI_SCHEMA_URL)
    open_before = _open_span_count.get()
    _open_span_count.set(open_before + 1)
    # Attached before the span starts, so the stamper sees the pairs on this
    # span too, and detached when the block exits, which is the pairs' lifetime.
    token = _processors.attach_context(context_pairs) if context_pairs else None
    try:
        with tracer.start_as_current_span(
            span_name,
            attributes=span_attributes,
            record_exception=False,
            set_status_on_exception=False,
        ) as current:
            _warn_on_split_trace(current, open_before)
            handle = SpanHandle(current, operation=operation_name)
            try:
                yield handle
            except _CONTROL_FLOW_EXCEPTIONS:
                raise
            except BaseException as error:
                current.set_status(Status(StatusCode.ERROR, type(error).__name__))
                raise
            finally:
                _processors.release_marks(current)
    finally:
        # Clamped because a generator started in one context and closed in another
        # decrements a count it never incremented, and a negative would then hide a
        # span that really is open.
        _open_span_count.set(max(_open_span_count.get() - 1, 0))
        if token is not None:
            _processors.detach_context(token)


def _warn_on_split_trace(current: Span, open_before: int) -> None:
    """Warn when ``current`` began a new trace with another SDK span still open.

    ``open_before`` is how many spans :func:`span` had open in this context when
    this one started. Above zero, that span's OpenTelemetry context was attached
    here, so a span starting now inherits its trace id and records it as a parent.
    A ``parent`` of ``None`` says it did not, and OpenTelemetry only leaves that
    empty when the span sitting in the context has an invalid span context, which
    is what a span nobody is recording carries. So something opened one of those
    between the two spans and every span after it went to a trace of its own.

    Two genuinely parallel root spans cannot reach the warning. A thread starts
    with an empty context, so ``open_before`` reads zero on each of them, and a
    task or a copied context carries the enclosing span along with the count.
    A parent that is not recorded but still carries a valid span context, such as
    a sampled out one, keeps its trace id on the child and is not a split either.

    A ``current`` that is not an ``opentelemetry.sdk`` span is a span this
    provider's sampler dropped. It carries no ``parent`` to read, and nothing is
    exporting it, so there is no trace to split.
    """
    if open_before == 0 or not isinstance(current, SDKSpan) or current.parent is not None:
        return
    from . import _core

    _core._warn_once(
        "split_trace",
        "Convergent started a span as the root of a new trace while another Convergent "
        "span was still open, so this run's spans are splitting into separate traces "
        "instead of joining one. Something between them opened a span that "
        "OpenTelemetry is not recording, and a span started under one of those begins a "
        "new trace. pydantic-evals opens one around every case, and so does any library "
        "that opens spans while nothing is configured to record them. Open the "
        "Convergent span outside that library's scope, or drop the wrapper span it "
        "opens, so both spans share one trace.",
    )


def has_open_span() -> bool:
    """True when a span :func:`span` started in this context has not ended yet."""
    return _open_span_count.get() > 0


def forget_open_spans() -> None:
    """Drop this context's open-span count.

    A forked child inherits the count without inheriting the frame that would end
    the span, so its own correct ``flush()`` would otherwise report an enclosing span
    the child does not have. Forking inside an observed function and flushing in the
    child is a documented pattern.
    """
    _open_span_count.set(0)


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=forget_open_spans)


def current_trace() -> TraceRef | None:
    """Where the active span sits, or ``None`` when no span is active."""
    return _trace_ref(trace.get_current_span())


def current_span() -> SpanHandle | _NoOpSpanHandle:
    """A handle on the innermost active span, whatever created it.

    This is how a function decorated with :func:`observe`, :func:`agent`, or
    :func:`tool` records content on its own span, since the decorator yields no
    handle. The handle drops and logs a reserved attribute key, the way the one
    :func:`span` yields does. ``set_context_attributes`` works only on a span
    the SDK opened, so on a framework's span it is refused and logged.

    The innermost active span is whatever the ambient OpenTelemetry context
    holds, so it may be a framework's span rather than one this SDK opened, and
    in a callback or a spawned task it may not be the span the caller expects.
    Never returns ``None`` and never raises: with no span active, or with
    tracing unconfigured, the handle's methods do nothing, mirroring
    :func:`span` when tracing is disabled.
    """
    active = trace.get_current_span()
    if not active.get_span_context().is_valid:
        return _NoOpSpanHandle()
    from . import _core

    state = _core.snapshot()
    if state.provider is None:
        return _NoOpSpanHandle()
    # The handle routes set_input/set_output on the span's operation, so read it
    # back from the span. Best-effort: only an SDK-backed span exposes its
    # attributes, and a foreign one falls back to the message keys.
    attributes = getattr(active, "attributes", None)
    operation = attributes.get("gen_ai.operation.name") if isinstance(attributes, Mapping) else ""
    return SpanHandle(active, operation=operation if isinstance(operation, str) else "")


def _trace_ref(target: Span) -> TraceRef | None:
    span_context = target.get_span_context()
    if not span_context.is_valid:
        return None
    return TraceRef(
        trace_id=format_trace_id(span_context.trace_id),
        span_id=format_span_id(span_context.span_id),
        # No route displays a single trace, so any link built here would 404.
        permalink=None,
    )


def _accepted(key: Any, value: Any, parameter: str = "attributes=") -> bool:
    if not _accepted_key(key, parameter):
        return False
    if not _valid_attribute(value):
        _report_once(
            f"invalid_attribute_value:{parameter}",
            f"Convergent ignored the {parameter} entry {key!r}: a span attribute "
            f"must be a string, bool, int, float, or a flat sequence of those, not "
            f"{type(value).__name__}.",
        )
        return False
    if isinstance(value, str | bool | int | float) and not _policy._is_attribute_value(value):
        # A subclass such as a StrEnum or IntEnum member records fine, but the
        # attribute filter compares by exact type, so no rule can ever match
        # the recorded value. Recorded anyway, warned once.
        _report_once(
            f"subclass_attribute_value:{parameter}",
            f"Convergent recorded the {parameter} entry {key!r}, whose value "
            f"subclasses a plain type ({type(value).__name__}). "
            "require_span_attributes= and reject_span_attributes= match by "
            "exact type, so no filter rule can match this value. For an enum "
            "member, pass '.value'.",
            level=logging.WARNING,
        )
    return True


def _accepted_context(key: Any, value: Any, parameter: str = "context_attributes=") -> bool:
    """Whether one ``context_attributes`` pair may attach.

    The same error path an invalid ``attributes`` entry takes: the pair is
    dropped and logged once per reason. The value check is stricter, exact
    plain scalars only, because the filter compares by exact type, so a
    subclass such as a ``StrEnum`` member would stamp a value no rule could
    ever match.
    """
    if not _accepted_key(key, parameter):
        return False
    if not _policy._is_attribute_value(value):
        _report_once(
            "invalid_context_attribute_value",
            f"Convergent ignored the {parameter} entry {key!r}: it must be "
            f"a plain string, bool, int, or float, not {type(value).__name__}. For "
            "an enum member, pass '.value'.",
        )
        return False
    return True


def _accepted_key(key: Any, parameter: str) -> bool:
    # The report key varies by parameter, and there are three parameters, so
    # the _report_once bound holds.
    if not isinstance(key, str) or key.startswith("convergent.") or key in _RESERVED_ATTRIBUTE_KEYS:
        _report_once(
            f"reserved_attribute:{parameter}",
            f"Convergent ignored the {parameter} key {key!r}: it carries identity "
            "the SDK owns. Use a key of your own instead.",
        )
        return False
    return True


def _semconv_operation(operation: str) -> str:
    """The ``gen_ai.operation.name`` for a caller's operation.

    Anything unrecognized is recorded verbatim: a guardrail check or an approval
    step is real work worth seeing even though the standard has no name for it.
    Non-string input is stringified because an unhashable value cannot be looked
    up and nothing here may raise.
    """
    if not isinstance(operation, str):
        return str(operation)
    return _OPERATIONS.get(operation, operation)


def _report_invalid(name: Any, operation: Any) -> None:
    """Log a name or operation the caller got wrong, and let the span through.

    Deliberately not a raise. "It only fails at import" is a best case, not a
    guarantee -- a lazily imported module, a worker importing on first task, or a
    name read from configuration all move the failure into the request path.
    Ingest validates instead, where rejecting one name costs the caller nothing.
    """
    if not isinstance(name, str) or not name.strip() or len(name) > _NAME_LIMIT:
        _report_once(
            "invalid_name",
            f"Convergent recorded a span with an unusable name ({name!r}). A name must "
            f"be 1-{_NAME_LIMIT} characters; ingest will reject this one. The span was "
            "still sent.",
        )
    # isinstance first: an unhashable operation cannot be looked up in a dict, and
    # this function must not raise for any input. A semconv name such as
    # ``execute_tool`` is recognized as well as the caller vocabulary that maps to
    # it, because ``span()`` gives the two the same treatment.
    if not isinstance(operation, str) or not _recognized_operation(operation):
        # WARNING, not ERROR. A custom operation is documented as supported --
        # _semconv_operation records it verbatim and SemanticSpanProcessor still
        # joins its execution -- so ERROR here would train callers to ignore the
        # level we use for things that are actually broken.
        _report_once(
            "invalid_operation",
            f"Convergent recorded a span with the unrecognized operation {operation!r}. "
            f"It is stored as given but will not map to a first-class concept. Known "
            f"operations: {', '.join(_OPERATIONS)}.",
            level=logging.WARNING,
        )


def _recognized_operation(operation: str) -> bool:
    return operation in _OPERATIONS or operation in _OPERATIONS.values()


def _report_once(key: str, message: str, *, level: int = logging.ERROR) -> None:
    """Log once per *reason* per process, at ERROR unless told otherwise.

    ``key`` must be a fixed reason, never anything derived from caller input.
    Keying on the offending name or attribute would make this set grow without
    bound for a caller passing ``f"agent-{uuid4()}"`` in a loop -- the exact
    high-cardinality leak our own docs warn customers about, reproduced inside the
    SDK, and reachable precisely because we now tolerate bad input instead of
    rejecting it. Tolerating must not itself leak.

    The cost is that only the first offending value appears in the logs. That is
    the right trade: ``check()`` reports malformed and undeclared names from the
    server, which sees all of them and is the better channel for the full list.

    Rate-limited because ``span()`` re-checks its arguments on every call, so an
    unusable name in a loop would emit a line per span and bury its own signal.
    """
    if key in _reported:
        return
    _reported.add(key)
    logger.log(level, message, extra={"event": "convergent.sdk.invalid_input"})


def _valid_attribute(value: Any) -> bool:
    if isinstance(value, str | bool | int | float):
        return True
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return all(isinstance(item, str | bool | int | float) for item in value)
    return False


def _messages(role: str, value: Any) -> str:
    """Encode ``value`` as a ``gen_ai.*.messages`` attribute.

    That attribute is a message *array*, and our own reader drops anything that
    does not decode to a list -- so a caller's bare dict cannot go through as-is.
    A value already shaped like a message list passes untouched; anything else
    becomes the text content of one message. Nothing is filtered or bounded here.
    """
    if _is_message_list(value):
        return _dump(value)
    return _dump([{"role": role, "parts": [{"type": "text", "content": _text(value)}]}])


def _is_message_list(value: Any) -> bool:
    return (
        isinstance(value, list)
        and bool(value)
        and all(isinstance(item, Mapping) and "role" in item for item in value)
    )


def _text(value: Any) -> str:
    """A string is recorded as it stands; anything else as JSON."""
    return value if isinstance(value, str) else _dump(value)


def _dump(value: Any) -> str:
    """JSON, never raising. ``default`` covers an unserializable object; the
    first except covers what it cannot, such as a circular reference.

    The second except covers the fallback itself. ``repr`` runs the caller's own
    code, so an object whose ``__repr__`` raises defeats both ``default=repr``
    and the fallback, and the exception escapes into the caller's ``set_input``
    call. That is the one contract this SDK makes.
    """
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=repr)
    except Exception:
        pass
    try:
        return repr(value)
    except Exception:
        return "<unrepresentable>"


class SemanticSpanProcessor(SpanProcessor):
    """Stamps the execution id, semantic version, and deployment identity on
    every GenAI span, and the release on an agent invocation that does not name
    its own version.

    The execution id is the span's own trace id, which is what groups one run's
    spans together. Work that crosses a process boundary keeps the same trace id
    through the standard ``traceparent`` header, so one run still groups correctly
    across processes and this SDK adds nothing of its own to what a caller sends
    out.

    The deployment identity must ride the span: when ``init()`` attaches to a
    provider the caller already owns, that provider's Resource is theirs and
    frozen, so a trace from an attached process would otherwise reach ingest
    with no deployment to link to.

    This deliberately never stamps ``gen_ai.agent.name``. :func:`span` already
    sets it for an ``agent_run``, and a framework agent names itself. An agent
    that names itself nowhere is left nameless on purpose, because ingest infers
    a better name from the trace tree or the service name than this could guess.
    """

    def __init__(
        self, release: str | None = None, deployment: Mapping[str, str] | None = None
    ) -> None:
        self._release = release
        self._deployment = dict(deployment or {})

    def on_start(
        self,
        span: SDKSpan,
        parent_context: context.Context | None = None,  # noqa: ARG002
    ) -> None:
        # Any span declaring a GenAI operation is agent work, including one whose
        # operation is not in the standard set -- a caller's own operation still
        # belongs to the execution it ran inside.
        attributes = span.attributes or {}
        operation = attributes.get("gen_ai.operation.name")
        if not isinstance(operation, str) or not operation:
            return

        span.set_attribute(_EXECUTION_KEY, f"{span.get_span_context().trace_id:032x}")
        span.set_attribute("convergent.semantic.version", _SEMANTIC_VERSION)
        for key, value in self._deployment.items():
            if key not in attributes:
                span.set_attribute(key, value)
        if (
            operation == "invoke_agent"
            and self._release is not None
            and "gen_ai.agent.version" not in attributes
        ):
            span.set_attribute("gen_ai.agent.version", self._release)

    def force_flush(self, timeout_millis: int = 30_000) -> bool:  # noqa: ARG002
        """Nothing is buffered here -- ``on_start`` sets attributes and returns.

        Stated rather than inherited because the base class does not answer the
        same way across versions: on opentelemetry-sdk 1.39, which is what an
        agent pinned to a box may carry, ``SpanProcessor.force_flush`` has a
        docstring and no body, so it returns ``None``. ``flush()`` ands every
        processor's answer together, so one ``None`` makes the whole call report
        falsy and a caller checking it concludes its spans did not make it.
        """
        return True


__all__ = [
    "ContextAttributes",
    "Operation",
    "SemanticSpanProcessor",
    "SpanHandle",
    "TraceRef",
    "agent",
    "current_span",
    "current_trace",
    "observe",
    "span",
    "tool",
]
