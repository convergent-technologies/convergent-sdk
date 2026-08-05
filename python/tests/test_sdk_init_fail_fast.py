"""Startup rejecting a bad configuration, and everything else staying quiet.

A problem that is local and deterministic -- the same call with the same inputs
failing the same way on every machine -- is settled at startup. By default
``init()`` and ``ConvergentSpanProcessor`` log it at ERROR, configure nothing,
and report ``reason="invalid_config"``, so a telemetry mistake never stops a
deployment. With ``strict=True`` or ``CONVERGENT_STRICT=1`` the same problem
raises instead, for the caller who wants a bad setup to stop the process. A
problem that depends on the network, the server, or what another library did is
neither: it stays a log line and a degraded ``Status``.

One test per condition on each side of that split, plus the mechanisms that keep
runtime safe: validation runs before any side effect, so a raising ``init()``
leaves the process exactly as it was, and only the first, claiming call may
raise at all.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import convergent
import convergent.otel
from convergent.otel import ConvergentSpanProcessor
from convergent import _config, _core, _registry, _transport
from convergent._file_export import SPANS_FILENAME

_KEY = "k-init"  # pragma: allowlist secret
_ENDPOINT = "https://dp.example.test"


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
        "OTEL_SERVICE_NAME",
        "OTEL_EXPORTER_OTLP_HEADERS",
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


def _sdk_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name.startswith("convergent.sdk")]


# --------------------------------------------------------------------------
# init() raises: the wrong type for a string argument
# --------------------------------------------------------------------------


@pytest.mark.parametrize("field", ["api_key", "endpoint", "release"])
def test_a_non_string_credential_raises_type_error(field: str) -> None:
    """Read as unset, these silently disabled tracing for the whole process."""
    given: dict[str, Any] = {field: 12345}

    with pytest.raises(TypeError):
        convergent.init(strict=True, **given)


# --------------------------------------------------------------------------
# init() raises: nothing configured, and the file-only case that is configured
# --------------------------------------------------------------------------


def test_nothing_configured_raises_value_error_naming_the_api_key_variable() -> None:
    """A deploy with a fat-fingered env var name fails at boot instead of running
    silent. A process that wants no telemetry does not call ``init()``."""
    with pytest.raises(ValueError, match="CONVERGENT_API_KEY"):
        convergent.init(strict=True)


def test_no_release_raises_value_error() -> None:
    """A trace that names no version cannot be compared with anything, so the
    release is required. Any string naming the version is accepted."""
    with pytest.raises(ValueError, match="CONVERGENT_RELEASE"):
        convergent.init(strict=True, api_key=_KEY, endpoint=_ENDPOINT)


def test_a_file_destination_with_no_api_key_is_a_complete_configuration(tmp_path: Path) -> None:
    """The sandbox case: no route to the receiver, so the file is how the trace
    gets out. Credentials are not required for that."""
    status = convergent.init(release="r1", destinations=[convergent.File(tmp_path)])

    assert status.enabled is True
    assert status.destinations == [f"file:{tmp_path / SPANS_FILENAME}"]


# --------------------------------------------------------------------------
# init() raises: a malformed endpoint
# --------------------------------------------------------------------------


def test_an_endpoint_that_is_not_a_url_raises_value_error() -> None:
    """It was accepted and failed later, at registration and at export."""
    with pytest.raises(ValueError):
        convergent.init(strict=True, api_key=_KEY, endpoint="ingest.convergent", release="r1")


def test_a_malformed_endpoint_environment_variable_raises_value_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An argument and its ``CONVERGENT_*`` variable get the same validation."""
    monkeypatch.setenv("CONVERGENT_ENDPOINT", "ingest.convergent")

    with pytest.raises(ValueError):
        convergent.init(strict=True, api_key=_KEY, release="r1")


# --------------------------------------------------------------------------
# init() raises: agents, which is a privacy control
# --------------------------------------------------------------------------


def test_agents_as_a_string_raises_type_error() -> None:
    """The reason this policy exists. A typo here used to turn the filter off with
    one log line, and every span in the process was sent."""
    with pytest.raises(TypeError):
        convergent.init(
            strict=True,
            api_key=_KEY,
            endpoint=_ENDPOINT,
            release="r1",
            agents=cast(Any, "checkout"),
        )


