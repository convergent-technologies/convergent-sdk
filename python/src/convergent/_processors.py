"""Stamps context attributes onto spans and withholds what the policy refuses.

It also stamps trajectory identity: :class:`TrajectoryStampProcessor` copies the
current and root agent invocation's span ids and start times onto each span at
start, so ingest can place a span without walking its parents. A stamp that
cannot be made marks the span ``convergent.invocation.stamp_failed`` and leaves
its subtree unstamped, each child marked with that flag too.

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
pairs live in a private OpenTelemetry context value for the span's lifetime.
:class:`ContextAttributesSpanProcessor` stamps each span at start under
``convergent.attributes.<key>``, library spans included.

``session()`` uses the same context stack for one reserved exception:
``convergent.session.id`` is stamped as a bare attribute. A span's existing
string value wins; an invalid value is replaced by the valid scoped id.

Every span also inherits its parent span's ``context_attributes=`` stamps. A
span that starts on another thread, or under a context saved before the
caller's block closed, carries the run's attributes through its parent. The
span's own live scope adds pairs, and it wins for a key both hold. These stamps
come from the parent span, never from a withdrawn scope's pairs, so they cannot
carry values that disagree with parentage.

The ``context_attributes=`` pairs stay in the process. ``inject()`` writes
nothing for them, unlike baggage, which a propagator writes into every
outbound request. The session id is the one value that can leave: ``inject()``
writes it only for a host in ``session_hosts`` or a carrier filled with
``carrier.inject(..., session=True)``, and ``extract`` reads it back (see
``_propagation``). The stamp is a span attribute under its own prefix, so it
overwrites nothing the caller or a library wrote. A key named in both
``attributes=`` and ``context_attributes=`` lands twice: bare and stamped.
OpenTelemetry evicts the oldest attribute when a span passes its attribute
limit. A span that loses a required stamp that way is withheld.

The require direction fails closed. A span it cannot place is withheld.
"""

from __future__ import annotations

import enum
import logging
import os
import threading
import time
import weakref
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from types import MappingProxyType
from typing import Any, NamedTuple

from opentelemetry import context, trace
from opentelemetry.attributes import BoundedAttributes
from opentelemetry.context import Context
from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor
from opentelemetry.trace import format_span_id

from . import _policy
from ._egress import DeclaredAgentFilter
from ._telemetry_keys import (
    CONVERGENT_ATTRIBUTES_PREFIX,
    CONVERGENT_INVOCATION_SPAN_ID,
    CONVERGENT_INVOCATION_STAMP_FAILED,
    CONVERGENT_INVOCATION_START_TIME_UNIX_MICROS,
    CONVERGENT_PARENT_INVOCATION_SPAN_ID,
    CONVERGENT_ROOT_INVOCATION_SPAN_ID,
    CONVERGENT_ROOT_INVOCATION_START_TIME_UNIX_MICROS,
    CONVERGENT_SESSION_ID,
    GEN_AI_AGENT_NAME,
    GEN_AI_OPERATION_NAME,
    GEN_AI_TOOL_NAME,
)

logger = logging.getLogger("convergent.sdk")

#: Minted by ``create_key``, which namespaces it. A caller's own context values
#: cannot collide with this one.
_CONTEXT_ATTRIBUTES = context.create_key("convergent-context-attributes")
#: A carrier entry ``extract`` accepted, as a :class:`_Received`.
_RECEIVED = context.create_key("convergent-received-trajectory")

#: The stamper stores each span's resolved pairs on the span object under this
#: name. Inheritance reads that field, never the span's public attributes. The
#: public bag is bounded, and it evicts its oldest entry first. It also mutates
#: under the caller's thread, so a cross-thread read can raise. The private
#: field is written at span start, and again by mark_span for pairs a caller
#: adds while the span runs.
_MARKS_FIELD = "_convergent_marks"

#: The trajectory position a child inherits, keyed by the registry's stamp keys
#: and copied to each span when it starts. ``inject()`` also writes it into the
#: ``convergent=`` tracestate entry, so a receiving process continues it.
_TRAJECTORY_FIELD = "_convergent_trajectory_stamp"
_TRAJECTORY_FAILED_FIELD = "_convergent_trajectory_stamp_failed"
#: The stamp, or the failed flag, the Convergent export copy writes back. It is
#: kept on the span's attribute bag, not the span: the exporter receives a
#: snapshot of the span, and the bag is the one object the snapshot shares.
_EXPORT_STAMP_FIELD = "_convergent_export_stamp"
#: Set beside it when the stamp was also written to the span itself, so a key
#: missing there at export was evicted and counted dropped.
_EXPORT_STAMP_WRITTEN_FIELD = "_convergent_export_stamp_written"
#: In ``_TRAJECTORY_FIELD`` under a parent whose place is unknown: a remote
#: parent that sent no stamp, or a local one the stamper never saw. The span and
#: its subtree carry no stamp keys, so ingest links them from parent ids.
_UNSTAMPED = object()
#: In ``_TRAJECTORY_FIELD`` on a span the stamper saw with no invocation above
#: it, so an invocation under it is a known root. A missing field means unknown.
_NO_INVOCATION: Mapping[str, Any] = MappingProxyType({})
#: The :class:`_Received` a span holds when its branch came from another process
#: and no local ancestor took a stamp, such as a SERVER span the agent filter
#: dropped. It follows parentage within the same trace, never ambient context.
_RECEIVED_FIELD = "_convergent_received"


