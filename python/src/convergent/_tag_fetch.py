"""The workspace tag the Convergent tracestate entry carries, fetched in the background.

``init()`` and ``otel.install()`` call :func:`fetch_workspace_tag`, which asks
``GET /v1/check`` for the tag on a thread of its own so setup never waits for it,
retrying twice on a network error or a 429 or 5xx. Every ``_PAUSE_S`` after
that, with a tag or without, the next use asks again, so a workspace turned off
or back on reaches a running process. A failed check keeps a tag it holds, an
answer without one drops it, and a refused key (401) stops asking. Readers call
:func:`_current_tag`.
"""

from __future__ import annotations

import math
import os
import re
import threading
import time

from opentelemetry import context as otel_context

from . import _registry, _transport
from ._receiver_routes import CHECK_PATH
from ._registry import _RETRYABLE_STATUS
from ._user_agent import USER_AGENT


class _TagState:
    """What the fetch knows, in one place, as ``_artifacts`` keeps its upload pause.

    ``tag``: the workspace tag ``GET /v1/check`` returned, or None before it
    answered, when it failed, or from a server too old to send one.
    ``request``: the check request's url and headers, from the first init() or
    install() that asked, until ``install()`` abandons that setup.
    ``thread``: the thread making that request and its retries. While set, no
    second thread starts; it clears itself however it ends, and a forked child
    clears it too.
    ``paused_until``: until this ``time.monotonic()`` reading, no new fetch
    starts; ``math.inf`` once the server refused the key.
    """

    def __init__(self) -> None:
        self.tag: str | None = None
        self.request: tuple[str, dict[str, str]] | None = None
        self.thread: threading.Thread | None = None
        self.paused_until = 0.0


_state = _TagState()
_tag_lock = threading.Lock()
#: Retries after a failed first attempt, each after a ``_registry.backoff_delay`` wait.
_RETRIES = 2
_RETRY_BASE_S = 2.0
_RETRY_CAP_S = 4.0
#: Every fetch, with a tag or without, waits this long before the next use asks
#: again, as file uploads pause in ``_artifacts``: about six checks an hour.
_PAUSE_S = 600.0
_TAG = "[0-9a-f]{8}"


def fetch_workspace_tag(endpoint: str, api_key: str) -> None:
    """Ask ``GET /v1/check`` for this process's workspace tag, without waiting.

    The first call's key and endpoint are the ones every check uses.
    """
    with _tag_lock:
        if _state.request is None:
            _state.request = (
                f"{endpoint.rstrip('/')}{CHECK_PATH}",
                {"Authorization": f"Bearer {api_key}", "user-agent": USER_AGENT},
            )
    _start_tag_fetch()


def _start_tag_fetch() -> None:
    with _tag_lock:
        request = _state.request
        if request is None or _state.thread is not None or time.monotonic() < _state.paused_until:
            return
        thread = _state.thread = threading.Thread(
            target=_fetch_tag, args=(request,), name="convergent-workspace-tag", daemon=True
        )
        try:
            thread.start()
            return
        except Exception as exc:  # noqa: BLE001 - no thread means no tag, which fails closed
            _state.thread = None
            reason = type(exc).__name__
    _pause_tag(reason)


def _pause_tag(reason: str | None) -> None:
    """Pause asking; the next use after ``_PAUSE_S`` asks again.

    ``reason`` is None when nothing needs saying: the server answered, with a
    tag or without one (how a workspace with propagation off answers), or a
    check failed while a tag is held.
    """
    _state.paused_until = time.monotonic() + _PAUSE_S
    if reason is not None:
        _warn_once(
            "workspace_tag_paused",
            f"Convergent could not get its workspace tag ({reason}) and will ask again in "
            f"{_PAUSE_S / 60:g} minutes. Until then, this process neither sends nor accepts "
            "the Convergent tracestate entry.",
        )


def _fetch_tag(request: tuple[str, dict[str, str]]) -> None:
    """Up to ``1 + _RETRIES`` attempts, then a pause; one while a tag is held.

    Only a failed request is retried, only on a network error or a retryable
    status (429 or 5xx), and only while there is no tag; a failed
    check keeps a tag already held. An answer without a tag (a workspace with
    propagation off, or a server too old to send one) drops the tag. Every
    fetch ends in a pause of ``_PAUSE_S``, except a 401, which ends fetching.
    The request runs with instrumentation suppressed, so it records no span.
    An error that gets past the handlers still ends in the pause.
    """
    paused_until = _state.paused_until
    try:
        _ask_for_tag(request)
    finally:
        with _tag_lock:
            # Setup abandoned while this fetch was out: the state is the next setup's.
            if _state.request is request:
                _state.thread = None
                if _state.paused_until == paused_until:
                    _state.paused_until = time.monotonic() + _PAUSE_S
        _send_held()


