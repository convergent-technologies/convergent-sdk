"""The SDK's HTTP calls to the control plane: deployment registration and the
``check()`` round trip.

``init()`` must finish its POST before it builds the tracer provider, because an
OpenTelemetry ``Resource`` is immutable once constructed -- a ``deployment_id``
that arrived later could never be attached to it.

Stdlib ``urllib`` on purpose: ``urlopen`` makes exactly one attempt, and taking on
``requests`` or ``tenacity`` to retry a single POST is not worth a dependency in a
customer's process. Full jitter keeps a fleet restarting after an outage from
retrying in lockstep. Retries need no idempotency key because the server upserts
on ``(organization_id, fingerprint)``. Redirects are refused so the Bearer key is
never re-sent to a redirect target.
"""

from __future__ import annotations

import http.client
import json
import logging
import random
import time
import urllib.error
import urllib.request
from collections.abc import Callable

logger = logging.getLogger(__name__)

__all__ = ["RegistrationError", "get_json", "post_json"]

MAX_RESPONSE_BYTES = 1 * 1024 * 1024

# Retry only these; every other 4xx (a 404, a 400) is terminal on the first try.
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects so the Bearer key never re-sends to a redirect target.

    Returning ``None`` from ``redirect_request`` makes urllib surface the 3xx as
    an ``HTTPError`` instead of following it -- which ``post_json`` then treats as
    a terminal (non-retryable) status.
    """

    def redirect_request(self, *args: object, **kwargs: object) -> None:
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


class RegistrationError(RuntimeError):
    """A control-plane request that could not be completed. ``reason`` is coarse and
    safe to log -- never carries the response body (which can echo the request)."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"convergent registration failed: {reason}")
        self.reason = reason


def post_json(
    url: str,
    payload: dict[str, object],
    *,
    headers: dict[str, str],
    max_attempts: int = 3,
    base_backoff: float = 0.5,
    max_backoff: float = 5.0,
    deadline: float = 5.0,
    attempt_timeout: float = 5.0,
    sleep: Callable[[float], None] | None = None,
    monotonic: Callable[[], float] | None = None,
    rng: Callable[[], float] | None = None,
) -> dict[str, object]:
    """POST ``payload`` as JSON, retrying transient failures within bounds.

    Returns the parsed JSON object on a 2xx, and raises ``RegistrationError`` for
    everything else -- retrying only a connection error, a timeout, a 429, or a
    5xx. ``deadline`` bounds the retry *loop*, not one attempt: ``attempt_timeout``
    is urllib's per-socket-operation timeout, so a server that trickles bytes just
    under it can keep a single attempt alive past the deadline. Accepted here
    because the endpoint is either Convergent's managed ingest or a receiver the
    caller runs themselves.

    ``sleep`` / ``monotonic`` / ``rng`` resolve at call time so a test can inject a
    fake clock and still monkeypatch ``time.sleep`` at the boundary.
    """
    sleep = sleep or time.sleep
    monotonic = monotonic or time.monotonic
    rng = rng or random.random

    try:
        data = json.dumps(payload).encode("utf-8")
    except (TypeError, ValueError):
        # Keep the "raises only RegistrationError" contract total for every
        # caller of this reusable primitive, not just today's JSON-safe body.
        raise RegistrationError("bad_payload") from None
    request_headers = {**headers, "Content-Type": "application/json"}
    deadline_at = monotonic() + deadline
    reason = "unavailable"

    for attempt in range(max_attempts):
        remaining = deadline_at - monotonic()
        if remaining <= 0:
            break
        try:
            # Built inside the try: a malformed url raises ValueError from
            # Request() itself, not from the opener.
            request = urllib.request.Request(url, data=data, headers=request_headers, method="POST")
            with _OPENER.open(request, timeout=min(attempt_timeout, remaining)) as response:
                return _parse(response.read(MAX_RESPONSE_BYTES + 1))
        except urllib.error.HTTPError as exc:
            if exc.code not in _RETRYABLE_STATUS:
                raise RegistrationError(f"http_{exc.code}") from None
            reason = f"http_{exc.code}"
        except ValueError:
            # A malformed endpoint (e.g. "unknown url type") never improves on
            # retry -- terminal, and it must not escape as a raw ValueError.
            raise RegistrationError("bad_url") from None
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
            reason = type(exc).__name__

        if attempt == max_attempts - 1:
            break
        delay = min(base_backoff * 2**attempt, max_backoff) * rng()  # full jitter
        if monotonic() + delay > deadline_at:
            break
        sleep(delay)

    raise RegistrationError(reason)


def get_json(url: str, *, headers: dict[str, str], timeout: float = 5.0) -> dict[str, object]:
    """GET ``url`` and return the JSON object it answered with, in one attempt.

    This shares ``post_json``'s opener, so a redirect is refused rather than followed
    with the Bearer key attached. It also raises the same ``RegistrationError``, so a
    caller reads one coarse ``reason`` whichever way the call failed.

    There is no retry loop. The only caller is ``check()``, which a person runs and
    reads, and a person who wants a second attempt runs it again.
    """
    try:
        # Built inside the try: a malformed url raises ValueError from Request()
        # itself, not from the opener.
        request = urllib.request.Request(url, headers=headers, method="GET")
        with _OPENER.open(request, timeout=timeout) as response:
            return _parse(response.read(MAX_RESPONSE_BYTES + 1))
    except urllib.error.HTTPError as exc:
        raise RegistrationError(f"http_{exc.code}") from None
    except ValueError:
        raise RegistrationError("bad_url") from None
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
        raise RegistrationError(type(exc).__name__) from None


def _parse(raw: bytes) -> dict[str, object]:
    if len(raw) > MAX_RESPONSE_BYTES:
        raise RegistrationError("response_too_large")
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise RegistrationError("bad_json") from None
    if not isinstance(obj, dict):
        raise RegistrationError("bad_json")
    return obj