#: On a span's attribute bag, as ``_EXPORT_STAMP_FIELD`` is: the :class:`_HeldStamp`
#: of a span whose stamp comes from a held entry. Its stamp and session reach the
#: span only once :class:`HeldEntryProcessor` sees the entry pass its check.
_HELD_FIELD = "_convergent_held_stamp"


class _Answer(enum.Enum):
    """What a held entry's check found."""

    #: No answer yet: the tag fetch is still out.
    PENDING = "pending"
    #: The entry's tag matched this process's own.
    ACCEPTED = "accepted"
    #: The tag named another workspace, the fetch ended with no tag, or the
    #: entry was given up (:meth:`_Held.settle`).
    REFUSED = "refused"


class _Held:
    """The workspace tag of an entry ``extract`` read before this process had its own.

    The first check made once this process knows its tag, or once its fetch
    ended without one, decides, and that answer stands for every span under
    the entry. Until then the entry is neither accepted nor refused: nothing
    it holds is written or sent. Only :meth:`settle` refuses an entry whose
    tag is still unknown, so every span under it goes out either stamped or
    unstamped, never a mix.
    """

    __slots__ = ("answer", "tag")

    def __init__(self, tag: str) -> None:
        self.tag = tag
        self.answer = _Answer.PENDING

    def check(self) -> _Answer:
        """The answer, asking for it first while it is still pending."""
        if self.answer is _Answer.PENDING:
            from . import _tag_fetch  # deferred: _tag_fetch imports _transport

            own, awaiting = _tag_fetch._tag_state()
            if not awaiting:
                found = _Answer.ACCEPTED if own == self.tag else _Answer.REFUSED
                with _held_lock:
                    # Set once, so a settle() that won the race is not overwritten.
                    if self.answer is _Answer.PENDING:
                        self.answer = found
        return self.answer

    def settle(self) -> None:
        """Refuse the entry if its check has no answer yet."""
        global _answers
        with _held_lock:
            if self.answer is _Answer.PENDING:
                self.answer = _Answer.REFUSED
                _answers += 1


_held_lock = threading.Lock()
#: Bumped whenever a waiting span's check may have gained an answer: the tag
#: fetch ended (:func:`release_answered`) or an entry settled. A holder rescans
#: its waiting spans only after this moved, not on every span end.
_answers = 0


class _Received(NamedTuple):
    """A carrier entry ``extract`` read and the remote parent span it named.

    A fresh entry has no stamp: work under it starts new trajectories.
    ``held`` is None for an entry accepted at ``extract``, and the pending check
    for one read before this process had a workspace tag.
    """

    trace_id: int
    span_id: int
    stamp: Mapping[str, Any] | None
    session_id: str | None
    held: _Held | None = None


class _HeldStamp(NamedTuple):
    """The stamp and received session a span keeps aside until ``entry`` passes."""

    entry: _Held
    stamp: Mapping[str, Any]
    session_id: str | None


def _checked(received: _Received | None) -> _Received | None:
    """``received`` when it may be used now: accepted at ``extract`` or since."""
    if received is None or received.held is None or received.held.check() is _Answer.ACCEPTED:
        return received
    return None


def _held_stamp(span: object) -> _HeldStamp | None:
    held = getattr(getattr(span, "_attributes", None), _HELD_FIELD, None)
    return held if isinstance(held, _HeldStamp) else None


#: Attached by ``span()`` and ``mark_span`` when context attributes were
#: requested but could not
#: resolved. :class:`FilterSpanProcessor` withholds every span carrying it.
#: A caller cannot write it: ``_accepted_context`` rejects ``convergent.`` keys.
UNRESOLVED = "convergent.unresolved"

#: Withheld spans remembered at once. A span that never ends never leaves the
#: table, so the table is capped the way the egress filter's is.
_UNRESOLVED_LIMIT = 4_096

#: Context tokens for marks made through a handle, stored on the span object
#: the way ``_MARKS_FIELD`` is, because the span owns their lifetime. ``span()``
#: drains them when the block exits, whichever handle made them.
_MARK_TOKENS_FIELD = "_convergent_mark_tokens"

#: The live filters, so ``mark_span`` can withhold a span whose pairs failed
#: after the span already started. Weak: a re-``init()`` drops the old filter.
_FILTERS: weakref.WeakSet[FilterSpanProcessor] = weakref.WeakSet()

#: Whether any scope was ever attached in this process. A process that sets no
#: context attributes or session id pays nothing at span start.
_ever_attached = False
#: Whether ``extract`` ever accepted a carrier entry in this process. Until then
#: no span can hold a received stamp, so none pays to look for one.
_ever_received = False
#: Set by ``init()`` once it installs its propagator: run as each agent invocation
#: starts, it warns once if that propagator was since replaced.
_propagator_check: Callable[[], None] | None = None


class _Mark:
    """One attached context scope, and whether it is still open.

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
    and ``session()`` validate the pairs before calling this.
    """
    global _ever_attached
    _ever_attached = True
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


