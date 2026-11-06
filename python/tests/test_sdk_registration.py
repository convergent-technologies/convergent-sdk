from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import convergent
from convergent import _core, _transport

_KEY = "k-reg"  # pragma: allowlist secret
_ENDPOINT = "https://dp.example.test"


@pytest.fixture(autouse=True)
def reset_sdk(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name in (
        "CONVERGENT_API_KEY",
        "CONVERGENT_ENDPOINT",
        "CONVERGENT_SPANS_DIR",
        "CONVERGENT_RELEASE",
        "CONVERGENT_DEBUG",
        "CONVERGENT_STRICT",
        "OTEL_SERVICE_NAME",
    ):
        monkeypatch.delenv(name, raising=False)
    _reset_otel()
    _core._reset_for_tests()
    yield
    _core._reset_for_tests()
    _reset_otel()


def _reset_otel() -> None:
    trace._TRACER_PROVIDER_SET_ONCE._done = False  # type: ignore[attr-defined]
    trace._TRACER_PROVIDER = None  # type: ignore[attr-defined]


@pytest.fixture
def exporter(monkeypatch: pytest.MonkeyPatch) -> InMemorySpanExporter:
    recorder = InMemorySpanExporter()
    monkeypatch.setattr(_transport, "build_processor", lambda **_: SimpleSpanProcessor(recorder))
    return recorder


def _resource_attrs() -> dict[str, object]:
    provider = _core.active_provider()
    assert provider is not None
    return dict(provider.resource.attributes)


def test_init_returns_the_status_it_configured(
    monkeypatch: pytest.MonkeyPatch, exporter: InMemorySpanExporter
) -> None:

    status = convergent.init(api_key=_KEY, endpoint=_ENDPOINT, release="v9")

    assert status.enabled is True
    assert status.release == "v9"
    assert status.destinations == ["convergent"]
    assert status.mode == "owned"
    assert status.reason is None
    assert status.agents == []
    assert status.app_url is None


def test_release_falls_back_to_the_environment(
    monkeypatch: pytest.MonkeyPatch, exporter: InMemorySpanExporter
) -> None:
    monkeypatch.setenv("CONVERGENT_RELEASE", "from-env")

    status = convergent.init(api_key=_KEY, endpoint=_ENDPOINT)

    assert status.release == "from-env"


def test_a_release_argument_beats_the_environment(
    monkeypatch: pytest.MonkeyPatch, exporter: InMemorySpanExporter
) -> None:
    monkeypatch.setenv("CONVERGENT_RELEASE", "from-env")

    status = convergent.init(api_key=_KEY, endpoint=_ENDPOINT, release="from-arg")

    assert status.release == "from-arg"


def test_debug_raises_the_sdk_log_level(
    monkeypatch: pytest.MonkeyPatch, exporter: InMemorySpanExporter
) -> None:
    assert _core.logger.level == logging.NOTSET

    convergent.init(api_key=_KEY, endpoint=_ENDPOINT, release="v9", debug=True)

    assert _core.logger.level == logging.DEBUG


def test_debug_can_come_from_the_environment(
    monkeypatch: pytest.MonkeyPatch, exporter: InMemorySpanExporter
) -> None:
    monkeypatch.setenv("CONVERGENT_DEBUG", "1")

    convergent.init(api_key=_KEY, endpoint=_ENDPOINT, release="v9")

    assert _core.logger.level == logging.DEBUG


def test_the_environment_turns_debug_on_even_when_the_argument_is_false(
    monkeypatch: pytest.MonkeyPatch, exporter: InMemorySpanExporter
) -> None:
    """configuration.md: CONVERGENT_DEBUG combines with the argument, so the
    operator can turn debug logging on without a code change."""
    monkeypatch.setenv("CONVERGENT_DEBUG", "1")

    convergent.init(api_key=_KEY, endpoint=_ENDPOINT, release="v9", debug=False)

    assert _core.logger.level == logging.DEBUG


def test_tracer_provider_hands_back_the_provider_init_configured(
    monkeypatch: pytest.MonkeyPatch, exporter: InMemorySpanExporter
) -> None:
    """A caller using OpenTelemetry directly adds their own processors, samplers,
    and spans to this, so it has to be the provider our destinations are on."""
    convergent.init(api_key=_KEY, endpoint=_ENDPOINT, release="v9")

    provider = convergent.tracer_provider()
    assert provider is _core.active_provider()

    provider.get_tracer("my-app").start_span("hand-rolled").end()
    convergent.flush()

    assert [span.name for span in exporter.get_finished_spans()] == ["hand-rolled"]


def test_tracer_provider_is_a_usable_no_op_when_tracing_is_disabled(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Never ``None``. A caller writing ``tracer_provider().add_span_processor(...)``
    at startup would otherwise need a check for ``None``, to cover the one case they
    cannot see coming, which is a deployment that arrives with no credentials."""
    with caplog.at_level(logging.WARNING, logger="convergent.sdk"), pytest.raises(ValueError):
        convergent.init(strict=True)

    provider = convergent.tracer_provider()
    assert isinstance(provider, TracerProvider)

    provider.get_tracer("my-app").start_span("dropped").end()
    assert convergent.flush().ok is True


def test_tracer_provider_before_init_warns(caplog: pytest.LogCaptureFixture) -> None:
    """This is a startup order a caller cannot see going wrong. The call hands back
    a provider that records nothing, so a processor added to it stays silent.
    """
    with caplog.at_level(logging.WARNING, logger="convergent.sdk"):
        provider = convergent.tracer_provider()

    assert isinstance(provider, TracerProvider)
    assert [r for r in caplog.records if "call init()" in r.getMessage()]


def test_the_sdk_does_not_filter_content(
    monkeypatch: pytest.MonkeyPatch, exporter: InMemorySpanExporter
) -> None:
    """Nothing is scrubbed by key name.

    Matching on a key name reads as a guarantee while missing any secret held in
    a value, so a secret-looking key survives. Deciding what may leave the
    process by value belongs in a collector.
    """

    convergent.init(api_key=_KEY, endpoint=_ENDPOINT, release="v9")
    with convergent.span(name="work", operation="tool_call") as handle:
        handle.set_input(
            {"prompt": "captured", "api_key": "sk-live-secret"}  # pragma: allowlist secret
        )
    convergent.flush()

    attributes = exporter.get_finished_spans()[-1].attributes or {}
    captured = str(attributes["gen_ai.tool.call.arguments"])
    assert "captured" in captured
    assert "sk-live-secret" in captured, "the SDK does not scrub; a collector does"
    assert "convergent.input" not in attributes, "the standard field replaced ours"


def test_existing_caller_provider_is_not_replaced(
    monkeypatch: pytest.MonkeyPatch, exporter: InMemorySpanExporter
) -> None:
    """When a caller already owns the global provider we attach to it and leave
    their Resource alone."""
    theirs = TracerProvider()
    trace.set_tracer_provider(theirs)

    status = convergent.init(api_key=_KEY, endpoint=_ENDPOINT, release="v9")

    assert convergent.tracer_provider() is theirs, "attaching hands the caller's provider back"
    assert status.mode == "attached", "the status must not claim a provider we did not create"


def test_a_passed_tracer_provider_is_the_one_we_attach_to(
    monkeypatch: pytest.MonkeyPatch, exporter: InMemorySpanExporter
) -> None:
    """Naming the provider says which one to use, rather than leaving it to whatever
    the global lookup happens to return when init() runs."""
    theirs = TracerProvider()

    status = convergent.init(api_key=_KEY, endpoint=_ENDPOINT, release="v9", tracer_provider=theirs)

    assert status.mode == "attached"
    assert convergent.tracer_provider() is theirs
    assert trace._TRACER_PROVIDER is None, "attaching does not set the global provider"  # type: ignore[attr-defined]

    theirs.get_tracer("my-app").start_span("hand-rolled").end()
    convergent.flush()

    assert [span.name for span in exporter.get_finished_spans()] == ["hand-rolled"]


def test_a_passed_tracer_provider_beats_the_global_one(
    monkeypatch: pytest.MonkeyPatch, exporter: InMemorySpanExporter
) -> None:
    """A process that set a global provider can still choose a different one for us,
    which is how it keeps our processors off everything else it records."""
    global_provider = TracerProvider()
    trace.set_tracer_provider(global_provider)
    theirs = TracerProvider()

    convergent.init(api_key=_KEY, endpoint=_ENDPOINT, release="v9", tracer_provider=theirs)

    assert convergent.tracer_provider() is theirs
    theirs.get_tracer("my-app").start_span("passed").end()
    global_provider.get_tracer("my-app").start_span("global").end()
    convergent.flush()

    assert [span.name for span in exporter.get_finished_spans()] == ["passed"]


def test_repeat_init_with_the_same_tracer_provider_stays_silent(
    monkeypatch: pytest.MonkeyPatch,
    exporter: InMemorySpanExporter,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The provider is part of the configuration a repeat init() is compared against,
    so handing over the same one twice is the same configuration twice."""
    theirs = TracerProvider()

    with caplog.at_level(logging.WARNING, logger="convergent.sdk"):
        convergent.init(api_key=_KEY, endpoint=_ENDPOINT, release="v9", tracer_provider=theirs)
        convergent.init(api_key=_KEY, endpoint=_ENDPOINT, release="v9", tracer_provider=theirs)

    assert not [r for r in caplog.records if "already configured" in r.message]


def test_a_second_init_naming_another_tracer_provider_warns_and_keeps_the_first(
    monkeypatch: pytest.MonkeyPatch,
    exporter: InMemorySpanExporter,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Naming two providers in one process is a setup mistake the caller has to hear
    about. Our processors are on the first one, and the second call cannot move them."""
    first = TracerProvider()
    second = TracerProvider()

    with caplog.at_level(logging.WARNING, logger="convergent.sdk"):
        convergent.init(api_key=_KEY, endpoint=_ENDPOINT, release="v9", tracer_provider=first)
        convergent.init(api_key=_KEY, endpoint=_ENDPOINT, release="v9", tracer_provider=second)

    assert [r for r in caplog.records if "already configured" in r.message]
    assert convergent.tracer_provider() is first
