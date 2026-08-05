"""Keeps spans from agents the caller did not declare inside their process.

``init(agents=[...])`` names the agents Convergent is allowed to see. When
``init()`` attaches to a tracer provider the caller already owns, every span that
provider produces reaches our exporters. That includes their web server, their
database, and their queues. This filter sits in front of our exporters only, so
the caller's own exporters still receive everything they received before.

A span is kept when it names a declared agent, or when it descends from a span
that was kept. Descent needs nothing on the wire, because the parent's span id is
enough inside one process. A parent that arrived from another process is the one
case where descent cannot be checked, so a span under a remote parent is kept
when it looks like model work instead. A mark cannot be carried across a process
boundary reliably, because OpenTelemetry baggage truncates without saying so and
is dropped whole when the caller's own baggage is oversized.

A span with no parent at all is kept only by naming a declared agent. It starts a
trace inside this process, so it would name its agent if it had one. Judging it on
attributes instead would send any root span carrying a ``gen_ai.*`` attribute,
which is the opposite of what naming your agents asked for.
"""

from __future__ import annotations

import os
import threading
import weakref
from collections.abc import Iterable, Sequence

from opentelemetry.context import Context
from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor

#: Unfinished spans remembered at once. A span that never ends never leaves the table.
_KEPT_LIMIT = 4_096

#: Scopes whose spans are model work even with no ``gen_ai.*`` attribute of their
#: own.
_LLM_SCOPES = ("convergent.sdk", "pydantic-ai", "openinference", "litellm")

#: Every live filter, so a fork can replace the locks its child inherited. Weak so
#: a filter a test threw away does not stay alive here.
_instances: weakref.WeakSet[DeclaredAgentFilter] = weakref.WeakSet()


class DeclaredAgentFilter(SpanProcessor):
    """Forwards to ``destinations`` only the spans a declared agent produced.

    One instance holds one table of kept span ids. There is one tracer provider
    per process, so every span in the process passes through this one object and
    the parent lookup always finds a parent that was kept.
    """

    def __init__(self, agents: Iterable[str], destinations: Sequence[SpanProcessor]) -> None:
        self._agents = frozenset(agents)
        self._destinations = tuple(destinations)
        #: Kept span ids, oldest first. A dict rather than a set so the oldest
        #: entry can be evicted when the table is full.
        self._kept: dict[int, None] = {}
        self._lock = threading.Lock()
        _instances.add(self)

    def on_start(self, span: Span, parent_context: Context | None = None) -> None:
        if not self._keep(span):
            return
        self._remember(span.get_span_context().span_id)
        for destination in self._destinations:
            destination.on_start(span, parent_context)

    def on_end(self, span: ReadableSpan) -> None:
        # on_end gets a different object than on_start; the span id is the only
        # key shared between them.
        span_context = span.get_span_context()
        if span_context is None:
            return
        with self._lock:
            kept = span_context.span_id in self._kept
            self._kept.pop(span_context.span_id, None)
        if not kept:
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

    def _keep(self, span: Span) -> bool:
        """Whether this span belongs to a declared agent's work.

        Fail closed. A span this cannot place is dropped, because sending a
        caller's unrelated spans to us is worse than losing some of an agent's own.
        An empty declaration matches no span at all, including one whose parent
        arrived from another process, so ``agents=[]`` sends nothing.
        """
        if not self._agents:
            return False
        agent = (span.attributes or {}).get("gen_ai.agent.name")
        if isinstance(agent, str) and agent:
            return agent in self._agents
        # span.parent, not parent_context: parent_context is None in the normal case.
        parent = span.parent
        if parent is None:
            return False
        if not parent.is_remote:
            with self._lock:
                return parent.span_id in self._kept
        return _is_model_work(span)

    def _remember(self, span_id: int) -> None:
        with self._lock:
            self._kept[span_id] = None
            if len(self._kept) <= _KEPT_LIMIT:
                return
            # Evict the oldest rather than refuse the newest, so a spike loses the
            # spans least likely to still be open instead of every new one.
            del self._kept[next(iter(self._kept))]
        from . import _core  # deferred: _core imports this module

        _core._warn_once(
            "egress_table_full",
            f"Convergent is tracking {_KEPT_LIMIT} unfinished spans and is dropping "
            "the oldest to make room, so some spans from your declared agents will "
            "not be sent. This usually means spans are being started and never ended.",
        )


def _is_model_work(span: Span) -> bool:
    if any(key.startswith("gen_ai.") for key in span.attributes or {}):
        return True
    scope = span.instrumentation_scope
    if scope is None:
        return False
    # Exact name or a dotted child of one, so a caller's own "litellm_proxy" tracer
    # is not read as litellm.
    return any(scope.name == name or scope.name.startswith(f"{name}.") for name in _LLM_SCOPES)


def _reset_locks_after_fork() -> None:
    """Replace every filter's lock in a forked child.

    fork() copies memory but only the calling thread, so a lock another thread
    held at fork time arrives locked in the child with nobody left to release it.
    Every ``on_start`` in the child would block forever. ``_core`` does the same
    for its own lock, and this one is held on the hotter path.
    """
    for instance in _instances:
        instance._lock = threading.Lock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_locks_after_fork)


__all__ = ["DeclaredAgentFilter"]