def mark_span(span: trace.Span, pairs: Mapping[str, Any]) -> None:
    """Stamp ``pairs`` onto a running span and scope them to the rest of it.

    The span's own stamp, its private marks field, and the ambient context all
    get the pairs, so spans started after this call inherit them. The context
    token is kept on the span, and :func:`release_marks` drains it when the
    span's block exits, whichever handle made the mark. Never raises.
    """
    try:
        if UNRESOLVED in pairs:
            span_id = span.get_span_context().span_id
            for filter_ in tuple(_FILTERS):
                filter_.withhold(span_id)
        # ``set_attribute`` rejects caller keys starting with ``convergent.``, so only
        # this stamper writes under the prefix. The session id is the one bare key.
        for key, value in pairs.items():
            if key != UNRESOLVED:
                span.set_attribute(CONVERGENT_ATTRIBUTES_PREFIX + key, value)
        merged = {**(getattr(span, _MARKS_FIELD, None) or {}), **pairs}
        setattr(span, _MARKS_FIELD, merged)
        tokens = getattr(span, _MARK_TOKENS_FIELD, None)
        if tokens is None:
            tokens = []
            setattr(span, _MARK_TOKENS_FIELD, tokens)
        tokens.append(attach_context(pairs))
    except Exception:  # noqa: BLE001 - a raise here would break the caller's request
        from . import _core  # deferred: _core imports this module

        _core._warn_once(
            "mark_span_failed",
            "Convergent could not scope context attributes to the running span.",
        )


def release_marks(span: trace.Span) -> None:
    """Detach every mark made on ``span`` through a handle. Never raises."""
    tokens = getattr(span, _MARK_TOKENS_FIELD, None) or []
    for token in reversed(tokens):
        detach_context(token)
    tokens.clear()


def _parent_marks(parent_context: Context | None) -> Mapping[str, Any]:
    """The parent span's stamped pairs, bare-keyed.

    A library can start a span under a context that never held the carrier. It
    can also replay a context the caller's block already closed. The parent
    span's stamps are what the run held, so the child inherits them.

    The stamps come from the parent's private field, never from its public
    attributes. The public bag evicts its oldest entry past the span attribute
    limit. Iterating it also races the caller's writes. A remote or absent
    parent is not a local SDK span, and answers nothing.
    """
    parent = trace.get_current_span(parent_context)
    if not isinstance(parent, Span):
        # A held entry's session waits for its check with the stamp; see _HeldStamp.
        received = _checked(_remote_received(parent_context))
        session_id = received.session_id if received is not None else None
        return {CONVERGENT_SESSION_ID: session_id} if session_id is not None else {}
    marks = getattr(parent, _MARKS_FIELD, None)
    return dict(marks) if marks else {}


#: The (bag, key) pairs ingest's canonical path (``normalize_span``, through
#: ``PRIORITY``) reads an operation, a tool name and an agent name from, in its
#: order. Ingest reads only the first key present, and drops a blank or
#: non-string value without trying the next. With no operation, a span naming a
#: tool is a tool call, and otherwise a span naming an agent is an agent run. A
#: monorepo test runs each case through ``normalize_span`` and compares.
_OPERATION_KEYS = (("span", "convergent.operation"), ("span", GEN_AI_OPERATION_NAME))
_TOOL_NAME_KEYS = (("span", "convergent.tool_name"), ("span", GEN_AI_TOOL_NAME))
_AGENT_NAME_KEYS = (
    ("span", "convergent.agent_name"),
    ("resource", "convergent.agent.name"),
    ("span", GEN_AI_AGENT_NAME),
    ("span", "agent_name"),
)
#: The instrumentation scopes whose ``agent <name>`` spans ingest's pydantic-ai
#: classifier files as agent runs before reading names. A monorepo test pins
#: them to that source's ``scopes``.
_PYDANTIC_AI_SCOPES = ("pydantic_ai", "pydantic-ai")


def with_received(
    remote: Context,
    stamp: Mapping[str, Any] | None,
    session_id: str | None,
    held: _Held | None = None,
) -> Context:
    """``remote`` holding an entry for its current span, the remote parent.

    A ``stamp`` of None makes the entry fresh: agents under it start new
    trajectories, keeping ``session_id``. ``held`` is the pending check of an
    entry read before this process had its workspace tag.
    """
    global _ever_received
    _ever_received = True
    parent = trace.get_current_span(remote).get_span_context()
    received = _Received(parent.trace_id, parent.span_id, stamp, session_id, held)
    return context.set_value(_RECEIVED, received, remote)


def _remote_received(parent_context: Context | None) -> _Received | None:
    """The accepted entry, when the remote parent is the span it named."""
    received = context.get_value(_RECEIVED, parent_context)
    if not isinstance(received, _Received):
        return None
    parent = trace.get_current_span(parent_context).get_span_context()
    if (parent.trace_id, parent.span_id) != (received.trace_id, received.span_id):
        return None
    return received


def _received_from(span: object, parent_context: Context | None) -> _Received | None:
    """The entry ``span`` passes on: a local span's own record, or, for the remote
    parent itself, the entry ``extract`` accepted for it."""
    if isinstance(span, Span):
        received = getattr(span, _RECEIVED_FIELD, None)
    else:
        received = _remote_received(parent_context)
    return received if isinstance(received, _Received) else None


def _carry_received(span: Span, parent_context: Context | None) -> None:
    """Hand a received entry down to ``span`` while no local ancestor holds a stamp."""
    parent = trace.get_current_span(parent_context)
    # An empty state (no agent above) still lets the entry through.
    if isinstance(parent, Span) and (
        getattr(parent, _TRAJECTORY_FIELD, None) or getattr(parent, _TRAJECTORY_FAILED_FIELD, False)
    ):
        return
    received = _received_from(parent, parent_context)
    if received is not None and received.trace_id == span.get_span_context().trace_id:
        setattr(span, _RECEIVED_FIELD, received)


