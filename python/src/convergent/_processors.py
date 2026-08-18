"""Stamps context attributes onto spans and withholds what the policy refuses.

The SDK's other processors live elsewhere: ``SemanticSpanProcessor`` in
``_semantic``, ``DeclaredAgentFilter`` in ``_egress``.

The filter sits in front of every destination the SDK set up, ``File`` and
``Console`` included. Processors the caller added to the provider still
receive every span.

``on_end`` decides each span from what the finished span carries. Three
sources answer for one condition key: the stamped mark
``convergent.attributes.<key>`` first, then the span's own bare attribute,
then the resource attribute. The first source holding the key answers.

A caller marks one request with ``span(..., context_attributes={...})``. The
pairs live in a private OpenTelemetry context value for the span's lifetime,
and :class:`ContextAttributesSpanProcessor` copies every pair onto each span
at start under ``convergent.attributes.<key>``, library spans included. The
context value stays in the process: ``inject()`` writes nothing for it, unlike
baggage, which a propagator writes into every outbound request. The stamp is a
span attribute under its own prefix, so it overwrites nothing the caller or a
library wrote, and a key named in both ``attributes=`` and
``context_attributes=`` lands twice: bare and stamped. OpenTelemetry evicts
the oldest attribute when a span passes its attribute limit. A span that loses
a required stamp that way is withheld.

The require direction fails closed. A span it cannot place is withheld.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from opentelemetry import context
from opentelemetry.context import Context
from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor

from . import _policy
from ._egress import DeclaredAgentFilter

#: Minted by ``create_key``, which namespaces it. A caller's own context values
#: cannot collide with this one.
_CONTEXT_ATTRIBUTES = context.create_key("convergent-context-attributes")

#: Every context pair is stamped under this prefix. ``set_attribute`` rejects
#: caller keys starting with ``convergent.``, so only the stamper writes here.
_MARK_PREFIX = "convergent.attributes."


class _Mark:
    """One attached ``context_attributes=`` scope, and whether it is still open.

    The liveness flag is the snapshot guard for a generator. A generator
    suspends inside its ``with`` block, so two interleaved generators detach
    their context tokens out of order, and ``context.detach`` then silently
    restores a context that still names a scope whose block already exited.
    ``detach_context`` marks the scope dead instead of trusting the restore,
    and ``context_pairs`` skips dead scopes, so a stale restore reads as no
    mark.
    """

    __slots__ = ("live", "pairs", "parent")

    def __init__(self, pairs: Mapping[str, Any], parent: _Mark | None) -> None:
        self.pairs = dict(pairs)
        self.parent = parent
        self.live = True


def attach_context(pairs: Mapping[str, Any]) -> object:
    """Attach ``pairs`` for every span started until the token is detached.

    Nested scopes merge, and the inner pair wins for a key both set. ``span()``
    validates the pairs before calling this.
    """
    parent = context.get_value(_CONTEXT_ATTRIBUTES)
    mark = _Mark(pairs, parent if isinstance(parent, _Mark) else None)
    return context.attach(context.set_value(_CONTEXT_ATTRIBUTES, mark)), mark


def detach_context(token: object) -> None:
    otel_token, mark = token  # type: ignore[misc]
    mark.live = False
    context.detach(otel_token)


def context_pairs(parent_context: Context | None = None) -> Mapping[str, Any]:
    """The pairs live in ``parent_context``, or in the context of this task.

    A span processor passes the context OpenTelemetry handed it, rather than
    reading the current one. The two differ for a span started from a context
    the caller built by hand, and the one the span was started in is the right
    answer.

    Only pairs whose block is still open count. A context can outlive the
    block that marked it -- a caller captures one, or an out-of-order detach
    restores one -- and a mark read from such a context would apply pairs the
    caller already withdrew.
    """
    value = context.get_value(_CONTEXT_ATTRIBUTES, parent_context)
    chain: list[_Mark] = []
    while isinstance(value, _Mark):
        chain.append(value)
        value = value.parent
    pairs: dict[str, Any] = {}
    for mark in reversed(chain):
        if mark.live:
            pairs.update(mark.pairs)
    return pairs


def wrap(
    policy: _policy.Policy | None,
    agents: Sequence[str] | None,
    destinations: Sequence[SpanProcessor],
) -> tuple[SpanProcessor, ...]:
    """The destinations behind the filters.

    The agent filter wraps the policy filter, because
    ``DeclaredAgentFilter.on_end`` must pop its table entry for every span the
    provider starts.
    """
    wrapped = tuple(destinations)
    if policy is not None:
        wrapped = (FilterSpanProcessor(policy, wrapped),)
    if agents is not None:
        wrapped = (DeclaredAgentFilter(agents, wrapped),)
    return wrapped


class ContextAttributesSpanProcessor(SpanProcessor):
    """Copies every context pair onto each span at start.

    ``span()`` is the key's only writer and validates every pair before it
    attaches, so the exact-type check below is a cheap last guard in front of
    ``set_attribute`` rather than a validation layer.
    """

    def on_start(self, span: Span, parent_context: Context | None = None) -> None:
        # Guarded the way FilterSpanProcessor.on_end is: the tracer provider
        # calls its processors in a bare loop, so an exception here would
        # reach the caller's application code. Warn once and stamp nothing.
        try:
            for key, value in context_pairs(parent_context).items():
                if _policy._is_attribute_value(value):
                    span.set_attribute(_MARK_PREFIX + key, value)
        except Exception:  # noqa: BLE001 - a raise here would break the caller's request
            from . import _core  # deferred: _core imports this module

            _core._warn_once(
                "context_stamp_failed",
                "Convergent could not stamp context attributes onto a span that "
                "started, so the span carries no mark. Tracing is degraded; your "
                "application is unaffected.",
            )

    def on_end(self, span: ReadableSpan) -> None:
        pass

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return True

    def shutdown(self) -> None:
        pass


#: The one stamper for the process. The class holds no state, so ``init()``
#: and ``ConvergentSpanProcessor`` both register this instance rather than
#: building their own. It is not a destination: it holds no queue, so no flush
#: or shutdown needs to reach it.
_STAMPER = ContextAttributesSpanProcessor()


class FilterSpanProcessor(SpanProcessor):
    """Forwards to ``destinations`` only the spans ``policy`` keeps.

    Stateless: the decision reads the finished span at ``on_end`` and nothing
    is remembered between calls. If ``agents=`` is also set, its filter runs
    first and this one sees only the spans it kept.
    """

    def __init__(self, policy: _policy.Policy, destinations: Sequence[SpanProcessor]) -> None:
        self._policy = policy
        self._destinations = tuple(destinations)

    def on_start(self, span: Span, parent_context: Context | None = None) -> None:
        for destination in self._destinations:
            destination.on_start(span, parent_context)

    def on_end(self, span: ReadableSpan) -> None:
        # Guarded, because nothing above catches: the tracer provider calls its
        # processors in a bare loop, so an exception here would leave
        # ``span.end()`` and reach the caller's application code.
        # Fail closed. Being unable to decide is not a reason to send.
        try:
            attributes = span.attributes or {}
            marks = {
                key[len(_MARK_PREFIX) :]: value
                for key, value in attributes.items()
                if key.startswith(_MARK_PREFIX)
            }
            keep = _policy.decide(
                self._policy,
                (marks, attributes, span.resource.attributes),
            )
        except Exception:  # noqa: BLE001 - a raise here would break the caller's request
            from . import _core  # deferred: _core imports this module

            _core._warn_once(
                "filter_decision_failed",
                "Convergent could not decide whether a span may be sent and is not "
                "sending it. Tracing is degraded; your application is unaffected.",
            )
            return
        if not keep:
            return
        for destination in self._destinations:
            destination.on_end(span)

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        # Materialized first: all() would stop flushing after the first False.
        flushed = [destination.force_flush(timeout_millis) for destination in self._destinations]
        return all(flushed)

    def shutdown(self) -> None:
        for destination in self._destinations:
            destination.shutdown()


__all__ = [
    "ContextAttributesSpanProcessor",
    "FilterSpanProcessor",
    "attach_context",
    "context_pairs",
    "detach_context",
    "wrap",
]
