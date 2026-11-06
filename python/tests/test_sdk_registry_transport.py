"""Network mechanics of ``check()``'s HTTP call, against a real loopback server.

``get_json`` runs inside a customer process and carries the org API key, so its
redirect and single-attempt behavior are contracts, not implementation details.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from convergent._registry import MAX_RESPONSE_BYTES, RegistrationError, get_json


class _FakeRegistry:
    """A loopback HTTP server that records requests and replays a status sequence.

    ``responses`` is consumed one per request; the last entry repeats once
    exhausted, so ``[(500, {}), (500, {}), (200, ok)]`` models "fail twice then
    succeed" and ``[(200, ok)]`` models "always succeed".
    """

    def __init__(self, responses: list[tuple[int, dict[str, object]]]) -> None:
        self.responses = responses
        self.requests: list[dict[str, object]] = []
        self._lock = threading.Lock()
        registry = self

        class _Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - stdlib method name
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length)
                with registry._lock:
                    registry.requests.append(
                        {
                            "path": self.path,
                            "authorization": self.headers.get("Authorization"),
                            "body": json.loads(raw) if raw else None,
                        }
                    )
                    idx = min(len(registry.requests) - 1, len(registry.responses) - 1)
                    status, body = registry.responses[idx]
                payload = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                if isinstance(body, dict) and "location" in body:  # for the redirect test
                    self.send_header("Location", str(body["location"]))
                self.end_headers()
                self.wfile.write(payload)

            # A GET has no Content-Length, so the shared handler records an empty body.
            do_GET = do_POST

            def log_message(self, format: str, *args: object) -> None:  # silence stdlib logging
                pass

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.url = f"http://127.0.0.1:{self._httpd.server_address[1]}"
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    def __enter__(self) -> _FakeRegistry:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._httpd.shutdown()
        self._thread.join(timeout=5)


def test_get_json_sends_the_credential_and_makes_one_attempt() -> None:
    """The server answers about whichever organization the key belongs to, so a GET
    that dropped the header would report on nobody."""
    with _FakeRegistry([(200, {"agents_truncated": False})]) as registry:
        result = get_json(
            f"{registry.url}/v1/check?release=v9",
            headers={"Authorization": "Bearer k-check"},  # pragma: allowlist secret
        )

    assert result == {"agents_truncated": False}
    assert registry.requests[0]["path"] == "/v1/check?release=v9"
    assert registry.requests[0]["authorization"] == "Bearer k-check"
    assert len(registry.requests) == 1


def test_get_json_reports_a_rejected_key_as_a_reason() -> None:
    with _FakeRegistry([(401, {})]) as registry:
        with pytest.raises(RegistrationError) as error:
            get_json(f"{registry.url}/v1/check", headers={})

    assert error.value.reason == "http_401"
    assert len(registry.requests) == 1, "check() asks once; a person can ask again"


def test_get_json_refuses_to_follow_a_redirect() -> None:
    """The Bearer key is never re-sent to a redirect target, so a 3xx is surfaced
    as an error instead of being followed."""
    with _FakeRegistry([(302, {})]) as registry:
        redirect: dict[str, object] = {"location": f"{registry.url}/elsewhere"}
        registry.responses[0] = (302, redirect)
        with pytest.raises(RegistrationError):
            get_json(f"{registry.url}/v1/check", headers={"Authorization": "Bearer secret"})

    assert len(registry.requests) == 1, "a redirect must not be followed (would be 2 requests)"
    assert registry.requests[0]["path"] == "/v1/check"


def test_registration_error_never_carries_the_response_body() -> None:
    """``reason`` is coarse and safe to log; the body can echo the request, which
    carries the API key."""
    with _FakeRegistry([(404, {"secret-echo": "Bearer super-secret"})]) as registry:
        with pytest.raises(RegistrationError) as error:
            get_json(f"{registry.url}/v1/check", headers={"Authorization": "Bearer super-secret"})

    assert "super-secret" not in str(error.value)
    assert error.value.reason == "http_404"


def test_get_json_rejects_an_oversized_response() -> None:
    """The size cap guards memory in the caller's process against a hostile or
    broken endpoint."""
    with _FakeRegistry([(200, {"pad": "x" * (MAX_RESPONSE_BYTES + 1)})]) as registry:
        with pytest.raises(RegistrationError) as error:
            get_json(f"{registry.url}/v1/check", headers={})

    assert error.value.reason == "response_too_large"


def test_get_json_reports_a_malformed_url_as_a_reason() -> None:
    """A bad endpoint must not escape as a raw ValueError from urllib."""
    with pytest.raises(RegistrationError) as error:
        get_json("not-a-url", headers={})

    assert error.value.reason == "bad_url"
