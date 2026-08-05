"""``convergent.check()`` reports what this process configured and what the server sees.

The server answers ``GET /v1/check`` with facts rather than finished text. The labels
and the layout below are the SDK's. The wording of each note is the server's. That is
why this SDK can print a note whose code it has never heard of instead of dropping it.
"""

from __future__ import annotations

import textwrap
import time
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass, field

from . import _core, _registry
from ._core import Status, logger

_WIDTH = 80
_LABEL = "  {:<12}"
_CONTINUATION = " " * len(_LABEL.format(""))

#: What ``Report.round_trip`` says when there was no key to call with.
_NO_CREDENTIALS = "no_credentials"

#: What ``Report.round_trip`` says when a 2xx body was not a check response. The
#: server makes ``key.organization_id`` required, so a body without it came from
#: something other than the check endpoint answering at that address.
_NOT_A_CHECK_RESPONSE = "not_a_check_response"

#: Cap on one string the server sent. Its own note messages run about 250
#: characters, so this is headroom rather than a content limit.
_MAX_FIELD_CHARS = 500

_MODES = {
    "owned": "a tracer provider we created",
    "attached": "attached to a tracer provider you own",
}

_REASONS = {
    "no_provider": (
        "Spans your own tracers record are being sent. convergent.span() and "
        "observe() are recording nothing, because the tracer provider your "
        "ConvergentSpanProcessor was added to cannot be found from here. Pass it to "
        "convergent.otel.install(), or install it with trace.set_tracer_provider()."
    ),
    "missing_config": (
        "init() or install() has not run in this process; a call that raised "
        "configured nothing. Set CONVERGENT_API_KEY. For init() you can also set "
        "CONVERGENT_SPANS_DIR to write spans to a file instead."
    ),
    "invalid_config": (
        "init() or install() was given a configuration that cannot work, and strict "
        "mode is off, so tracing is disabled and nothing is sent. The convergent.sdk "
        "logger carries the exact problem at ERROR. Set CONVERGENT_STRICT=1 to stop "
        "the process at startup instead."
    ),
    "setup_failed": (
        "init() or install() ran but tracing could not start, so nothing is being "
        "recorded. The convergent.sdk logger already carries a warning naming what "
        "failed. Set CONVERGENT_DEBUG=1 if your application filters that logger out."
    ),
    "already_configured": (
        "init() or install() ran more than once with different settings. The first "
        "configuration is the one running, and the later call's settings were not "
        "applied. Tracing itself is working."
    ),
}

#: Reasons that describe a working setup. ``already_configured`` reports that a later
#: call lost, not that anything stopped, so it must not fail a CI gate built on
#: ``bool(check())``.
_BENIGN_REASONS = frozenset({None, "already_configured"})


@dataclass(frozen=True)
class Note:
    """One problem the server can see, and what to do about it.

    ``code`` is a plain string rather than a fixed set of values. A code added after
    this SDK shipped still reaches the reader through ``message``.
    """

    code: str
    message: str


@dataclass(frozen=True)
class Report:
    """What ``check()`` found. Print it.

    ``status`` is the local half, the same thing ``init()`` returned. Everything
    else is what the server said when asked about that key and release.
    """

    status: Status
    #: ``"ok"`` when the check endpoint answered. Otherwise why it did not:
    #: ``"no_credentials"`` when there was nothing to call with,
    #: ``"not_a_check_response"`` when something else answered at that address, or a
    #: coarse transport reason such as ``"http_401"`` or ``"TimeoutError"``.
    round_trip: str = _NO_CREDENTIALS
    #: How long the round trip took. Set only when the server answered.
    round_trip_ms: int | None = None
    #: Where the round trip went, when one was attempted. Never carries the key.
    endpoint: str | None = None
    organization_id: str | None = None
    #: Agents the server has linked to this release, by name, or by id for a
    #: linked agent that has no name yet.
    agents: list[str] = field(default_factory=list)
    #: True when the server had more linked agents than it would list.
    agents_truncated: bool = False
    notes: list[Note] = field(default_factory=list)

    def __bool__(self) -> bool:
        """True when the Convergent credentials work.

        It takes tracing being on, no part of the SDK being known to be broken, and
        the server having answered for this key. A losing second ``init()`` stays
        True, because ``already_configured`` means the running configuration works.
        A correct file-only setup is False, because there is no key to answer with.
        Gate on ``Status.enabled`` and the spans file instead for that one.
        """
        return (
            self.status.enabled
            and self.status.reason in _BENIGN_REASONS
            and self.round_trip == "ok"
        )

    def __str__(self) -> str:
        status = self.status
        lines = ["convergent: " + ("enabled" if status.enabled else "disabled")]
        if status.enabled:
            lines += [
                _row("release", status.release or "not set"),
                _row("mode", _MODES.get(status.mode, status.mode)),
                _row("sending to", ", ".join(status.destinations) or "nothing"),
            ]
        else:
            lines.append(_row("reason", status.reason or "unknown"))
        # A reason can also arrive alongside enabled, for a part of the setup that
        # is not working while spans are still being sent.
        explanation = _REASONS.get(status.reason or "")
        if explanation:
            lines.append(_row("", explanation))

        lines += ["", _row("round trip", self._round_trip_line())]
        if self.round_trip != "ok":
            return "\n".join(lines)
        lines += [
            _row("key", self.organization_id or "unknown"),
            _row("agents", self._agents_line()),
            "",
            *self._notes_block(),
        ]
        return "\n".join(lines)

    def _round_trip_line(self) -> str:
        if self.round_trip == "ok":
            return f"ok ({self.round_trip_ms}ms)"
        if self.round_trip == _NO_CREDENTIALS:
            return "not attempted, there is no api key to check with"
        return f"failed ({self.round_trip})   endpoint {self.endpoint}"

    def _agents_line(self) -> str:
        if not self.agents:
            return "none linked to this release yet"
        listed = ", ".join(self.agents)
        if self.agents_truncated:
            return f"{listed}, and more the server did not list"
        return listed

    def _notes_block(self) -> list[str]:
        if not self.notes:
            return ["  no notes"]
        heading = f"  {len(self.notes)} note" + ("s" if len(self.notes) > 1 else "")
        return [heading, *(_wrap(note.message, "    - ", " " * 6) for note in self.notes)]