def _is_invocation(span: Span) -> bool:
    """Whether ingest's linker would anchor a trajectory on this span.

    Only what is set when the span starts counts: its children are stamped
    before any later attribute exists.
    """
    from . import _semantic  # deferred: _semantic imports this module

    attributes = span.attributes or {}
    bags = {"span": attributes, "resource": getattr(span.resource, "attributes", None) or {}}
    # Other instrumentors' markings (OpenInference, OpenLLMetry) are not read:
    # ingest's canonical path does not read them either.
    operation = _first_value(bags, _OPERATION_KEYS)
    if operation is not None:
        return operation == _semantic._semconv_operation("agent_run")
    # pydantic-ai's own agent span: INTERNAL, named ``agent <agent.name>``.
    # Ingest runs this scope rule before it reads a tool or agent name.
    agent_name = attributes.get("agent.name")
    scope = span.instrumentation_scope
    if (
        isinstance(agent_name, str)
        and bool(agent_name)
        and span.name == f"agent {agent_name}"
        and span.kind is trace.SpanKind.INTERNAL
        and scope is not None
        and any(scope.name == s or scope.name.startswith(f"{s}.") for s in _PYDANTIC_AI_SCOPES)
    ):
        return True
    if _first_value(bags, _TOOL_NAME_KEYS) is not None:
        return False
    return _first_value(bags, _AGENT_NAME_KEYS) is not None


def _first_value(
    bags: Mapping[str, Mapping[str, Any]], keys: Sequence[tuple[str, str]]
) -> str | None:
    """The first of ``keys`` present, if ingest keeps it: a non-blank string.

    Ingest never tries a later key once one is present, so a blank or non-string
    value there means none.
    """
    for bag, key in keys:
        if key in bags[bag]:
            value = bags[bag][key]
            return value if isinstance(value, str) and value.strip() else None
    return None


def _warn_trajectory_stamp_failed() -> None:
    from . import _core  # deferred: _core imports this module

    _core._warn_once(
        "trajectory_stamp_failed",
        "Convergent could not stamp trajectory identity on a span. "
        "Tracing is degraded; your application is unaffected.",
    )


def _fits(span: Span, values: Mapping[str, Any]) -> bool:
    """Whether writing ``values`` to ``span`` evicts none of its attributes.

    BoundedAttributes silently evicts its oldest value past the limit. A key
    the span already holds is overwritten in place, so it takes no new room.
    """
    attributes = getattr(span, "_attributes", None)
    limit = getattr(attributes, "maxlen", None)
    if attributes is None or limit is None:
        return True
    return len(attributes) + len(values) <= limit or len(attributes.keys() | values.keys()) <= limit


def _mark_stamp_failed(span: Span) -> None:
    """Keep descendants unstamped, and flag the span when the flag fits.

    The flag goes only into free room, so it never evicts an attribute the
    caller or a context mark wrote.
    """
    try:
        setattr(span, _TRAJECTORY_FAILED_FIELD, True)
        flag = {CONVERGENT_INVOCATION_STAMP_FAILED: True}
        written = _fits(span, flag)
        _keep_for_export(span, flag, written=written)
        if written:
            span.set_attribute(CONVERGENT_INVOCATION_STAMP_FAILED, True)
    except Exception:  # noqa: BLE001 - malformed custom span
        pass


def _give_up_trajectory_stamp(span: Span) -> None:
    _mark_stamp_failed(span)
    try:
        _warn_trajectory_stamp_failed()
    except Exception:  # noqa: BLE001 - warnings must never break application work
        pass


