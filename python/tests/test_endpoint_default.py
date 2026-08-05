"""The endpoint the published docs promise, pinned in a file this repo owns.

``python/docs/reference/api.md`` tells a customer that ``endpoint`` falls back to
``CONVERGENT_ENDPOINT`` and then to ``https://ingest.convergent.dev``, so a key on
its own is enough to reach Convergent. These four cases are pinned here, in this repo's own suite, so
the four cases the docs describe stay pinned across a cut.

The resolved endpoint is read off the running configuration rather than off the
function that computes it, because that function has already moved between modules
once. What the docs promise is what ``init()`` ends up configured with.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import convergent
from convergent import _core, _registry, _transport

_KEY = "k-endpoint"  # pragma: allowlist secret
_DOCUMENTED_DEFAULT = "https://ingest.convergent.dev"
_FROM_THE_ENVIRONMENT = "https://collector.example.test"
_FROM_THE_ARGUMENT = "https://receiver.example.test"


@pytest.fixture(autouse=True)
def reset_sdk(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name in (
        "CONVERGENT_API_KEY",
        "CONVERGENT_ENDPOINT",
        "CONVERGENT_RELEASE",
        "CONVERGENT_SPANS_DIR",
        "CONVERGENT_TRACES_EXPORTER",
        "CONVERGENT_DEBUG",
        "CONVERGENT_STRICT",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(
        _registry, "post_json", lambda *a, **k: {"deployment_id": "dep_test", "is_new": True}
    )
    monkeypatch.setattr(
        _transport, "build_processor", lambda **_: SimpleSpanProcessor(InMemorySpanExporter())
    )
    _reset_otel()
    _core._reset_for_tests()
    yield
    _core._reset_for_tests()
    _reset_otel()


def _reset_otel() -> None:
    trace._TRACER_PROVIDER_SET_ONCE._done = False  # type: ignore[attr-defined]
    trace._TRACER_PROVIDER = None  # type: ignore[attr-defined]


def _configured_endpoint() -> str | None:
    config = _core._running_config()
    assert config is not None, "init() configured nothing, so it resolved no endpoint"
    return config.endpoint


def test_a_key_on_its_own_reaches_the_managed_ingest_endpoint() -> None:
    """The quickstart: a key, a release, and nothing about an address."""
    convergent.init(api_key=_KEY, release="r1")

    assert _configured_endpoint() == _DOCUMENTED_DEFAULT


def test_the_environment_variable_replaces_the_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """How a customer-hosted receiver or a local collector is pointed at."""
    monkeypatch.setenv("CONVERGENT_ENDPOINT", _FROM_THE_ENVIRONMENT)

    convergent.init(api_key=_KEY, release="r1")

    assert _configured_endpoint() == _FROM_THE_ENVIRONMENT


def test_the_argument_wins_over_the_environment_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Precedence the whole configuration shares: argument, variable, default."""
    monkeypatch.setenv("CONVERGENT_ENDPOINT", _FROM_THE_ENVIRONMENT)

    convergent.init(api_key=_KEY, endpoint=_FROM_THE_ARGUMENT, release="r1")

    assert _configured_endpoint() == _FROM_THE_ARGUMENT


def test_a_keyless_process_gets_no_endpoint_at_all(tmp_path: Path) -> None:
    """The default belongs to the key. A file-only process has no address, and
    giving it one would aim a keyless process at the managed receiver."""
    convergent.init(release="r1", destinations=[convergent.File(tmp_path)])

    assert _configured_endpoint() is None
