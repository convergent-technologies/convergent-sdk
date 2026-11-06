"""The SDK's HTTP calls to the control plane: the ``check()`` round trip, and the
opener and backoff that file uploads share.

Stdlib ``urllib`` on purpose: taking on ``requests`` is not worth a dependency in
a customer's process. Full jitter keeps a fleet restarting after an outage from
retrying in lockstep. Redirects are refused so the Bearer key is never re-sent to
a redirect target.
"""

from __future__ import annotations

import functools
import http.client
import json
import random
import urllib.error
import urllib.request
from collections.abc import Callable

__all__ = ["RegistrationError", "backoff_delay", "get_json"]

MAX_RESPONSE_BYTES = 1 * 1024 * 1024

# Retry only these; every other 4xx (a 404, a 400) is terminal on the first try.
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


@functools.cache
def _opener() -> urllib.request.OpenerDirector:
    # Temporal's workflow sandbox refuses urllib.request use while importing the SDK.
    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        """Refuse redirects so the Bearer key never re-sends to a redirect target.

        Returning ``None`` from ``redirect_request`` makes urllib surface the 3xx as
        an ``HTTPError`` instead of following it, which callers treat as terminal.
        """

        def redirect_request(self, *args: object, **kwargs: object) -> None:
            return None

    return urllib.request.build_opener(_NoRedirect)


class RegistrationError(RuntimeError):
    """A control-plane request that could not be completed. ``reason`` is coarse and
    safe to log -- never carries the response body (which can echo the request)."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"convergent request failed: {reason}")
        self.reason = reason


def backoff_delay(
    attempt: int, *, base: float, cap: float, rng: Callable[[], float] = random.random
) -> float:
    """Full jitter: a random wait up to ``base * 2**attempt`` seconds, capped at ``cap``."""
    return min(base * 2**attempt, cap) * rng()


def get_json(url: str, *, headers: dict[str, str], timeout: float = 5.0) -> dict[str, object]:
    """GET ``url`` and return the JSON object it answered with, in one attempt.

    A redirect is refused rather than followed with the Bearer key attached, and
    every failure raises ``RegistrationError``, so a caller reads one coarse
    ``reason`` whichever way the call failed.

    There is no retry loop here. ``check()`` is run and read by a person, who can run
    it again. The workspace tag fetch retries on its own thread, then fails closed.
    """
    try:
        # Built inside the try: a malformed url raises ValueError from Request()
        # itself, not from the opener.
        request = urllib.request.Request(url, headers=headers, method="GET")
        with _opener().open(request, timeout=timeout) as response:
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