def _stamp_trajectory(span: Span, parent_context: Context | None, *, dropped: bool = False) -> None:
    """Stamp this span from its local parent, or from the entry a remote parent sent.

    A span the agent filter drops is never an anchor and gets no attributes:
    it only passes its parent's stamp on, so a declared agent under an
    undeclared one still names the declared agent above as its parent.
    """
    parent = trace.get_current_span(parent_context)
    # Set by the context stamper, which runs ahead of the agent filter, when this
    # branch came from another process with an accepted entry and no local
    # ancestor took a stamp yet.
    received = getattr(span, _RECEIVED_FIELD, None)
    if not isinstance(received, _Received) or (
        received.held is not None and received.held.check() is _Answer.REFUSED
    ):
        # A held entry that failed its check counts as never received, as one
        # ``extract`` refused does.
        received = None
    received_stamp = received.stamp if received is not None else None
    # The held entry this span's stamp comes from. Its spans stay held even once
    # the entry passed, so every span under it is written in at its end the same
    # way, with the received session its parents' marks could not carry.
    held: _HeldStamp | None = None
    if not isinstance(parent, Span):
        if received is not None:
            inherited = received_stamp
            # A fresh entry has no stamp: no agent above, so an agent here is
            # its own root.
            if received.held is not None:
                held = _HeldStamp(received.held, received.stamp or {}, received.session_id)
        elif parent.get_span_context().is_valid:
            # A parent in another process, with no stamp received from it: where
            # this span sits in the caller's trajectory is unknown, so the whole
            # subtree is left for ingest to link.
            setattr(span, _TRAJECTORY_FIELD, _UNSTAMPED)
            return
        else:
            inherited = None
    elif getattr(parent, _TRAJECTORY_FAILED_FIELD, False):
        if dropped:
            setattr(span, _TRAJECTORY_FAILED_FIELD, True)
        else:
            _mark_stamp_failed(span)
        return
    else:
        inherited = getattr(parent, _TRAJECTORY_FIELD, None)
        if inherited is None and received is not None:
            # A local parent no stamper reached, such as a dropped SERVER span, on
            # a branch an accepted entry vouches for.
            inherited = received_stamp or _NO_INVOCATION
            if received.held is not None:
                held = _HeldStamp(received.held, received.stamp or {}, received.session_id)
        else:
            held = _held_stamp(parent)
            if held is not None and held.entry.check() is _Answer.REFUSED:
                # The parent's entry failed its check, so the parent goes out unstamped.
                inherited = _UNSTAMPED
        if not isinstance(inherited, Mapping):
            # Unknown ancestry, as under a remote parent with no stamp: either
            # _UNSTAMPED, or a parent that started before the SDK attached to
            # this tracer provider and so has no state at all.
            setattr(span, _TRAJECTORY_FIELD, _UNSTAMPED)
            return
        inherited = inherited or None
    if dropped:
        setattr(span, _TRAJECTORY_FIELD, inherited or _NO_INVOCATION)
        if held is not None and not _hold(span, held):
            setattr(span, _TRAJECTORY_FIELD, _UNSTAMPED)
        return
    invocation = _is_invocation(span)
    if invocation and _propagator_check is not None:
        try:
            _propagator_check()
        except Exception:  # noqa: BLE001 - a warning must never cost the stamp
            logger.debug("Convergent propagator check failed", exc_info=True)
    if inherited is None and not invocation:
        # Under a held fresh entry this span still waits for the check, so it
        # gets the caller's session at its end, and its children stay held.
        setattr(span, _TRAJECTORY_FIELD, _NO_INVOCATION)
        if held is not None and not _hold(span, held._replace(stamp=_NO_INVOCATION)):
            setattr(span, _TRAJECTORY_FIELD, _UNSTAMPED)
        return

    span_context = span.get_span_context()
    if span_context is None:
        return
    own_id = format_span_id(span_context.span_id)
    # SDK spans normally have a start time by on_start, but a custom/malformed
    # span must not make trajectory stamping escape into application code.
    if span.start_time is None:
        _give_up_trajectory_stamp(span)
        return
    own_start = span.start_time // 1_000
    if invocation:
        values: dict[str, Any] = {
            CONVERGENT_INVOCATION_SPAN_ID: own_id,
            CONVERGENT_INVOCATION_START_TIME_UNIX_MICROS: own_start,
        }
        if inherited is None:
            values[CONVERGENT_ROOT_INVOCATION_SPAN_ID] = own_id
            values[CONVERGENT_ROOT_INVOCATION_START_TIME_UNIX_MICROS] = own_start
        else:
            values[CONVERGENT_ROOT_INVOCATION_SPAN_ID] = inherited[
                CONVERGENT_ROOT_INVOCATION_SPAN_ID
            ]
            values[CONVERGENT_ROOT_INVOCATION_START_TIME_UNIX_MICROS] = inherited[
                CONVERGENT_ROOT_INVOCATION_START_TIME_UNIX_MICROS
            ]
            values[CONVERGENT_PARENT_INVOCATION_SPAN_ID] = inherited[CONVERGENT_INVOCATION_SPAN_ID]
    else:
        values = dict(inherited or {})
        # Only an invocation records its parent invocation. Ordinary descendants
        # inherit the current and root anchors, but do not repeat that relation.
        values.pop(CONVERGENT_PARENT_INVOCATION_SPAN_ID, None)

    if held is not None:
        # Children inherit the stamp, but it reaches this span's attributes only
        # once the entry passes its check (HeldEntryProcessor).
        setattr(
            span,
            _TRAJECTORY_FIELD,
            values if _hold(span, held._replace(stamp=values)) else _UNSTAMPED,
        )
        return
    # A stamp that does not fit whole is not written to the shared span: it
    # would drop a caller's attribute, a context mark, or one of its own keys.
    # The Convergent export copy has no limit and writes the stamp back, and
    # the span's children inherit it from the private field.
    written = _fits(span, values)
    if written:
        span.set_attributes(values)
    setattr(span, _TRAJECTORY_FIELD, values)
    _keep_for_export(span, values, written=written)


def _hold(span: Span, held: _HeldStamp) -> bool:
    """Keep ``held`` on the span's attribute bag for its end; False when there is none."""
    attributes = getattr(span, "_attributes", None)
    if attributes is None:
        return False
    setattr(attributes, _HELD_FIELD, held)
    return True


def _keep_for_export(span: Span, values: Mapping[str, Any], *, written: bool = False) -> None:
    attributes = getattr(span, "_attributes", None)
    if attributes is not None:
        setattr(attributes, _EXPORT_STAMP_FIELD, values)
        setattr(attributes, _EXPORT_STAMP_WRITTEN_FIELD, written)


def evicted_stamp_keys(span: ReadableSpan) -> int:
    """How many of the stamp's keys OpenTelemetry evicted from ``span`` after they
    were written, and so counts in its ``dropped_attributes``."""
    attributes = getattr(span, "_attributes", None)
    if not getattr(attributes, _EXPORT_STAMP_WRITTEN_FIELD, False):
        return 0
    present = span.attributes or {}
    return sum(1 for key in trajectory_stamp(span) if key not in present)