def _send_held() -> None:
    """Send the spans that waited for this answer, so an idle process does not keep them.

    Runs after the lock is let go: the release checks the tag, which takes it.
    """
    from . import _processors  # deferred: _processors reads this module

    try:
        _processors.release_answered()
    except Exception:  # noqa: BLE001 - telemetry must never break application work
        pass


def _ask_for_tag(request: tuple[str, dict[str, str]]) -> None:
    url, headers = request
    otel_context.attach(_transport.suppressed())
    held = _state.tag is not None
    retries = 0 if held else _RETRIES
    reason: str | None = None
    for attempt in range(1 + retries):
        time.sleep(
            _registry.backoff_delay(attempt - 1, base=_RETRY_BASE_S, cap=_RETRY_CAP_S)
            if attempt
            else 0.0
        )
        try:
            tag = _registry.get_json(url, headers=headers).get("workspace_tag")
        except Exception as exc:  # noqa: BLE001 - telemetry must never break application work
            if _state.request is not request:
                return
            # No exc_info: a handler that captures locals would carry the bearer header.
            # get_json's own errors carry a short, log-safe ``reason`` string.
            given = getattr(exc, "reason", None)
            reason = given if isinstance(given, str) else type(exc).__name__
            if reason == "http_401":
                _stop_tag(reason)
                return
            if held:
                reason = None
            elif not _retryable(reason):
                break
            elif attempt < retries:
                _warn_once(
                    "workspace_tag_retry",
                    f"Convergent could not fetch its workspace tag ({reason}) and will retry "
                    f"up to {retries - attempt} times. Until it has one, this process sends no "
                    "Convergent tracestate entry, and holds the spans under an entry it "
                    "receives until the fetch answers.",
                )
            continue
        if _state.request is not request:
            return
        valid = isinstance(tag, str) and re.fullmatch(_TAG, tag) is not None
        _state.tag = tag if valid else None
        reason = None if valid or tag is None else "malformed tag"
        break
    _pause_tag(reason)


def _retryable(reason: str) -> bool:
    """No HTTP status, or one in ``_RETRYABLE_STATUS``, the rule ``_artifacts._step`` uses."""
    status = reason.removeprefix("http_")
    return status == reason or (status.isdigit() and int(status) in _RETRYABLE_STATUS)


def _forget() -> None:
    """Setup was abandoned: drop its request and tag, so the next setup asks with its own key."""
    with _tag_lock:
        _state.tag = None
        _state.request = None
        _state.thread = None
        _state.paused_until = 0.0


def _stop_tag(reason: str) -> None:
    """The server refused the key: no tag and no fetch for the rest of the process."""
    _state.tag = None
    _state.paused_until = math.inf
    _warn_once(
        "workspace_tag_refused",
        f"Convergent could not fetch its workspace tag ({reason}): the API key was refused, "
        "so it will not ask again. This process neither sends nor accepts the Convergent "
        "tracestate entry.",
    )


def _warn_once(key: str, message: str) -> None:
    from . import _core  # deferred: _core imports this module

    _core._warn_once(key, message)


def _current_tag() -> str | None:
    """This process's workspace tag, or None while it has none.

    With no fetch running, the first ask after a pause starts the next one,
    with a tag or without. A child forked while the fetch was out has no
    thread making it, so the child's first ask starts its own.
    """
    tag = _state.tag
    if (
        _state.request is not None
        and _state.thread is None
        and time.monotonic() >= _state.paused_until
    ):
        _start_tag_fetch()
    return tag


def _tag_state() -> tuple[str | None, bool]:
    """:func:`_current_tag`, and whether a tag may still arrive: none is held and a fetch is out.

    A fetch sets the tag before it clears its thread, so the thread is read
    first: a fetch seen running counts as still out even if it ends before the
    tag is read, and one seen gone has already set whatever tag it found. A
    fetch the read of the tag starts counts as out too.
    """
    thread = _state.thread
    tag = _current_tag()
    return tag, tag is None and (thread is not None or _state.thread is not None)


def _reset_after_fork() -> None:
    """Give a forked child its own lock and, if the fetch was still out, its own fetch.

    fork() copies only the calling thread, so the lock may arrive held and the
    parent's fetch thread does not exist in the child.
    """
    global _tag_lock
    _tag_lock = threading.Lock()
    _state.thread = None


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_after_fork)