def test_agents_holding_something_other_than_names_raises_type_error() -> None:
    with pytest.raises(TypeError):
        convergent.init(
            strict=True, api_key=_KEY, endpoint=_ENDPOINT, release="r1", agents=cast(Any, [1, 2])
        )


def test_an_empty_agent_name_raises_value_error() -> None:
    with pytest.raises(ValueError):
        convergent.init(
            strict=True, api_key=_KEY, endpoint=_ENDPOINT, release="r1", agents=["checkout", ""]
        )


def test_more_agent_names_than_the_cap_raises_value_error() -> None:
    names = [f"agent-{index}" for index in range(_config._DECLARED_AGENT_LIMIT + 1)]

    with pytest.raises(ValueError, match=str(_config._DECLARED_AGENT_LIMIT)):
        convergent.init(strict=True, api_key=_KEY, endpoint=_ENDPOINT, release="r1", agents=names)


def test_an_agent_name_longer_than_the_cap_raises_value_error() -> None:
    with pytest.raises(ValueError, match=str(_config._AGENT_NAME_LIMIT)):
        convergent.init(
            strict=True,
            api_key=_KEY,
            endpoint=_ENDPOINT,
            release="r1",
            agents=["x" * (_config._AGENT_NAME_LIMIT + 1)],
        )


# --------------------------------------------------------------------------
# init() raises: destinations
# --------------------------------------------------------------------------


def test_a_destination_that_is_not_a_file_or_console_raises_type_error() -> None:
    """A path where a ``File`` belongs used to be swallowed, and the destination
    lost."""
    with pytest.raises(TypeError):
        convergent.init(
            strict=True,
            api_key=_KEY,
            endpoint=_ENDPOINT,
            release="r1",
            destinations=cast(Any, ["/tmp/traces"]),
        )


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permission bits")
def test_a_file_destination_under_an_unwritable_directory_raises_os_error(tmp_path: Path) -> None:
    """The directory cannot even be created, so the ``OSError`` propagates."""
    blocked = tmp_path / "blocked"
    blocked.mkdir(mode=0o500)

    with pytest.raises(OSError):
        convergent.init(
            strict=True, release="r1", destinations=[convergent.File(blocked / "spans")]
        )


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permission bits")
def test_a_file_destination_in_a_read_only_directory_raises_os_error(tmp_path: Path) -> None:
    """The directory is there and the file cannot be opened in it."""
    read_only = tmp_path / "read-only"
    read_only.mkdir(mode=0o500)

    with pytest.raises(OSError):
        convergent.init(strict=True, release="r1", destinations=[convergent.File(read_only)])


# --------------------------------------------------------------------------
# init() raises: tracer_provider
# --------------------------------------------------------------------------


def test_an_api_package_tracer_provider_raises_type_error() -> None:
    """The API package's provider has no ``add_span_processor``, so it was ignored
    and the global lookup used instead -- a provider the caller never chose."""
    with pytest.raises(TypeError):
        convergent.init(
            strict=True,
            api_key=_KEY,
            endpoint=_ENDPOINT,
            release="r1",
            tracer_provider=cast(Any, trace.NoOpTracerProvider()),
        )


def test_a_tracer_provider_that_is_not_a_provider_raises_type_error() -> None:
    with pytest.raises(TypeError):
        convergent.init(
            strict=True,
            api_key=_KEY,
            endpoint=_ENDPOINT,
            release="r1",
            tracer_provider=cast(Any, "the global one please"),
        )


# --------------------------------------------------------------------------
# init() raises: debug, and the exporter environment variable
# --------------------------------------------------------------------------


def test_a_non_boolean_debug_raises_type_error() -> None:
    with pytest.raises(TypeError):
        convergent.init(
            strict=True, api_key=_KEY, endpoint=_ENDPOINT, release="r1", debug=cast(Any, "yes")
        )


def test_a_debug_environment_value_outside_the_accepted_set_raises_value_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``CONVERGENT_DEBUG=maybe`` was read as false, so a caller who asked for debug
    logs got none and nothing said why."""
    monkeypatch.setenv("CONVERGENT_DEBUG", "maybe")

    with pytest.raises(ValueError):
        convergent.init(strict=True, api_key=_KEY, endpoint=_ENDPOINT, release="r1")


def test_an_unknown_traces_exporter_environment_value_raises_value_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``consle`` was ignored silently, so the console destination never appeared.
    OpenTelemetry raises on an unknown ``OTEL_TRACES_EXPORTER`` too."""
    monkeypatch.setenv("CONVERGENT_TRACES_EXPORTER", "consle")

    with pytest.raises(ValueError):
        convergent.init(strict=True, api_key=_KEY, endpoint=_ENDPOINT, release="r1")