def trajectory_stamp(span: ReadableSpan) -> Mapping[str, Any]:
    """The stamp ``span`` got at start, or its failed flag, as the export copy writes it.

    The span's public attributes can lose either one: OpenTelemetry evicts the
    oldest keys once a span passes its attribute limit after it started, and
    either one is written there only when it fits.
    """
    values = getattr(getattr(span, "_attributes", None), _EXPORT_STAMP_FIELD, None)
    return values if isinstance(values, Mapping) else {}


def _pass_trajectory_through(span: Span, parent_context: Context | None) -> None:
    try:
        _stamp_trajectory(span, parent_context, dropped=True)
    except Exception:  # noqa: BLE001 - telemetry must never break application work
        setattr(span, _TRAJECTORY_FAILED_FIELD, True)


class TrajectoryStampProcessor(SpanProcessor):
    """Copies the current and root invocation onto each span at start.

    The SDK version is not written here: the Convergent exporter adds it to
    its own copy of each span. ``wrap()`` places this processor behind
    the agent filter, so a dropped invocation never becomes the anchor of an
    exported child; the filter passes a dropped span's inherited stamp on
    instead. It runs after the context stamper, so its capacity check also
    covers the context marks.
    """

    def on_start(self, span: Span, parent_context: Context | None = None) -> None:
        try:
            _stamp_trajectory(span, parent_context)
        except Exception:  # noqa: BLE001 - telemetry must never break application work
            _give_up_trajectory_stamp(span)

    def on_end(self, span: ReadableSpan) -> None:
        pass

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return True

    def shutdown(self) -> None:
        pass


#: Stateless like ``_STAMPER``, so every ``wrap()`` chain shares this instance.
_TRAJECTORY_STAMPER = TrajectoryStampProcessor()


def with_attributes(span: ReadableSpan, attributes: BoundedAttributes) -> ReadableSpan:
    """A copy of the ended ``span`` with ``attributes`` in place of its own."""
    return ReadableSpan(
        name=span.name,
        context=span.context,
        parent=span.parent,
        resource=span.resource,
        attributes=attributes,
        # The bounded lists, when there are any, carry the event and link drop counts.
        events=getattr(span, "_events", span.events),
        links=getattr(span, "_links", span.links),
        kind=span.kind,
        status=span.status,
        start_time=span.start_time,
        end_time=span.end_time,
        instrumentation_scope=span.instrumentation_scope,
    )


def _with_held_stamp(span: ReadableSpan) -> ReadableSpan:
    """``span`` with its held stamp and session written in, once the entry passed its check.

    A span whose entry failed goes as it is: unstamped, as under an entry
    ``extract`` refused. The copy keeps the span's attribute limits, and the
    stamp goes into it only when it fits, as at span start.
    """
    held = _held_stamp(span)
    if held is None or held.entry.check() is not _Answer.ACCEPTED:
        return span
    own = dict(span.attributes or {})
    bag = getattr(span, "_attributes", None)
    limit = getattr(bag, "maxlen", None)
    if (
        held.session_id is not None
        and CONVERGENT_SESSION_ID not in own
        and (limit is None or len(own) < limit)
    ):
        # Only into free room: a full span keeps the caller's attributes and
        # goes without the session.
        own[CONVERGENT_SESSION_ID] = held.session_id
    written = limit is None or len(own.keys() | held.stamp.keys()) <= limit
    attributes = BoundedAttributes(
        maxlen=limit,
        attributes={**own, **held.stamp} if written else own,
        max_value_len=getattr(bag, "max_value_len", None),
    )
    attributes.dropped = span.dropped_attributes
    setattr(attributes, _EXPORT_STAMP_FIELD, held.stamp)
    setattr(attributes, _EXPORT_STAMP_WRITTEN_FIELD, written)
    return with_attributes(span, attributes)


#: Ended spans waiting for their entry's check at once. Past this, the oldest
#: waiting span's entry is refused and its spans go out unstamped.
_WAITING_LIMIT = 2_048