def check() -> Report:
    """Diagnose this installation. Takes no arguments, and never raises.

    Reads what ``init()`` configured, then asks the server what it can see for the
    same key and release. A network failure, a rejected key, or a response that
    does not parse all come back as a report saying so.

    With no api key there is nobody to ask, so the report covers the local half
    and says the round trip did not happen.
    """
    status, credentials = _core.status_and_credentials()
    if credentials is None:
        return Report(status)

    url = f"{credentials.endpoint.rstrip('/')}/v1/check"
    if status.release:
        url += "?" + urllib.parse.urlencode({"release": status.release})
    started = time.monotonic()
    try:
        body = _registry.get_json(url, headers=credentials.headers)
    except _registry.RegistrationError as exc:
        return Report(status, round_trip=exc.reason, endpoint=credentials.endpoint)
    except Exception as exc:
        # No exc_info: a handler that captures locals would carry the bearer header
        # out of get_json's frame and off the machine.
        logger.warning("Convergent could not complete its check request: %s", type(exc).__name__)
        return Report(status, round_trip="unexpected", endpoint=credentials.endpoint)

    organization_id = _string(body.get("key"), "organization_id")
    if not organization_id:
        return Report(status, round_trip=_NOT_A_CHECK_RESPONSE, endpoint=credentials.endpoint)

    return Report(
        status,
        round_trip="ok",
        round_trip_ms=int((time.monotonic() - started) * 1_000),
        endpoint=credentials.endpoint,
        organization_id=organization_id,
        agents=_agents(body),
        agents_truncated=body.get("agents_truncated") is True,
        notes=_notes(body),
    )


def _agents(body: Mapping[str, object]) -> list[str]:
    names = []
    for row in _rows(body, "agents"):
        name = _string(row, "name") or _string(row, "agent_id")
        if name:
            names.append(name)
    return names


def _notes(body: Mapping[str, object]) -> list[Note]:
    notes = []
    for row in _rows(body, "notes"):
        code, message = _string(row, "code"), _string(row, "message")
        if code and message:
            notes.append(Note(code, message))
    return notes


def _rows(body: Mapping[str, object], key: str) -> list[object]:
    value = body.get(key)
    return value if isinstance(value, list) else []


def _string(source: object, key: str) -> str | None:
    """One string field out of a response nothing has validated."""
    if isinstance(source, Mapping):
        value = source.get(key)
        if isinstance(value, str):
            return _printable(value)
    return None


def _printable(value: str) -> str:
    """Text the server sent, bounded and stripped of anything that moves a cursor.

    ``textwrap.fill`` folds newlines but keeps an escape sequence, a backspace, and a
    bidi override intact. Somebody pastes this report into a support thread, so a note
    message or an agent name must not be able to rewrite the lines above it. Agent
    names are the ones to worry about, because they come from the customer's own trace
    data rather than from us.
    """
    text = "".join(char if char.isprintable() else " " for char in value[:_MAX_FIELD_CHARS])
    return f"{text}..." if len(value) > _MAX_FIELD_CHARS else text


def _row(label: str, value: str) -> str:
    return _wrap(value, _LABEL.format(label), _CONTINUATION)


def _wrap(text: str, first: str, rest: str) -> str:
    # Neither break is allowed: an id, a path, or a hyphenated agent name split
    # across two lines is one a reader cannot paste back into a search.
    return textwrap.fill(
        text,
        width=_WIDTH,
        initial_indent=first,
        subsequent_indent=rest,
        break_long_words=False,
        break_on_hyphens=False,
    )


__all__ = ["Note", "Report", "check"]
