"""W3C transport for the SDK's existing trajectory and session context.

Every carrier injected inside agent work, or inside work that starts new
trajectories such as a Temporal workflow run, gets one ``convergent=``
tracestate member, whatever host it goes to, model providers included. A
carrier gets none when the process has no workspace tag; when the SDK did not
stamp the running agent (one under a remote parent with no accepted entry, a
failed stamp, or a span that started before the SDK attached); when no W3C
``traceparent`` goes out; when the entry does not fit the W3C limits; or when
the carrier already holds a ``tracestate`` header the SDK cannot parse. The
member holds the sending span's id, the current agent invocation's span id and
start time and, under a nested agent, the root invocation's span id and start
time. In work that starts new trajectories, with no agent since, it is the
fresh entry and holds only the sending span's id. It holds nothing else unless
the session may go too: the session id is the caller's own value and may name a
person, so it rides only to a host the caller listed in ``session_hosts``, read
from the outbound CLIENT span that is current while an HTTP instrumentation
injects, or to a carrier the caller filled with
``convergent.carrier.inject(..., session=True)``.

The value is fixed width so the whole member stays under the 128 characters
past which W3C lets a hop drop it first::

    0 <workspace:8 hex> <sender:16 hex> <invocation:16 hex> <start:11 base36>  <session>
    1 <workspace:8 hex> <sender:16 hex> <invocation:16 hex> <start:11 base36>
      <root:16 hex> <root start:11 base36>                                     <session>
    2 <workspace:8 hex> <sender:16 hex>                                        <session>

The leading digit is the entry's format marker, and a receiver rejects a
marker it does not know, so a later format is never misread. ``0`` marks an
invocation that is its own root; ``1`` a nested one. ``2`` holds no
invocation: the sender sits in work that starts new trajectories, such as a
Temporal workflow run, so an agent under it is its own root. Times are
microseconds since the epoch. The workspace tag is the one ``GET /v1/check``
returned for this process's key, fetched on a thread of its own so
``init()`` never waits for it, and retried twice if the request fails. Every
``_PAUSE_S`` after that, with a tag or without, the next use asks again, so a
workspace turned off or back on reaches a running process; a failed check
keeps a tag it holds, an answer without one drops it, and only a refused key
stops asking with that key. If the SDK is later set up with another key or
endpoint, the old tag is dropped and the new key asks for its own. A process
sends an entry only once it has a tag, and accepts one only when the tag
equals its own, so a call from another workspace leaves the receiver
unstamped. An entry read while the process's tag fetch is still out, as by a
short-lived worker that extracts straight after ``init()``, is held: its spans
keep their stamp aside, and each goes out stamped once the tag arrives and
matches, or unstamped if it never does (``_processors.HeldEntryProcessor``). The session,
up to ``SESSION_LIMIT`` characters, is the tail. A session longer than that, or
holding a character a tracestate value cannot carry, is never truncated or
rewritten: it stays local and the SDK warns once.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Iterator, Mapping, MutableMapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any
from urllib.parse import urlsplit

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.propagators.textmap import (
    CarrierT,
    Getter,
    Setter,
    TextMapPropagator,
    default_getter,
    default_setter,
)
from opentelemetry.sdk.trace import Span
from opentelemetry.trace import SpanContext, SpanKind, TraceState

from . import _config, _processors, _tag_fetch
from ._telemetry_keys import (
    CONVERGENT_INVOCATION_SPAN_ID,
    CONVERGENT_INVOCATION_START_TIME_UNIX_MICROS,
    CONVERGENT_ROOT_INVOCATION_SPAN_ID,
    CONVERGENT_ROOT_INVOCATION_START_TIME_UNIX_MICROS,
)

logger = logging.getLogger("convergent.sdk")
_KEY = "convergent"
_TRACEPARENT = "traceparent"
_TRACESTATE = "tracestate"
_HEADER_LIMIT = 512
#: W3C's limit on tracestate members.
_MAX_MEMBERS = 32
#: The longest session id the entry carries. The largest entry, a nested
#: invocation's with a session this long, is 126 characters with its key.
SESSION_LIMIT = 36
_TAG = _tag_fetch._TAG
_TIME_WIDTH = 11
_TIME_LIMIT = 36**_TIME_WIDTH
_DIGITS = "0123456789abcdefghijklmnopqrstuvwxyz"
_ID = "[0-9a-f]{16}"
_TIME = f"[0-9a-z]{{{_TIME_WIDTH}}}"
#: Printable ASCII but space, comma and equals: what a tracestate value may hold.
_SESSION = re.compile(rf"[!-+\--<>-~]{{1,{SESSION_LIMIT}}}")
#: One W3C tracestate member, as OpenTelemetry's parser accepts it without a warning.
_TRACESTATE_MEMBER = re.compile(
    r"(?:[a-z][_0-9a-z\-\*\/]{0,255}|"
    r"[a-z0-9][_0-9a-z\-\*\/]{0,240}@[a-z][_0-9a-z\-\*\/]{0,13})="
    r"[\x20-\x2b\x2d-\x3c\x3e-\x7e]{0,255}[\x21-\x2b\x2d-\x3c\x3e-\x7e][ \t]*"
)
_ROOT_ENTRY = re.compile(f"0({_TAG})({_ID})({_ID})({_TIME})({_SESSION.pattern})?")
_NESTED_ENTRY = re.compile(f"1({_TAG})({_ID})({_ID})({_TIME})({_ID})({_TIME})({_SESSION.pattern})?")
_FRESH_ENTRY = re.compile(f"2({_TAG})({_ID})({_SESSION.pattern})?")
#: Set by ``convergent.carrier.inject(..., session=True)`` for one call.
_SEND_SESSION: ContextVar[bool] = ContextVar("convergent_send_session", default=False)


@contextmanager
def _sending_session(send: bool = True) -> Iterator[None]:
    """Send the session id with every carrier injected inside this block."""
    token = _SEND_SESSION.set(send)
    try:
        yield
    finally:
        _SEND_SESSION.reset(token)


def _warn_once(key: str, message: str) -> None:
    from . import _core  # deferred: _core imports this module

    _core._warn_once(key, message)


def _valid_span_id(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(_ID, value) is not None and value != "0" * 16


def _time(value: object) -> str | None:
    if type(value) is not int or not 0 <= value < _TIME_LIMIT:
        return None
    digits = []
    for _ in range(_TIME_WIDTH):
        value, digit = divmod(value, 36)
        digits.append(_DIGITS[digit])
    return "".join(reversed(digits))


def _encode(
    stamp: Mapping[str, Any] | None, tag: str, sender_id: str, session_id: str | None
) -> str | None:
    """The entry for ``stamp``, or None when the stamp is not one the entry can carry.

    No stamp gives the fresh entry, for a sender whose work starts new trajectories.
    """
    if stamp is None:
        return f"2{tag}{sender_id}{session_id or ''}" if _valid_span_id(sender_id) else None
    invocation = stamp.get(CONVERGENT_INVOCATION_SPAN_ID)
    root = stamp.get(CONVERGENT_ROOT_INVOCATION_SPAN_ID)
    start = _time(stamp.get(CONVERGENT_INVOCATION_START_TIME_UNIX_MICROS))
    root_start = _time(stamp.get(CONVERGENT_ROOT_INVOCATION_START_TIME_UNIX_MICROS))
    ids = (sender_id, invocation, root)
    if not all(_valid_span_id(value) for value in ids) or start is None or root_start is None:
        return None
    if (root, root_start) == (invocation, start):
        value = f"0{tag}{sender_id}{invocation}{start}"
    else:
        value = f"1{tag}{sender_id}{invocation}{start}{root}{root_start}"
    return value + (session_id or "")


def _decode(value: str) -> tuple[dict[str, Any] | None, str, str | None, str] | None:
    """The entry's stamp, keyed by stamp key, its sender span id, session id and workspace tag.

    A fresh entry has no stamp.
    """
    if match := _FRESH_ENTRY.fullmatch(value):
        tag, sender_id, session_id = match.groups()
        return (None, sender_id, session_id, tag) if _valid_span_id(sender_id) else None
    if match := _ROOT_ENTRY.fullmatch(value):
        tag, sender_id, invocation, start, session_id = match.groups()
        root, root_start = invocation, start
    elif match := _NESTED_ENTRY.fullmatch(value):
        tag, sender_id, invocation, start, root, root_start, session_id = match.groups()
    else:
        return None
    if not all(_valid_span_id(span_id) for span_id in (sender_id, invocation, root)):
        return None
    stamp = {
        CONVERGENT_INVOCATION_SPAN_ID: invocation,
        CONVERGENT_INVOCATION_START_TIME_UNIX_MICROS: int(start, 36),
        CONVERGENT_ROOT_INVOCATION_SPAN_ID: root,
        CONVERGENT_ROOT_INVOCATION_START_TIME_UNIX_MICROS: int(root_start, 36),
    }
    return stamp, sender_id, session_id, tag


def _header_values(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
        return [item for item in value if isinstance(item, str)]
    return []


def _parse(header: str) -> TraceState | None:
    """``header`` as a TraceState, or None when it breaks the W3C rules."""
    header = header.strip(" \t")
    members = [member for member in re.split(r"[ \t]*,[ \t]*", header) if member]
    if len(members) > _MAX_MEMBERS or any(
        _TRACESTATE_MEMBER.fullmatch(member) is None for member in members
    ):
        return None
    state = TraceState.from_header([header])
    # from_header answers an invalid header with an empty state, not an error.
    return state if state or not members else None


def _set_entry(header: str, value: str) -> str | None:
    """``header`` with ``value`` as its one Convergent entry, or None when that does not fit."""
    if len(header) > _HEADER_LIMIT:
        return None
    state = _parse(header)
    if state is None:
        return None
    if _KEY in state:
        state = state.delete(_KEY)
    if len(state) >= _MAX_MEMBERS:
        # add would refuse it too, but OpenTelemetry logs a warning on every refusal.
        return None
    state = state.add(_KEY, value)
    result = state.to_header()
    return result if _KEY in state and len(result) <= _HEADER_LIMIT else None


def _cut_entry(header: str) -> str:
    """``header`` minus its ``convergent=`` member, every other byte between the ends as it came.

    The ends are trimmed: a header value may not start or end with a space, and
    cutting the first or last member can leave one there.
    """
    if f"{_KEY}=" not in header:
        return header
    kept = (m for m in header.split(",") if not m.strip(" \t").startswith(f"{_KEY}="))
    return ",".join(kept).strip(" \t")


def _carrier_tracestates(carrier: object) -> dict[str, list[object]]:
    """Every value a mapping carrier holds under a ``tracestate`` key of any case.

    A multidict can hold one key several times, once per header line.
    """
    found: dict[str, list[object]] = {}
    if isinstance(carrier, Mapping):
        for key, value in carrier.items():
            if isinstance(key, str) and key.lower() == _TRACESTATE:
                found.setdefault(key, []).append(value)
    return found


def _cut_value(value: object) -> object:
    """``value`` minus any ``convergent=`` member, as a string or list of lines, as it came."""
    if isinstance(value, str):
        return _cut_entry(value)
    if isinstance(value, list | tuple) and any(_cut_value(line) != line for line in value):
        lines = (_cut_value(line) for line in value)
        return type(value)(line for line in lines if line != "")
    return value


def _cut_carrier_entry(carrier: object) -> None:
    """Cut a ``convergent=`` member a reused mapping carrier still holds, in place.

    Each ``tracestate`` value keeps its key and shape; one left empty is removed.
    """
    try:
        if not isinstance(carrier, MutableMapping):
            return
        pairs = [
            (key, value)
            for key, value in carrier.items()
            if isinstance(key, str) and key.lower() == _TRACESTATE
        ]
        cut = [(key, _cut_value(value)) for key, value in pairs]
        if cut == pairs:
            return
        if callable(add := getattr(carrier, "add", None)):
            # A multidict holds a key once per header line, and a case-insensitive
            # one deletes every case at once, so its lines are written back in order.
            for key in dict.fromkeys(key for key, _ in pairs):
                if key in carrier:
                    del carrier[key]
            for key, value in cut:
                if value:
                    add(key, value)
            return
        for (key, value), (_, new) in zip(pairs, cut, strict=True):
            if new == value:
                continue
            if new:
                carrier[key] = new
            else:
                del carrier[key]
    except Exception as exc:  # noqa: BLE001 - telemetry must never break application work
        # Traceback locals could carry the carrier's headers, including Authorization.
        logger.debug("Convergent could not cut the carrier's old entry (%s)", type(exc).__name__)


def _without_remote_entry(extracted: Context) -> Context:
    """``extracted`` with the Convergent entry dropped from the remote trace state.

    Child spans copy the remote trace state, and any other propagator or
    exporter forwards it. The entry may hold a session id, so it never stays
    there; what the SDK accepts lives only in its own record.
    """
    span_context = trace.get_current_span(extracted).get_span_context()
    if not span_context.is_valid or _KEY not in span_context.trace_state:
        return extracted
    stripped = SpanContext(
        span_context.trace_id,
        span_context.span_id,
        is_remote=True,
        trace_flags=span_context.trace_flags,
        trace_state=span_context.trace_state.delete(_KEY),
    )
    return trace.set_span_in_context(trace.NonRecordingSpan(stripped), extracted)


def _carrier_value(carrier: CarrierT, getter: Getter[CarrierT], key: str) -> str:
    """The header joined from every line it came on, as HTTP lets a hop split it."""
    return ",".join(_header_values(getter.get(carrier, key)))


def _destination_host(span: object) -> str | None:
    """The host an outbound CLIENT span names, from the attributes HTTP instrumentations set."""
    if getattr(span, "kind", None) is not SpanKind.CLIENT:
        return None
    attributes = getattr(span, "attributes", None) or {}
    host = attributes.get("server.address")
    if not isinstance(host, str):
        url = attributes.get("url.full", attributes.get("http.url"))
        host = urlsplit(url).hostname if isinstance(url, str) else None
    return host.lower().rstrip(".") if host else None


def _active_transport(
    context: Context | None, session_hosts: frozenset[str]
) -> tuple[str, dict[str, Any] | None, str | None] | None:
    active = trace.get_current_span(context)
    span_context = active.get_span_context()
    if not span_context.is_valid:
        return None
    parent_id = f"{span_context.span_id:016x}"
    if getattr(active, _processors._TRAJECTORY_FAILED_FIELD, False):
        return None
    # A stamp from a held entry goes out only once the entry passed its check.
    held = _processors._held_stamp(active)
    if held is not None and held.entry.check() is not _processors._Answer.ACCEPTED:
        return None
    session_id = _processors.context_pairs(context).get(_processors.CONVERGENT_SESSION_ID)
    # An empty state means no local agent above: the same as no stamp here. Only
    # a local span holds a stamp.
    stamp = getattr(active, _processors._TRAJECTORY_FIELD, None) or None
    # A span no local agent stamped, such as a dropped SERVER span in a middle
    # service, forwards the stamp it received. Under an attached extract()
    # context, the remote parent itself rebuilds the entry from what was
    # accepted, never passing it on as received.
    received = None if stamp is not None else _processors._received_from(active, context)
    if received is not None and _processors._checked(received) is None:
        return None
    # A received fresh entry with no local agent since: still fresh.
    fresh = False
    if received is not None:
        stamp = received.stamp
        fresh = stamp is None
        if not isinstance(session_id, str):
            session_id = received.session_id
    if not isinstance(stamp, Mapping) and not fresh:
        return None
    if not isinstance(session_id, str) and isinstance(active, Span):
        # A received session is carried on the active span's private marks so it
        # follows ordinary local work without leaking into ambient context. A live
        # local session scope takes precedence when both are present.
        session_id = _processors._parent_marks(context).get(_processors.CONVERGENT_SESSION_ID)
    if not isinstance(session_id, str) and held is not None:
        session_id = held.session_id
    if not isinstance(session_id, str):
        session_id = None
    elif not _SEND_SESSION.get() and _destination_host(active) not in session_hosts:
        session_id = None
    return parent_id, dict(stamp) if isinstance(stamp, Mapping) else None, session_id


class _RecordingSetter(Setter[CarrierT]):
    """Passes writes through, holding back ``tracestate`` so it is written once."""

    def __init__(self, inner: Setter[CarrierT]) -> None:
        self.inner = inner
        self.traceparent: str | None = None
        self.tracestate: str | None = None

    def set(self, carrier: CarrierT, key: str, value: str) -> None:
        if key.lower() == _TRACESTATE:
            self.tracestate = value
            return
        if key.lower() == _TRACEPARENT:
            self.traceparent = value
        self.inner.set(carrier, key, value)


def _pass_on_without_entry(
    carrier: CarrierT, recording: _RecordingSetter[CarrierT], setter: Setter[CarrierT]
) -> None:
    """Write what the inner propagator wrote, cutting only an old ``convergent=`` member."""
    _cut_carrier_entry(carrier)
    if recording.tracestate is None:
        return
    if header := _cut_entry(recording.tracestate):
        setter.set(carrier, _TRACESTATE, header)
        return
    # The inner propagator would have overwritten this key; a header left from
    # an earlier request must not go out in its place.
    try:
        if isinstance(carrier, MutableMapping):
            carrier.pop(_TRACESTATE, None)
    except Exception as exc:  # noqa: BLE001 - telemetry must never break application work
        logger.debug(
            "Convergent could not clear the carrier's old tracestate (%s)", type(exc).__name__
        )


class ConvergentPropagator(TextMapPropagator):
    """Delegate OTel propagation, adding one bounded Convergent tracestate member.

    ``session_hosts`` lists the hosts that may receive the session id; with
    none listed, only the stamp is sent.
    """

    def __init__(self, inner: TextMapPropagator, session_hosts: Iterable[str] = ()) -> None:
        self._inner = inner
        self.session_hosts = _config._session_hosts(session_hosts)

    def inject(
        self,
        carrier: CarrierT,
        context: Context | None = None,
        setter: Setter[CarrierT] = default_setter,
    ) -> None:
        tag = _tag_fetch._current_tag()
        if tag is None:
            self._inject_without_tag(carrier, context, setter)
            return
        recording = _RecordingSetter(setter)
        self._inner.inject(carrier, context, recording)
        previous = recording.tracestate
        # Only a mapping can be read back without the caller's getter. A reused
        # carrier may still hold an earlier call's entry, under any key case.
        stale: dict[str, list[object]] = {}
        try:
            stale = _carrier_tracestates(carrier)
            if previous is None and stale:
                lines = [
                    line
                    for values in stale.values()
                    for value in values
                    for line in _header_values(value)
                ]
                previous = ",".join(lines) or None
        except Exception as exc:  # noqa: BLE001 - telemetry must never break application work
            logger.debug(
                "Convergent could not read the carrier's tracestate (%s)", type(exc).__name__
            )
        tracestate = None
        try:
            tracestate = self._with_entry(previous or "", tag, context, recording)
        except Exception as exc:  # noqa: BLE001 - telemetry must never break application work
            logger.debug("Convergent tracestate entry skipped (%s)", type(exc).__name__)
        if tracestate is None:
            # An entry already in the header is never passed on as it came: it
            # may carry a session id the gate above would not send.
            _pass_on_without_entry(carrier, recording, setter)
            return
        try:
            if isinstance(carrier, MutableMapping):
                for key in stale:
                    while key in carrier:
                        carrier.pop(key)
        except Exception as exc:  # noqa: BLE001 - telemetry must never break application work
            logger.debug(
                "Convergent could not clear the carrier's old tracestate (%s)", type(exc).__name__
            )
        if tracestate:
            setter.set(carrier, _TRACESTATE, tracestate)

    def _inject_without_tag(
        self, carrier: CarrierT, context: Context | None, setter: Setter[CarrierT]
    ) -> None:
        """No entry to add, so only an existing one is cut, as off is a no-op.

        An entry left on a remote parent by a plain W3C extract, or in a reused
        carrier, may hold a session id; every other byte goes out as it came.
        """
        recording = _RecordingSetter(setter)
        self._inner.inject(carrier, context, recording)
        _pass_on_without_entry(carrier, recording, setter)

    def _with_entry(
        self,
        previous: str,
        tag: str,
        context: Context | None,
        recording: _RecordingSetter[CarrierT],
    ) -> str | None:
        """The tracestate with the Convergent entry appended, or None to leave it alone."""
        active = _active_transport(context, self.session_hosts)
        if recording.traceparent is None or active is None:
            return None
        parent_id, stamp, session_id = active
        fields = recording.traceparent.split("-")
        if len(fields) != 4 or fields[2] != parent_id:
            return None
        if session_id is not None and _SESSION.fullmatch(session_id) is None:
            _warn_once(
                "tracestate_session_invalid",
                "Convergent did not send a session id over tracestate: it is longer than "
                f"{SESSION_LIMIT} characters or holds a space, comma, equals sign or "
                "non-ASCII character. The trajectory entry went without it.",
            )
            session_id = None
        value = _encode(stamp, tag, parent_id, session_id)
        if value is None:
            return None
        if len(previous) <= _HEADER_LIMIT and _parse(previous) is None:
            _warn_once(
                "tracestate_unparseable",
                "Convergent could not parse the tracestate header already on an outbound "
                "request, so it added no trajectory entry and removed only a convergent= "
                "member from the header, leaving the rest as it came.",
            )
            return None
        result = _set_entry(previous, value)
        if result is None and session_id is not None:
            result = _set_entry(previous, value.removesuffix(session_id))
            if result is not None:
                _warn_once(
                    "tracestate_session_fit",
                    "Convergent did not send a session id over tracestate: with it, the "
                    "header would break W3C limits (512 characters, 32 members). The "
                    "trajectory entry went without it.",
                )
        if result is None:
            _warn_once(
                "tracestate_limit",
                "Convergent context did not fit W3C tracestate limits; session data was "
                "dropped first and the trajectory entry was omitted if it still did not fit.",
            )
        return result

    def extract(
        self,
        carrier: CarrierT,
        context: Context | None = None,
        getter: Getter[CarrierT] = default_getter,
    ) -> Context:
        extracted = self._inner.extract(carrier, context, getter)
        stripped = None
        try:
            stripped = _without_remote_entry(extracted)
            return self._with_remote(carrier, getter, stripped)
        except Exception as exc:  # noqa: BLE001 - telemetry must never break application work
            logger.debug("Convergent tracestate entry ignored (%s)", type(exc).__name__)
            if stripped is not None:
                return stripped
            # Fail closed: the extracted context may still hold the entry and
            # its session, so drop the remote parent rather than return it.
            return context if context is not None else Context()

    def _with_remote(
        self, carrier: CarrierT, getter: Getter[CarrierT], extracted: Context
    ) -> Context:
        span_context: SpanContext = trace.get_current_span(extracted).get_span_context()
        if not span_context.is_valid:
            return extracted
        header = _carrier_value(carrier, getter, _TRACESTATE)
        # Nothing to accept without an entry: the header is left to the inner
        # propagator, unparsed.
        if f"{_KEY}=" not in header:
            return extracted
        state = _parse(header) if len(header) <= _HEADER_LIMIT else None
        entry = state.get(_KEY) if state is not None else None
        if entry is None:
            return extracted
        decoded = _decode(entry)
        if decoded is None:
            return extracted
        stamp, sender_id, session_id, tag = decoded
        if sender_id != f"{span_context.span_id:016x}":
            return extracted
        # While this process's tag is still on its way, as in a short-lived
        # worker that extracts straight after init(), the entry is held for the
        # check. With no fetch out and no tag, it is refused.
        own, awaiting = _tag_fetch._tag_state()
        held = _processors._Held(tag) if awaiting else None
        # Another workspace's entry is not accepted: the receiver stays unstamped.
        if held is None and tag != own:
            return extracted
        return _processors.with_received(extracted, stamp, session_id, held)

    @property
    def fields(self) -> set[str]:
        return set(self._inner.fields) | {_TRACESTATE}


def public_propagator() -> TextMapPropagator:
    """The global propagator when it is ours, else one wrapping it.

    Reading it never changes the global propagator.
    """
    # Deferred, as in the two below: importing opentelemetry.propagate reads
    # OTEL_PROPAGATORS and raises on a name it cannot load, which must not break
    # ``import convergent``.
    from opentelemetry.propagate import get_global_textmap

    current = get_global_textmap()
    return current if isinstance(current, ConvergentPropagator) else ConvergentPropagator(current)


def _warn_if_replaced() -> None:
    """Warn once when the propagator ``init()`` installed is no longer global."""
    from opentelemetry.propagate import get_global_textmap

    if isinstance(get_global_textmap(), ConvergentPropagator):
        return
    _warn_once(
        "propagator_replaced",
        "The global OpenTelemetry propagator was replaced after convergent.init(), so "
        "outbound requests no longer carry the Convergent tracestate entry. Set your own "
        "propagator before init(), and init() wraps it.",
    )


def install_global(session_hosts: Iterable[str] = ()) -> None:
    from opentelemetry.propagate import get_global_textmap, set_global_textmap

    current = get_global_textmap()
    if isinstance(current, ConvergentPropagator):
        current.session_hosts = _config._session_hosts(session_hosts)
    else:
        set_global_textmap(ConvergentPropagator(current, session_hosts))
    _processors._propagator_check = _warn_if_replaced


def _reset_for_tests() -> None:
    """Unwrap the global propagator ``init()`` wrapped."""
    from opentelemetry.propagate import get_global_textmap, set_global_textmap

    current = get_global_textmap()
    if isinstance(current, ConvergentPropagator):
        set_global_textmap(current._inner)
    _processors._ever_received = False
    _processors._propagator_check = None