class HeldEntryProcessor(SpanProcessor):
    """Forwards every span to ``destinations``, writing in a held entry's stamp at its end.

    An entry ``extract`` read before this process had its workspace tag is held,
    not refused, and the spans under it start with their stamps kept aside. A
    span that ends while the tag is still unknown waits here, up to
    ``_WAITING_LIMIT`` of them, and goes on as soon as the tag fetch answers,
    so a short-lived worker that extracts straight after ``init()`` still links
    to its caller. Nothing waits for the
    tag at span start or end; ``force_flush`` and ``shutdown`` wait for a check
    still out, within their timeout, then refuse any entry still unknown.
    """

    def __init__(self, destinations: Sequence[SpanProcessor]) -> None:
        self._destinations = tuple(destinations)
        self._waiting: deque[ReadableSpan] = deque()
        self._lock = threading.Lock()
        #: The ``_answers`` this holder last scanned its waiting spans at.
        self._scanned = -1
        _HOLDERS.add(self)

    def _reset_after_fork(self) -> None:
        # The parent still holds and sends these spans; the lock may arrive held.
        self._waiting = deque()
        self._lock = threading.Lock()
        self._scanned = -1

    def on_start(self, span: Span, parent_context: Context | None = None) -> None:
        for destination in self._destinations:
            destination.on_start(span, parent_context)

    def on_end(self, span: ReadableSpan) -> None:
        if not _ever_received:
            self._forward(span)
            return
        # Guarded like FilterSpanProcessor.on_end: a raise here would leave
        # span.end() and reach the caller. The span then goes on unstamped.
        outgoing: ReadableSpan | None = span
        try:
            # Read before the check: an answer that lands between the check and
            # the wait has moved it, so the span is not left waiting unscanned.
            seen = _answers
            if _decided(span):
                outgoing = _with_held_stamp(span)
            else:
                self._wait(span, seen)
                outgoing = None
        except Exception:  # noqa: BLE001 - telemetry must never break application work
            _warn_trajectory_stamp_failed()
        try:
            # Spans that waited ended first, so they go out first.
            self._release()
        except Exception:  # noqa: BLE001 - telemetry must never break application work
            _warn_trajectory_stamp_failed()
        if outgoing is not None:
            self._forward(outgoing)

    def _wait(self, span: ReadableSpan, seen: int) -> None:
        with self._lock:
            self._waiting.append(span)
            if _answers != seen:
                self._scanned = -1
            oldest = self._waiting[0] if len(self._waiting) > _WAITING_LIMIT else None
        if oldest is not None and (held := _held_stamp(oldest)) is not None:
            held.entry.settle()
            from . import _core  # deferred: _core imports this module

            _core._warn_once(
                "held_spans_full",
                f"Convergent held {_WAITING_LIMIT} spans waiting for its workspace tag and "
                "sent the oldest request's spans without their trajectory stamp.",
            )

    def _release(self) -> None:
        """Forward, in the order they ended, the waiting spans whose check has answered."""
        if not self._waiting:
            return
        ready: list[ReadableSpan] = []
        with self._lock:
            if self._scanned == _answers:
                # No check has gained an answer since the last scan.
                return
            self._scanned = _answers
            still: deque[ReadableSpan] = deque()
            for span in self._waiting:
                (ready if _decided(span) else still).append(span)
            self._waiting = still
        for span in ready:
            self._forward(_with_held_stamp(span))

    def _forward(self, span: ReadableSpan) -> None:
        for destination in self._destinations:
            destination.on_end(span)

    def _settle_waiting(self, timeout_s: float) -> None:
        """Wait up to ``timeout_s`` for a check still out, then refuse every entry
        still unknown and send what waited."""
        try:
            if not self._waiting:
                return
            from . import _tag_fetch  # deferred: _tag_fetch imports _transport

            if (thread := _tag_fetch._state.thread) is not None:
                thread.join(timeout=max(timeout_s, 0.0))
            with self._lock:
                waiting = list(self._waiting)
            for span in waiting:
                held = _held_stamp(span)
                if held is not None and held.entry.check() is _Answer.PENDING:
                    held.entry.settle()
            with self._lock:
                # The last chance to send them: rescan even if the count did not
                # move, as when setup was dropped with its fetch still out.
                self._scanned = -1
            self._release()
        except Exception:  # noqa: BLE001 - telemetry must never break application work
            _warn_trajectory_stamp_failed()

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        # One budget for the wait and the destinations, as flush() keeps one.
        deadline = time.monotonic() + max(timeout_millis, 0) / 1_000
        self._settle_waiting(timeout_millis / 1_000)
        remaining_ms = max(0, int((deadline - time.monotonic()) * 1_000))
        # Materialized first: all() would stop flushing after the first False.
        flushed = [destination.force_flush(remaining_ms) for destination in self._destinations]
        return all(flushed)

    def shutdown(self) -> None:
        self._settle_waiting(HELD_RELEASE_BUDGET_S)
        for destination in self._destinations:
            destination.shutdown()


#: How long ``flush()``-less exit waits for a tag fetch still out before held
#: spans go out unstamped: half of the 10 s exit deadline, so the exporters keep
#: their 5 s (Theo, D-110 A, 2026-11-03). A failing fetch can take about 21 s
#: (three 5 s requests and up to 6 s of backoff), so a tag that arrives after
#: the first 5 s finds the spans already sent unstamped.
HELD_RELEASE_BUDGET_S = 5.0


def release_held(timeout_s: float) -> None:
    """Settle and send the spans every holder keeps, waiting up to ``timeout_s`` in
    total for a tag fetch still out.

    ``flush()`` and the exit drain reach only the exporters, so they call this
    first: otherwise held spans would reach an exporter already shut down.
    """
    deadline = time.monotonic() + timeout_s
    for holder in list(_HOLDERS):
        holder._settle_waiting(deadline - time.monotonic())


def release_answered() -> None:
    """Send every waiting span whose entry's check now has an answer, waiting for nothing.

    The tag fetch calls this when it ends, so spans that ended before the tag
    go out without waiting for a later span end or a flush.
    """
    global _answers
    with _held_lock:
        _answers += 1
    for holder in list(_HOLDERS):
        holder._release()


def held_spans() -> int:
    """Ended spans still waiting for their entry's check, for ``flush()``'s ``pending``."""
    return sum(len(holder._waiting) for holder in list(_HOLDERS))


#: The live processors, so a forked child can reset each one's queue and lock.
_HOLDERS: weakref.WeakSet[HeldEntryProcessor] = weakref.WeakSet()