# --------------------------------------------------------------------------
# Constructing a destination raises
# --------------------------------------------------------------------------


def test_a_file_name_that_climbs_out_of_its_directory_raises_value_error(tmp_path: Path) -> None:
    """``filename`` is joined with ``path``, so ``../spans.jsonl`` writes prompts
    somewhere other than the directory that was named."""
    with pytest.raises(ValueError):
        convergent.File(tmp_path, filename="../spans.jsonl")


def test_an_absolute_file_name_raises_value_error() -> None:
    """``Path.__truediv__`` drops its left side when the right is absolute, so an
    absolute name discards ``path`` entirely."""
    with pytest.raises(ValueError):
        convergent.File("/data/traces", filename="/abs/spans.jsonl")


def test_an_empty_file_name_raises_value_error(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        convergent.File(tmp_path, filename="")


def test_a_bare_file_name_is_accepted(tmp_path: Path) -> None:
    """The counterpart. Several processes sharing one directory is what the
    argument exists for."""
    assert convergent.File(tmp_path, filename="worker-1.jsonl").filename == "worker-1.jsonl"


def test_a_boolean_file_mode_raises_type_error(tmp_path: Path) -> None:
    with pytest.raises(TypeError):
        convergent.File(tmp_path, mode=cast(Any, True))


def test_a_non_integer_file_mode_raises_type_error(tmp_path: Path) -> None:
    """``"0600"`` is a string of digits, not permission bits, and ``fchmod`` would
    reject it after ``init()`` had reported a healthy setup."""
    with pytest.raises(TypeError):
        convergent.File(tmp_path, mode=cast(Any, "0600"))


def test_an_unknown_console_stream_raises_value_error() -> None:
    """The ``Literal`` on ``stream`` holds no runtime weight, and an unknown name
    used to fail at export time, after ``init()`` reported a healthy setup."""
    with pytest.raises(ValueError):
        convergent.Console(stream=cast(Any, "stdotu"))


def test_a_non_boolean_console_pretty_raises_type_error() -> None:
    with pytest.raises(TypeError):
        convergent.Console(pretty=cast(Any, "yes"))


# --------------------------------------------------------------------------
# A raising init() leaves the process exactly as it was
# --------------------------------------------------------------------------


def test_a_destinations_iterable_that_raises_mid_iteration_becomes_a_type_error() -> None:
    """A caller generator's own exception must not escape init() as itself; the
    raise set stays TypeError, ValueError, and OSError."""

    def exploding() -> Iterator[Any]:
        yield convergent.Console()
        raise RuntimeError("boom from the caller's iterable")

    with pytest.raises(TypeError):
        convergent.init(strict=True, release="r1", destinations=cast(Any, exploding()))


def test_an_agents_iterable_that_raises_mid_iteration_becomes_a_type_error() -> None:
    def exploding_names() -> Iterator[Any]:
        yield "support-agent"
        raise RuntimeError("boom from the caller's iterable")

    with pytest.raises(TypeError):
        convergent.init(
            strict=True,
            api_key=_KEY,
            endpoint=_ENDPOINT,
            release="r1",
            agents=cast(Any, exploding_names()),
        )


class _HostilePath:
    """A ``PathLike`` whose ``__fspath__`` raises, the way a cloud path does when it
    cannot reach the client it needs."""

    def __fspath__(self) -> str:
        raise RuntimeError("boom from the caller's __fspath__")


def test_a_path_whose_fspath_raises_is_rejected_by_default() -> None:
    """A caller object's own exception must not escape init(); resolving the path
    runs their ``__fspath__``, which can raise anything at all."""
    status = convergent.init(
        release="r1", destinations=[convergent.File(cast(Any, _HostilePath()))]
    )

    assert (status.enabled, status.reason) == (False, "invalid_config")
    assert _core._running_config() is None


def test_a_path_whose_fspath_raises_becomes_a_chained_type_error_under_strict() -> None:
    with pytest.raises(TypeError) as excinfo:
        convergent.init(
            strict=True, release="r1", destinations=[convergent.File(cast(Any, _HostilePath()))]
        )

    assert isinstance(excinfo.value.__cause__, RuntimeError)


def test_a_processor_rejects_a_validation_error_outside_the_documented_set(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Validation normalizes the lanes it knows about, so anything else is a bug in
    it. The processor still rejects rather than letting it reach the caller."""

    def explode(**_: object) -> Any:
        raise RuntimeError("boom from validation")

    monkeypatch.setattr(_config, "_processor_config", explode)

    with caplog.at_level(logging.ERROR, logger="convergent.sdk"):
        ConvergentSpanProcessor(api_key=_KEY, endpoint=_ENDPOINT, release="r1")

    assert _core.live_status().reason == "invalid_config"
    assert any(r.levelno == logging.ERROR for r in caplog.records)


def test_a_setup_failure_after_a_rejected_config_reports_the_setup_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The last startup attempt is the one check() reports. Reporting the earlier
    rejection would also drop the release and credentials check() needs."""
    rejected = convergent.init(api_key=_KEY, endpoint=_ENDPOINT, release="r1", agents=cast(Any, 1))
    assert (rejected.enabled, rejected.reason) == (False, "invalid_config")

    monkeypatch.setattr(
        _core, "_attach_processors", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    status = convergent.init(api_key=_KEY, endpoint=_ENDPOINT, release="r2")

    assert (status.enabled, status.reason, status.release) == (False, "setup_failed", "r2")
    credentials = _core.credentials()
    assert credentials is not None
    assert credentials.endpoint == _ENDPOINT


def test_a_processor_that_adopts_after_a_rejected_config_forgets_the_rejection() -> None:
    """Claiming the process settles the earlier rejection, so releasing the claim
    leaves a process that configured nothing rather than a stale rejection."""
    ConvergentSpanProcessor(release="r1")
    assert _core.live_status().reason == "invalid_config"

    processor = ConvergentSpanProcessor(api_key=_KEY, endpoint=_ENDPOINT, release="r2")
    assert _core.live_status().enabled is True

    processor._unadopt()
    assert _core.live_status().reason == "missing_config"


def test_a_file_mode_outside_permission_bits_raises_value_error(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        convergent.File(tmp_path, mode=0o4755)


def test_a_destination_that_fails_after_its_probe_never_takes_down_another(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A post-probe build failure is a race on this machine, so that one
    destination is skipped and the rest of the configuration still delivers."""

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise OSError("disk filled between probe and open")

    monkeypatch.setattr(_core, "OtlpFileSpanExporter", refuse)

    with caplog.at_level(logging.WARNING, logger="convergent.sdk"):
        status = convergent.init(
            api_key=_KEY,
            endpoint=_ENDPOINT,
            release="r1",
            destinations=[convergent.File(tmp_path)],
        )

    assert status.enabled, "the network destination survives the file's race"
    assert "convergent" in status.destinations
    assert any("could not open" in record.message for record in caplog.records)


def test_a_setup_failure_shuts_down_the_processors_it_built(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A failure after the destination processors are built must shut them down,
    or each one leaks a descriptor and an export thread."""
    shut_down: list[object] = []
    original = _transport.batch_processor

    def recording(exporter: Any) -> Any:
        processor = original(exporter)
        original_shutdown = processor.shutdown

        def shutdown() -> None:
            shut_down.append(processor)
            original_shutdown()

        monkeypatch.setattr(processor, "shutdown", shutdown)
        return processor

    monkeypatch.setattr(_transport, "batch_processor", recording)
    monkeypatch.setattr(
        _core, "_attach_processors", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    )

    status = convergent.init(release="r1", destinations=[convergent.File(tmp_path)])

    assert (status.enabled, status.reason) == (False, "setup_failed")
    assert shut_down, "the built file processor was never shut down"


def test_a_raising_init_leaves_the_module_unconfigured() -> None:
    """All raising validation runs before the lock is taken, before the
    registration POST, and before any processor is attached. No partial state
    exists that a later span could trip over."""
    with pytest.raises(TypeError):
        convergent.init(
            strict=True,
            api_key=_KEY,
            endpoint=_ENDPOINT,
            release="r1",
            agents=cast(Any, "checkout"),
        )

    assert _core.live_status().reason == "missing_config"
    assert _core._config is None
    assert _core._processors == []


def test_a_valid_init_after_a_raising_one_configures_the_process() -> None:
    """The corrected call works, which is only true if the failed one committed
    nothing."""
    with pytest.raises(TypeError):
        convergent.init(
            strict=True,
            api_key=_KEY,
            endpoint=_ENDPOINT,
            release="r1",
            agents=cast(Any, "checkout"),
        )

    status = convergent.init(
        strict=True, api_key=_KEY, endpoint=_ENDPOINT, release="r1", agents=["checkout"]
    )

    assert status.enabled is True
    assert status.agents == ["checkout"]


# --------------------------------------------------------------------------
# init() stays quiet
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reason",
    ["URLError", "http_401", "http_500"],
    ids=["receiver_unreachable", "revoked_key", "server_error"],
)
def test_a_failed_registration_does_not_raise(
    reason: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Network and server state. A revoked key or a receiver outage during a fleet
    restart must not stop the fleet from booting."""

    def refuse(*_: object, **__: object) -> dict[str, object]:
        raise _registry.RegistrationError(reason)

    monkeypatch.setattr(_registry, "post_json", refuse)

    with caplog.at_level(logging.WARNING, logger="convergent.sdk"):
        status = convergent.init(api_key=_KEY, endpoint=_ENDPOINT, release="r1")

    assert status.enabled is True
    assert status.deployment is None, "the release rides every trace as the fallback"
    assert [message for message in _sdk_warnings(caplog) if "registration failed" in message]


def test_a_second_init_with_a_bad_agents_value_does_not_raise(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A repeat ``init()`` is runtime: any library in the process may call it at any
    time, so the second call keeps the first configuration even when its own inputs
    are malformed. Only the claiming call may raise."""
    first = convergent.init(api_key=_KEY, endpoint=_ENDPOINT, release="r1")

    with caplog.at_level(logging.WARNING, logger="convergent.sdk"):
        second = convergent.init(agents=cast(Any, 12345))

    assert second.enabled is True
    assert second.release == first.release
    assert second.destinations == first.destinations
    assert _sdk_warnings(caplog), "the losing call still says its settings are not running"


def test_attaching_to_a_provider_another_sdk_installed_does_not_raise() -> None:
    """Environmental. Somebody else owning the global provider is not a bad local
    configuration."""
    trace.set_tracer_provider(TracerProvider())

    status = convergent.init(api_key=_KEY, endpoint=_ENDPOINT, release="r1")

    assert status.enabled is True
    assert status.mode == "attached"


def test_the_runtime_api_never_raises_after_init_returns(tmp_path: Path) -> None:
    """The OpenTelemetry MUST NOT rule. Everything after ``init()`` returns keeps
    the never-raise behavior, hostile arguments included."""
    convergent.init(release="r1", destinations=[convergent.File(tmp_path)])

    with convergent.span(name="support-agent", operation="agent_run") as run:
        run.set_input(cast(Any, object()))
        run.set_attribute("gen_ai.request.model", cast(Any, object()))

    assert convergent.flush(timeout_ms=5_000).ok is True
    assert convergent.tracer_provider() is not None
    assert _core.live_status().enabled is True


# --------------------------------------------------------------------------
# The own-your-provider path: same policy as init()
# --------------------------------------------------------------------------


def test_a_processor_with_no_api_key_raises_value_error() -> None:
    """``ConvergentSpanProcessor`` takes no ``destinations``, so credentials are the
    only configuration it has, and without them it sends nothing."""
    with pytest.raises(ValueError, match="CONVERGENT_API_KEY"):
        ConvergentSpanProcessor(strict=True, release="r1")


def test_a_processor_with_agents_as_a_string_raises_type_error() -> None:
    with pytest.raises(TypeError):
        ConvergentSpanProcessor(
            strict=True,
            api_key=_KEY,
            endpoint=_ENDPOINT,
            release="r1",
            agents=cast(Any, "checkout"),
        )


def test_install_with_something_that_is_not_a_provider_raises_type_error() -> None:
    with pytest.raises(TypeError):
        convergent.otel.install(
            cast(Any, "not a provider"), api_key=_KEY, release="r1", strict=True
        )


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permission bits")
def test_default_mode_disables_and_logs_instead_of_raising(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """With strict off, a configuration that cannot work fails closed: the exact
    problem is logged at ERROR, nothing is configured, and nothing is sent."""
    built: list[object] = []
    monkeypatch.setattr(_transport, "build_processor", lambda **k: built.append(k))

    with caplog.at_level(logging.ERROR, logger="convergent.sdk"):
        status = convergent.init(
            api_key=_KEY, endpoint=_ENDPOINT, release="r1", agents=cast(Any, 123)
        )

    assert (status.enabled, status.reason) == (False, "invalid_config")
    assert built == [], "an invalid configuration builds nothing"
    assert _core._running_config() is None
    record = next(r for r in caplog.records if r.levelno == logging.ERROR)
    assert "agents" in record.getMessage()
    assert "CONVERGENT_STRICT" in record.getMessage()

    with convergent.span(name="support-agent", operation="agent_run"):
        pass
    assert convergent.flush().ok is True, "runtime stays quiet after a rejected config"


def test_convergent_strict_env_turns_raising_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """The environment can enforce strict startup without a code change, and it
    wins even when the call site passes strict=False."""
    monkeypatch.setenv("CONVERGENT_STRICT", "1")

    with pytest.raises(TypeError):
        convergent.init(api_key=_KEY, endpoint=_ENDPOINT, release="r1", agents=cast(Any, 123))
    with pytest.raises(TypeError):
        convergent.init(
            api_key=_KEY, endpoint=_ENDPOINT, release="r1", agents=cast(Any, 123), strict=False
        )


def test_a_malformed_convergent_strict_value_disables_tracing(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A CONVERGENT_STRICT value outside the accepted set cannot silently mean
    off, so the otherwise valid configuration is rejected."""
    monkeypatch.setenv("CONVERGENT_STRICT", "maybe")

    with caplog.at_level(logging.ERROR, logger="convergent.sdk"):
        status = convergent.init(api_key=_KEY, endpoint=_ENDPOINT, release="r1")

    assert (status.enabled, status.reason) == (False, "invalid_config")
    assert any("CONVERGENT_STRICT" in r.getMessage() for r in caplog.records)


def test_a_valid_init_after_a_rejected_one_works() -> None:
    rejected = convergent.init(api_key=_KEY, endpoint=_ENDPOINT, release="r1", agents=cast(Any, 1))
    assert (rejected.enabled, rejected.reason) == (False, "invalid_config")

    status = convergent.init(api_key=_KEY, endpoint=_ENDPOINT, release="r1")
    assert status.enabled is True
    assert status.reason is None
    assert _core.live_status().reason is None, "the rejection is forgotten once a config commits"


def test_a_processor_with_bad_config_disarms_instead_of_raising_by_default(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    built: list[object] = []
    monkeypatch.setattr(_transport, "build_processor", lambda **k: built.append(k))

    with caplog.at_level(logging.ERROR, logger="convergent.sdk"):
        ConvergentSpanProcessor(release="r1")

    assert built == [], "a keyless processor builds nothing by default"
    assert _core.live_status().reason == "invalid_config"


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permission bits")
def test_default_mode_skips_an_unopenable_file_and_keeps_the_rest(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    blocked = tmp_path / "blocked"
    blocked.mkdir(mode=0o500)

    with caplog.at_level(logging.WARNING, logger="convergent.sdk"):
        status = convergent.init(
            api_key=_KEY,
            endpoint=_ENDPOINT,
            release="r1",
            destinations=[convergent.File(blocked / "spans")],
        )

    assert status.enabled is True, "the network destination survives"
    assert "convergent" in status.destinations
    assert any("could not open" in r.message for r in caplog.records)


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permission bits")
def test_default_mode_rejects_a_config_left_with_nothing_to_send_to(tmp_path: Path) -> None:
    blocked = tmp_path / "blocked"
    blocked.mkdir(mode=0o500)

    status = convergent.init(release="r1", destinations=[convergent.File(blocked / "spans")])

    assert (status.enabled, status.reason) == (False, "invalid_config")


def test_a_raising_file_probe_leaves_no_processor_behind(tmp_path: Path) -> None:
    """Two destinations, the second unopenable: nothing is committed, nothing is
    built for the first, and a corrected call afterwards works."""
    good = tmp_path / "good"
    blocked = tmp_path / "blocked"
    blocked.mkdir(mode=0o500)

    with pytest.raises(OSError):
        convergent.init(
            strict=True,
            release="r1",
            destinations=[convergent.File(good), convergent.File(blocked / "spans")],
        )

    assert _core._running_config() is None
    assert _core._processors == []
    assert not _core.live_status().enabled

    status = convergent.init(strict=True, release="r1", destinations=[convergent.File(good)])
    assert status.enabled
    convergent.flush()
    assert (good / SPANS_FILENAME).exists()


def _assert_key_not_in_frames(secret: str, error: BaseException) -> None:
    """Walk the traceback the way a locals-capturing crash reporter does."""
    tb = error.__traceback__
    while tb is not None:
        frame = tb.tb_frame
        if frame.f_code.co_filename != __file__:
            for name, value in frame.f_locals.items():
                assert secret not in repr(value), (
                    f"the api key appears in local {name!r} of {frame.f_code.co_qualname}"
                )
        tb = tb.tb_next


@pytest.mark.parametrize(
    "bad_kwargs",
    [
        {"agents": cast(Any, 123)},
        {"endpoint": "not a url"},
    ],
    ids=["raise_before_credential_resolution", "raise_after_credential_resolution"],
)
def test_a_raising_init_keeps_the_api_key_out_of_frame_locals(
    bad_kwargs: dict[str, Any],
) -> None:
    """A crash reporter that serializes locals must not find the key.

    The endpoint case is the load-bearing one: it raises after the key is
    resolved, so it fails if any frame binds the key as a plain string.
    """
    secret = "k-very-secret-value"  # pragma: allowlist secret
    kwargs: dict[str, Any] = {"api_key": secret, "endpoint": _ENDPOINT, "release": "r1"}
    kwargs.update(bad_kwargs)

    with pytest.raises((TypeError, ValueError)) as excinfo:
        convergent.init(strict=True, **kwargs)

    _assert_key_not_in_frames(secret, excinfo.value)


def test_a_repeat_init_keeps_the_api_key_out_of_frame_locals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The repeat lane never validates, so nothing there masks the key for it.

    A repeat ``init()`` returns rather than raises, so the traceback here comes
    from a warning that blew up underneath it.
    """
    convergent.init(api_key=_KEY, endpoint=_ENDPOINT, release="r1")
    secret = "k-very-secret-value"  # pragma: allowlist secret

    def explode(*_: object, **__: object) -> None:
        raise RuntimeError("boom from the warning path")

    monkeypatch.setattr(_core, "_warn_conflicting_config", explode)

    with pytest.raises(RuntimeError) as excinfo:
        convergent.init(api_key=secret, endpoint=_ENDPOINT, release="r2")

    _assert_key_not_in_frames(secret, excinfo.value)


def test_a_raising_processor_keeps_the_api_key_out_of_frame_locals() -> None:
    secret = "k-very-secret-value"  # pragma: allowlist secret

    with pytest.raises(ValueError) as excinfo:
        ConvergentSpanProcessor(strict=True, api_key=secret, endpoint="not a url", release="r1")

    _assert_key_not_in_frames(secret, excinfo.value)


def test_a_dropped_processor_keeps_the_api_key_out_of_frame_locals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same lane on the processor: another configuration already owns the
    process, so construction drops without validating anything."""
    convergent.init(api_key=_KEY, endpoint=_ENDPOINT, release="r1")
    secret = "k-very-secret-value"  # pragma: allowlist secret

    def explode(*_: object, **__: object) -> None:
        raise RuntimeError("boom from the warning path")

    monkeypatch.setattr(_core, "_warn_once", explode)

    with pytest.raises(RuntimeError) as excinfo:
        ConvergentSpanProcessor(api_key=secret, endpoint=_ENDPOINT, release="r2")

    _assert_key_not_in_frames(secret, excinfo.value)


def test_a_raising_install_keeps_the_api_key_out_of_frame_locals() -> None:
    secret = "k-very-secret-value"  # pragma: allowlist secret

    with pytest.raises(TypeError) as excinfo:
        convergent.otel.install(
            cast(Any, "not a provider"), api_key=secret, release="r1", strict=True
        )

    _assert_key_not_in_frames(secret, excinfo.value)


def test_a_processor_built_after_init_claimed_the_process_does_not_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once a configuration is committed, the startup entry points stop raising in
    that process: a library adding a processor to its own provider is runtime for
    the application that already configured tracing."""
    convergent.init(api_key=_KEY, endpoint=_ENDPOINT, release="r1")

    built: list[object] = []
    original = _transport.build_processor

    def recording(**kwargs: Any) -> object:
        processor = original(**kwargs)
        built.append(processor)
        return processor

    monkeypatch.setattr(_transport, "build_processor", recording)
    ConvergentSpanProcessor(agents=cast(Any, 12345))

    assert built == [], "a processor that lost the claim builds no exporter"