def _reset_after_fork() -> None:
    """Give a forked child its own locks and no spans the parent is holding, as
    ``_tag_fetch`` and ``_transport`` reset theirs."""
    global _held_lock
    _held_lock = threading.Lock()
    for holder in list(_HOLDERS):
        holder._reset_after_fork()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_after_fork)


def _decided(span: ReadableSpan) -> bool:
    """Whether ``span`` may go on: it holds no stamp, or its entry's check has answered."""
    held = _held_stamp(span)
    return held is None or held.entry.check() is not _Answer.PENDING


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
    # Outside the policy filter, so it judges a held stamp's session like any other.
    wrapped = (HeldEntryProcessor(wrapped),)
    if agents is not None:
        # Only the trajectory stamp sits behind the agent filter, so a dropped
        # invocation never anchors an exported child; a dropped span passes its
        # parent's stamp through instead. Context marks stay on every span,
        # because a declared agent can inherit them from a dropped one.
        wrapped = (
            DeclaredAgentFilter(
                agents, (_TRAJECTORY_STAMPER, *wrapped), on_drop=_pass_trajectory_through
            ),
        )
    else:
        wrapped = (_TRAJECTORY_STAMPER, *wrapped)
    return wrapped


class ContextAttributesSpanProcessor(SpanProcessor):
    """Copies every context pair onto each span at start.

    ``span()`` and ``session()`` validate every pair before attaching it, so
    the exact-type check below is a cheap last guard in front of
    ``set_attribute`` rather than a validation layer.
    """

    def on_start(self, span: Span, parent_context: Context | None = None) -> None:
        if not _ever_attached and not _ever_received:
            return
        # Guarded the way FilterSpanProcessor.on_end is: the tracer provider
        # calls its processors in a bare loop, so an exception here would
        # reach the caller's application code. Warn once and stamp nothing.
        try:
            if _ever_received:
                _carry_received(span, parent_context)
            pairs = {**_parent_marks(parent_context), **context_pairs(parent_context)}
            stamped: dict[str, Any] = {}
            for key, value in pairs.items():
                if key == UNRESOLVED:
                    # Never a span attribute: the bounded bag evicts oldest-first
                    # past the attribute limit, and this mark losing that race
                    # would send the span it exists to withhold.
                    stamped[key] = True
                elif key == CONVERGENT_SESSION_ID:
                    existing = (
                        span.attributes.get(CONVERGENT_SESSION_ID) if span.attributes else None
                    )
                    if isinstance(existing, str):
                        stamped[key] = existing
                    elif isinstance(value, str):
                        span.set_attribute(CONVERGENT_SESSION_ID, value)
                        stamped[key] = value
                elif _policy._is_attribute_value(value):
                    span.set_attribute(CONVERGENT_ATTRIBUTES_PREFIX + key, value)
                    stamped[key] = value
            if stamped:
                # Written before start_span returns, so no other thread holds
                # the span yet. See _MARKS_FIELD.
                setattr(span, _MARKS_FIELD, stamped)
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

    The decision reads the finished span at ``on_end``. The one thing
    remembered between calls is the withhold table, span ids whose context
    attributes could not be resolved. If ``agents=`` is also set, its filter
    runs first and this one sees only the spans it kept.
    """

    def __init__(self, policy: _policy.Policy, destinations: Sequence[SpanProcessor]) -> None:
        self._policy = policy
        self._destinations = tuple(destinations)
        #: Span ids to withhold, written at start and read at end. No lock:
        #: one span starts once and ends once.
        self._unresolved: set[int] = set()
        _FILTERS.add(self)

    def withhold(self, span_id: int) -> None:
        """Withhold this span at its end, whatever its attributes say."""
        if len(self._unresolved) >= _UNRESOLVED_LIMIT:
            from . import _core  # deferred: _core imports this module

            _core._warn_once(
                "unresolved_table_full",
                f"Convergent is tracking {_UNRESOLVED_LIMIT} withheld spans that "
                "never ended and is dropping the oldest records. A span whose "
                "record is dropped is judged by its attributes when it ends.",
            )
            self._unresolved.pop()
        self._unresolved.add(span_id)

    def on_start(self, span: Span, parent_context: Context | None = None) -> None:
        # on_end receives a snapshot of the span, so the marks field is only
        # readable here, and the span id is the one key both calls share.
        span_context = span.get_span_context()
        if span_context is not None and UNRESOLVED in (getattr(span, _MARKS_FIELD, None) or {}):
            self.withhold(span_context.span_id)
        for destination in self._destinations:
            destination.on_start(span, parent_context)

    def on_end(self, span: ReadableSpan) -> None:
        # Guarded, because nothing above catches: the tracer provider calls its
        # processors in a bare loop, so an exception here would leave
        # ``span.end()`` and reach the caller's application code.
        # Fail closed. Being unable to decide is not a reason to send.
        try:
            attributes = span.attributes or {}
            span_context = span.get_span_context()
            if span_context is not None and span_context.span_id in self._unresolved:
                # Fail closed. An unresolvable tag is not a reason to send.
                self._unresolved.discard(span_context.span_id)
                return
            marks = {
                key[len(CONVERGENT_ATTRIBUTES_PREFIX) :]: value
                for key, value in attributes.items()
                if key.startswith(CONVERGENT_ATTRIBUTES_PREFIX)
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
    "UNRESOLVED",
    "ContextAttributesSpanProcessor",
    "FilterSpanProcessor",
    "attach_context",
    "mark_span",
    "context_pairs",
    "detach_context",
    "wrap",
]
