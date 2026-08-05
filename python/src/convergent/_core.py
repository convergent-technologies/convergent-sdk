from __future__ import annotations

import atexit
import logging
import os
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Literal, NamedTuple

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanLimits, SpanProcessor, TracerProvider

from . import _egress, _registry, _semantic, _transport
from ._config import (
    _FLAG_ON,
    _clean,
    _Config,
    _masked_key,
    _probe_file,
    _strict_enabled,
    _target,
    _validated_config,
)
from ._console_export import ConsoleSpanExporter
from ._destinations import Destination, File
from ._file_export import OtlpFileSpanExporter
from ._semantic import SemanticSpanProcessor

logger = logging.getLogger("convergent.sdk")


@dataclass(frozen=True)
class Snapshot:
    provider: TracerProvider | None = None
    release: str | None = None
    # Resource attributes carrying the deployment identity init() registered.
    deployment: Mapping[str, str] = field(default_factory=dict)
    # False when we attached to a provider the caller already set, which means
    # its Resource is theirs and frozen, and its processors are theirs too.
    owns_provider: bool = False
    agents: tuple[str, ...] = ()


@dataclass(frozen=True)
class Status:
    """What ``init()`` configured."""

    enabled: bool
    deployment: str | None = None
    release: str | None = None
    #: The declared names the server confirmed it linked, falling back to what was
    #: declared when it did not say. This reports registration, not filtering. The
    #: filter always enforces the names this process declared, so a name the server
    #: dropped is still a name whose spans are sent.
    agents: list[str] = field(default_factory=list)
    destinations: list[str] = field(default_factory=list)
    mode: Literal["owned", "attached"] = "owned"
    app_url: str | None = None
    reason: str | None = None


class Credentials(NamedTuple):
    """Where a control-plane request goes, and the header that authorizes it.

    The header is built here rather than handed out as a bare key. A caller who
    unpacked two strings in the wrong order would otherwise send the endpoint as
    the bearer token.

    The header name is lowercase because gRPC requires lowercase metadata keys,
    and HTTP header names are case-insensitive, so ``headers`` works as-is for
    both an HTTP request and gRPC exporter metadata.
    """

    endpoint: str
    headers: dict[str, str]


@dataclass(frozen=True)
class FlushResult:
    """What a ``flush()`` actually did.

    ``flush()`` used to return a bare bool, which could not distinguish "drained
    everything" from "gave up with spans still queued" -- the case a short-lived
    process most needs to know about. ``__bool__`` keeps ``if flush():`` reading
    the way it always did.
    """

    ok: bool
    #: Spans still queued when the budget ran out. Best-effort: read from
    #: OpenTelemetry's own queue, which is private, so 0 can also mean unknown.
    pending: int
    #: Spans thrown away since the last ``flush()``: queue-full drops plus
    #: batches the receiver refused or the export never delivered.
    dropped: int
    elapsed_ms: int

    def __bool__(self) -> bool:
        return self.ok


_DRAIN_BUDGET_S = 5.0


#: Why startup last failed, and the configuration it was trying to run. A
#: rejected configuration never got far enough to have one. One variable rather
#: than one per reason, so the last attempt is what ``check()`` reports: a
#: rejected call followed by one that got as far as building would otherwise
#: report the rejection and lose the release the second call meant to reach.
_StartupFailure = tuple[Literal["invalid_config", "setup_failed"], _Config | None]

_lock = threading.RLock()
_state = Snapshot()
_config: _Config | None = None
_startup_failure: _StartupFailure | None = None
_dropped: TracerProvider | None = None
_processors: list[SpanProcessor] = []
#: Asked which provider carries the span processor a caller added themselves. Set
#: by :func:`_adopt`, and ``None`` for every other way of configuring tracing.
_provider_source: Callable[[], TracerProvider | None] | None = None
#: A start-registration callable for each configuration that was adopted through
#: a ConvergentSpanProcessor. ``flush()`` calls these before draining, so the
#: first flush also starts deployment registration.
_registration_triggers: dict[_Config, Callable[[], None]] = {}
_warned: set[str] = set()
_atexit_registered = False
_after_fork_registered = False
_finalize_registered = False


class _AfterForkSentinel:
    pass


_after_fork_sentinel = _AfterForkSentinel()


def init(
    *,
    api_key: str | None = None,
    endpoint: str | None = None,
    release: str | None = None,
    agents: list[str] | None = None,
    destinations: Sequence[Destination] = (),
    tracer_provider: TracerProvider | None = None,
    debug: bool = False,
    strict: bool = False,
) -> Status:
    """Configure tracing for this process and describe what was configured.

    Takes only process-level facts. Agent identity belongs on ``observe()``,
    which names each agent per run and supports several agents in one process --
    a single process-wide name could not. ``service.name`` is left to
    OpenTelemetry's own ``OTEL_SERVICE_NAME`` detection.

    ``agents`` names the agents Convergent is allowed to see. Name them and we send
    those agents' spans and everything that happens inside their runs, including
    tool calls and database queries. Anything outside an agent run stays in this
    process, and so does any span naming an agent you did not list. Leave it out
    and every span this process records is sent, which is what a process with no
    other OpenTelemetry setup usually wants. Use it when ``init()`` attaches to a
    provider you already own, because then every span your application produces
    reaches us. An agent whose work crosses two processes needs ``agents`` in each
    one, and the second process keeps the work arriving from the first when the
    span looks like model work.

    ``api_key`` implies the Convergent destination. ``endpoint`` overrides the
    managed ingest endpoint for local or private receivers. The same address
    registers the deployment and receives spans. ``destinations`` adds places on
    top of that, and every span goes to all of them:

        init(destinations=[convergent.File("/data/traces"), convergent.Console()])

    ``CONVERGENT_SPANS_DIR`` is shorthand for a single ``File``, kept because it is
    the common case: a sandbox with no route to the receiver, where the file is how
    the trace gets out and someone outside collects it. Credentials are not
    required for that, and without them nothing is sent over the network at all.

    ``tracer_provider`` is the provider to attach to, instead of looking one up. Our
    processors are added to it, its Resource is left alone, and the global provider
    is not set. Leave it out and ``init()`` uses the global provider when one exists
    and creates one when it does not.

    ``debug`` raises this SDK's own logger to DEBUG.

    ``strict`` decides what a configuration that cannot work does. Off, the
    default, ``init()`` logs the exact problem at ERROR, configures nothing, and
    returns ``Status(enabled=False, reason="invalid_config")``, so a telemetry
    mistake never stops a deployment and never sends anything either. On, the
    same problem raises, which is for the caller who wants a bad setup to stop
    the process at startup. ``CONVERGENT_STRICT`` turns it on from the
    environment, so a pipeline can enforce it without a code change.

    Returns a :class:`Status` for the configuration this process is running, which
    on a repeat call is the first one that won.

    Strict startup is the one place this SDK raises, and only for a problem that
    is local and deterministic: the same call fails the same way on every
    machine. Every check runs before the lock, the registration call, the
    processors, and any global, so a raise leaves the process as it was and a
    corrected call afterwards works. The one mark a raise can leave is the spans
    file itself, because proving a ``File`` destination can be opened creates it
    and any missing parent directories. Nothing after this returns raises, and
    neither does a repeat call: any library in the process may call ``init()`` at
    any time, so a second call keeps the first configuration even when its own
    arguments are bad. Two threads racing their first calls are both claiming
    calls, and the loser can raise.

    Raises:
        Only when ``strict`` is on, from the argument or ``CONVERGENT_STRICT``.
        TypeError: an argument has the wrong type -- ``api_key``, ``endpoint`` or
            ``release`` that is not a string, ``debug`` or ``strict`` that is not
            a bool, ``tracer_provider`` that is not an
            ``opentelemetry.sdk.trace`` provider, ``agents`` that is not a list
            of names, or a ``destinations`` entry that is not a ``File`` or a
            ``Console``.
        ValueError: an argument or ``CONVERGENT_*`` variable has a value that
            cannot work -- nothing configured at all, no release, an endpoint
            that is not an http or https URL, an empty or oversized agent name,
            more names than registration accepts, or an unreadable
            ``CONVERGENT_DEBUG``, ``CONVERGENT_STRICT``, or
            ``CONVERGENT_TRACES_EXPORTER``.
        OSError: a ``File`` destination's file cannot be opened.
    """
    # Rebound before anything else, so this frame's locals stay masked whatever
    # path the call takes out of here.
    api_key = _masked_key(api_key)
    if _running_config() is not None:
        return _repeat_init(
            api_key=api_key,
            endpoint=endpoint,
            release=release,
            agents=agents,
            destinations=destinations,
            tracer_provider=tracer_provider,
            debug=debug,
            strict=strict,
        )

    strict_on = _strict_enabled(strict)
    try:
        config = _validated_config(
            api_key=api_key,
            endpoint=endpoint,
            release=release,
            agents=agents,
            destinations=destinations,
            tracer_provider=tracer_provider,
            debug=debug,
            strict=strict,
        )
        config = _probed_config(config, strict_on)
    except Exception as error:
        if strict_on:
            raise
        _enable_debug(debug)
        return _reject_config(error)
    _enable_debug(debug)
    _warn_on_several_spans_files(config.destinations)
    if _claimed(config):
        return _not_ours(config)
    return _commit_config(config)


def _commit_config(config: _Config) -> Status:
    """Build ``config``'s destinations, register the deployment, and claim the process.

    Everything with a side effect, after validation has proved the configuration
    can work. A failure anywhere in here is ``setup_failed`` rather than a raise:
    the deterministic problems are already behind us, so what is left is the
    machine and the network.
    """
    started = False
    destination_processors: list[SpanProcessor] = []
    try:
        try:
            destination_processors = _build_destinations(config.destinations)
        except Exception:
            return _fail_setup(config)

        # Registration runs with no lock held. It is a blocking network call, so
        # holding _lock across it would stall every other SDK caller, and a fork
        # inside that window would leave the child holding a lock nobody releases.
        # With no credentials it makes no call at all -- see _register_deployment.
        deployment, confirmed = _register_deployment(
            api_key=config.api_key,
            endpoint=config.endpoint,
            release=config.release,
            agents=config.agents,
        )
        linked = tuple(confirmed) if confirmed is not None else (config.agents or ())

        with _lock:
            global _config, _startup_failure, _state
            if _config is not None:
                if _config != config:
                    _warn_conflicting_config(config, _config)
                    return _not_ours(config)
                return live_status()

            try:
                provider, owns_provider = _provider_for(config, deployment)
                processors = _attach_processors(
                    provider, config, deployment, destination_processors
                )
                if not processors:
                    return _fail_setup(config)
                if owns_provider:
                    trace.set_tracer_provider(provider)
            except Exception:
                return _fail_setup(config)
            _processors.extend(processors)
            _config = config
            _startup_failure = None
            _state = Snapshot(provider, config.release, deployment, owns_provider, linked)
            _ensure_process_drains()
            started = True
        return live_status()
    finally:
        # An uncommitted processor holds a descriptor and an export thread.
        if not started:
            _shut_down(destination_processors)


def _provider_for(config: _Config, deployment: Mapping[str, str]) -> tuple[TracerProvider, bool]:
    """The provider to attach to, and whether this process built it.

    A provider the caller handed over or already installed is theirs, so its
    Resource is left alone and the deployment identity rides the semantic
    processor instead.
    """
    given = config.tracer_provider
    current = trace.get_tracer_provider() if given is None else given
    if isinstance(current, TracerProvider):
        return current, False
    attributes: dict[str, str] = dict(deployment)
    attributes["service.version"] = config.release
    return TracerProvider(resource=Resource.create(attributes), span_limits=_span_limits()), True


def _probed_config(config: _Config, strict: bool) -> _Config:
    """``config`` with every spans file proved openable, or the survivors.

    Strict raises the ``OSError``. Otherwise the destination that will not open
    is dropped with a warning, and a configuration left with nothing at all to
    send to becomes a ``ValueError`` for the caller's reject path.
    """
    survivors: list[Destination] = []
    for destination in config.destinations:
        if isinstance(destination, File):
            try:
                _probe_file(destination)
            except OSError:
                if strict:
                    raise
                _warn_unopenable_file(Path(destination.path) / destination.filename)
                continue
        survivors.append(destination)
    if len(survivors) == len(config.destinations):
        return config
    if not config.api_key and not survivors:
        raise ValueError(
            "every configured destination failed to open and there is no api key, "
            "so there is nothing to send spans to"
        )
    return replace(config, destinations=tuple(survivors))


def _reject_config(error: Exception) -> Status:
    """Record and report a configuration that cannot work, without raising.

    The strict-off half of startup validation, shared by ``init()`` and
    ``ConvergentSpanProcessor``: the exact problem is logged at ERROR, nothing is
    configured, and nothing is sent, so a bad configuration fails closed instead
    of open.
    """
    global _startup_failure
    with _lock:
        _startup_failure = ("invalid_config", None)
    logger.error(
        "Convergent tracing is disabled because the configuration cannot work: %s "
        "-- strict mode is off, so the SDK logged this instead of raising. Set "
        "strict=True or CONVERGENT_STRICT=1 to stop the process at startup instead.",
        error,
    )
    return live_status()


def _enable_debug(debug: object) -> None:
    # is True, not truthiness: a repeat init() passes an unvalidated debug whose
    # __bool__ may raise.
    if debug is True or _clean(os.environ.get("CONVERGENT_DEBUG")).lower() in _FLAG_ON:
        logger.setLevel(logging.DEBUG)


def _repeat_init(
    *,
    api_key: object,
    endpoint: object,
    release: object,
    agents: object,
    destinations: object,
    tracer_provider: object,
    debug: object,
    strict: object,
) -> Status:
    """The status an ``init()`` call gets once another configuration owns the process.

    A repeat call is runtime rather than startup, so it warns and keeps the first
    configuration instead of raising. Arguments the claiming call would have
    rejected have no configuration to compare against, and the warning then names
    no dropped setting rather than raising to work one out.
    """
    _enable_debug(debug)
    try:
        config = _validated_config(
            api_key=api_key,
            endpoint=endpoint,
            release=release,
            agents=agents,
            destinations=destinations,
            tracer_provider=tracer_provider,
            debug=debug,
            strict=strict,
        )
    except Exception:
        config = None
    running = _running_config()
    if running is not None and running != config:
        _warn_conflicting_config(config, running)
    return _not_ours(config)


def live_status() -> Status:
    """What this process configured, as of now. Disabled until ``init()`` claims it."""
    # Through snapshot(), so check() looks for the caller's provider before it
    # reports on one rather than reporting on whatever a previous call found.
    state = snapshot()
    with _lock:
        config, failure = _config, _startup_failure
        unreachable = state.provider is None and _provider_source is not None
    if config is None:
        if failure is None:
            return Status(enabled=False, reason="missing_config")
        reason, failed = failure
        # check() sends this release, so the server reports on the deployment
        # this process meant to reach.
        return Status(
            enabled=False, reason=reason, release=failed.release if failed is not None else None
        )
    return Status(
        enabled=True,
        deployment=state.deployment.get("convergent.deployment.id"),
        release=state.release,
        agents=list(state.agents),
        destinations=_destinations(config),
        mode="owned" if state.owns_provider else "attached",
        # Spans the caller's own tracers record still reach us through their
        # processor, so this is enabled with one part of it not working.
        reason="no_provider" if unreachable else None,
    )


def _not_ours(config: _Config | None) -> Status:
    """What ``init()`` returns when another configuration already claimed the process.

    Tracing is on, so ``enabled`` stays true and the fields describe what is
    actually running. ``reason`` is what tells the caller the setup it asked for is
    not the one running, which no other field on ``Status`` can say: a caller whose
    settings lost would otherwise read a healthy status.
    """
    running = live_status()
    if running.enabled and _running_config() != config:
        return replace(running, reason="already_configured")
    return running


def credentials() -> Credentials | None:
    """Where spans are going and what authorizes a call there. Returns ``None`` when
    nothing goes over the network, which is a file-only process or one that never
    configured tracing.

    A process whose tracing could not start still has an endpoint and a key. Asking
    the server helps most in that state, because the key and the endpoint are right
    and nothing is arriving.
    """
    with _lock:
        config = _config or (_startup_failure[1] if _startup_failure is not None else None)
    if config is None or config.api_key is None or config.endpoint is None:
        return None
    return Credentials(config.endpoint, {"authorization": f"Bearer {config.api_key}"})


def status_and_credentials() -> tuple[Status, Credentials | None]:
    """Both halves of one report, from a single read of the state.

    If a caller read the two separately, a concurrent ``init()`` could land in between
    and produce a report that says tracing is disabled above a successful round trip.
    """
    with _lock:
        return live_status(), credentials()


def _record_setup_failure(config: _Config) -> None:
    """Remember a configuration that could not start, so ``check()`` can report it.

    Kept out of ``_config``, which the rest of the module reads as "a provider is
    running". ``live_status()`` and ``credentials()`` read this instead, so a setup
    failure does not report itself as a process that never configured anything.
    """
    global _startup_failure
    with _lock:
        _startup_failure = ("setup_failed", config)
    _warn_setup_failed()


def _fail_setup(config: _Config) -> Status:
    """Record that tracing could not start and return the matching status."""
    _record_setup_failure(config)
    return live_status()


def _destinations(config: _Config) -> list[str]:
    names: list[str] = []
    if config.api_key and config.endpoint:
        names.append("convergent")
    for destination in config.destinations:
        if isinstance(destination, File):
            names.append(f"file:{_target(destination)}")
        else:
            names.append(f"console:{destination.stream}")
    return names


def _warn_on_several_spans_files(resolved: Sequence[Destination]) -> None:
    """Warn when every span is being written to more than one file.

    Two names for one file are deduplicated above. Two different files cannot be. A
    ``File`` destination adds to ``CONVERGENT_SPANS_DIR`` rather than replacing it,
    so a process reaches this without anyone asking for a second copy of every span.
    """
    paths = [str(_target(d)) for d in resolved if isinstance(d, File)]
    if len(paths) < 2:
        return
    _warn_once(
        "several_spans_files",
        "Convergent is writing every span to more than one file "
        f"({', '.join(paths)}); a File destination adds to CONVERGENT_SPANS_DIR "
        "rather than replacing it",
    )


def _build_destinations(destinations: Sequence[Destination]) -> list[SpanProcessor]:
    """Every destination that starts, as span processors, in the order given.

    A destination that will not start is skipped with a warning inside
    :func:`_build_destination`. Anything that still escapes (an OpenTelemetry
    misconfiguration in the batch processor) shuts down the processors already
    built and becomes ``setup_failed`` in the caller, never a raise.
    """
    built: list[SpanProcessor] = []
    try:
        for destination in destinations:
            processor = _build_destination(destination)
            if processor is not None:
                built.append(processor)
    except BaseException:
        _shut_down(built)
        raise
    return built


def _shut_down(processors: Iterable[SpanProcessor]) -> None:
    for processor in processors:
        try:
            processor.shutdown()
        except Exception:
            pass


def _build_destination(destination: Destination) -> SpanProcessor | None:
    """One destination as a span processor, or ``None`` when it will not start.

    Validation already settled the deterministic failures: :func:`_probe_file`
    proved the file openable, so a failure here is a race on this machine (a full
    disk, a replaced directory) or a stream someone swapped out. Those are
    environmental, so one destination that will not start is skipped with a
    warning and never takes down another.
    """
    if isinstance(destination, File):
        target = Path(destination.path) / destination.filename
        try:
            exporter = OtlpFileSpanExporter(target, mode=destination.mode)
        except Exception:
            _warn_unopenable_file(target)
            return None
        try:
            return _transport.batch_processor(exporter)
        except BaseException:
            exporter.shutdown()
            raise
    try:
        return _transport.batch_processor(
            ConsoleSpanExporter(destination.stream, pretty=destination.pretty)
        )
    except Exception:
        _warn_once(
            f"console:{destination.stream}",
            f"Convergent could not start its {destination.stream} destination",
        )
        return None


def _attach_processors(
    provider: TracerProvider,
    config: _Config,
    deployment: Mapping[str, str],
    built: Sequence[SpanProcessor],
) -> list[SpanProcessor]:
    """Give ``provider`` its semantic processor, the built destinations, and the network.

    Semantic first, exporters second, so a span carries its identity before
    anything ships it. ``deployment`` goes to the semantic processor because an
    attached provider's Resource is the caller's and cannot carry it.

    ``built`` holds the destination processors :func:`_build_destinations` already
    made, because a destination that will not start settles the configuration and
    has to settle it before anything here is committed.

    The returned list is what ``flush()`` and the exit drain iterate, and it holds
    the exporters themselves even when a filter is in front of them on the
    provider. Those two read OpenTelemetry's own queue state, which lives on an
    exporter and not on the filter wrapping it, so handing them the filter would
    make every queue reading come back zero.

    Returns ``[]`` when nothing started, so the caller decides what that means
    rather than ending up with a provider that records spans and drops them
    silently -- the failure mode this whole feature exists to avoid.
    """
    exporters: list[SpanProcessor] = list(built)
    if config.api_key and config.endpoint:
        try:
            exporters.append(
                _transport.build_processor(api_key=config.api_key, endpoint=config.endpoint)
            )
        except Exception:
            _warn_once("otlp_exporter", "Convergent could not start its network exporter")
    if not exporters:
        return []

    semantic = SemanticSpanProcessor(config.release, deployment)
    # One filter, so one table of kept span ids serves every span in the process.
    destinations: list[SpanProcessor] = (
        [_egress.DeclaredAgentFilter(config.agents, exporters)]
        if config.agents is not None
        else exporters
    )
    for processor in [semantic, *destinations]:
        provider.add_span_processor(processor)
    return [semantic, *exporters]


def flush(timeout_ms: int = 5_000) -> FlushResult:
    """Drain buffered spans now. Never raises.

    Call this in any process that might be killed rather than exit normally --
    a Lambda between invocations, a recycled worker -- or the spans since the last
    export are lost.

    A span that has not ended yet is not in what this drains, so a ``flush()``
    written at the end of an ``observe()`` body misses that run's own span. One
    warning per process says so when it happens.

    ``ok`` answers "did every flush succeed and deliver what it drained". It
    deliberately does not also require an empty queue:
    ``pending`` is sampled after the last ``force_flush`` returns, so in a live
    process any span produced *during* the flush is already queued again. Folding
    that into ``ok`` reported failure for a flush that worked, and made a healthy
    service calling ``flush()`` per request log a warning per request. A
    destination that cannot write, such as a ``File`` on a disk that filled up,
    turns ``ok`` off the same way a refused network export does.

    ``pending`` is the informational half, and it is what a short-lived process
    should read before it exits. ``dropped`` counts the spans thrown away since
    the last call, both by a full queue and by an export that failed or was
    refused, neither of which any ``force_flush`` result reports.

    The loss count is taken and reset once per process, so two callers flushing at
    the same time split the signal: exactly one of them sees a given loss, in its
    ``ok`` and in its ``dropped``.
    """
    started = time.monotonic()
    with _lock:
        processors = tuple(_processors)
        triggers = tuple(_registration_triggers.values())
    for trigger in triggers:
        trigger()
    if not processors:
        return FlushResult(ok=True, pending=0, dropped=0, elapsed_ms=0)

    _warn_flush_inside_span()
    deadline = started + max(timeout_ms, 0) / 1_000
    ok = True
    for processor in processors:
        remaining_ms = max(0, int((deadline - time.monotonic()) * 1_000))
        try:
            if _transport.is_shut_down(processor):
                continue
            ok = processor.force_flush(remaining_ms) and ok
        except Exception:
            logger.warning("Convergent could not flush all pending traces")
            ok = False

    pending = sum(_transport.pending_spans(processor) for processor in processors)
    lost = _transport.lost_spans()
    ok = ok and lost == 0
    dropped = _transport.dropped_spans() + lost
    elapsed_ms = int((time.monotonic() - started) * 1_000)
    # Only when the flush actually failed *and* something is still queued. Spans
    # left behind by a successful flush are a busy process, not a problem.
    if not ok and pending:
        logger.warning(
            "Convergent flushed for %dms and left %d span(s) queued; "
            "OTEL_EXPORTER_OTLP_TIMEOUT bounds how long one export may take",
            elapsed_ms,
            pending,
        )
    return FlushResult(ok=ok, pending=pending, dropped=dropped, elapsed_ms=elapsed_ms)


def snapshot() -> Snapshot:
    global _state
    with _lock:
        state = _state
        # A span processor is never told which provider holds it, so a caller who
        # used add_span_processor is asked here instead, on each read until it
        # answers. They may add the processor before they install the provider.
        source = _provider_source if state.provider is None else None
        if source is not None:
            found = source()
            if found is not None:
                _state = state = replace(state, provider=found)
                source = None
    if source is not None:
        _warn_no_provider()
    return state


def active_provider() -> TracerProvider | None:
    return snapshot().provider


def tracer_provider() -> TracerProvider:
    """The provider this process is configured with, for using OpenTelemetry directly.

    Add your own processors, samplers, and spans to it. This never returns ``None``.
    With tracing off you get a provider that records nothing, so your code needs no
    check for ``None`` around it.

    Call ``init()`` first. Until it has configured a destination there is nothing to
    hand back, so a processor added here receives no span, and one warning says so.
    """
    global _dropped
    provider = active_provider()
    if provider is not None:
        return provider
    _warn_once(
        "tracer_provider_disabled",
        "Convergent tracing is not configured, so tracer_provider() returned a provider "
        "that records nothing; call init() before adding your own processors",
    )
    # One instance, because every TracerProvider registers its own atexit shutdown.
    with _lock:
        if _dropped is None:
            _dropped = TracerProvider()
        return _dropped


def _claimed(config: _Config) -> bool:
    """True when another init() already configured this process, warning unless
    the repeat call carries the same configuration."""
    with _lock:
        existing = _config
    if existing is None:
        return False
    if existing != config:
        _warn_conflicting_config(config, existing)
    return True


def _running_config() -> _Config | None:
    """The configuration currently running, or ``None`` when none has claimed."""
    with _lock:
        return _config


def _adopt(
    config: _Config,
    deployment: Mapping[str, str],
    exporters: Sequence[SpanProcessor],
    provider_source: Callable[[], TracerProvider | None],
    start_registration: Callable[[], None] | None = None,
) -> _Config | None:
    """Claim this process for a processor the caller added to their own provider.

    Returns ``None`` once ``config`` is the configuration this process runs, or the
    configuration already running when something else claimed it first. The claim
    is settled before anything is recorded, so a processor that lost leaves no
    exporter behind in the drain and its caller can shut down the one it built.

    ``flush()`` and the exit drain iterate ``exporters``, which are the network
    processors themselves rather than any filter in front of them, for the reason
    :func:`_attach_processors` gives.

    ``provider_source`` answers which provider ``span()`` and ``observe()`` record
    through. ``config.tracer_provider`` already answers it for a caller who handed
    one over, and :func:`snapshot` asks the source for everyone else.
    """
    global _config, _provider_source, _startup_failure, _state
    with _lock:
        if _config is not None:
            return _config
        _processors.extend(exporters)
        _config = config
        _startup_failure = None
        _provider_source = provider_source
        if start_registration is not None:
            _registration_triggers[config] = start_registration
        _state = Snapshot(
            provider=config.tracer_provider,
            release=config.release,
            deployment=dict(deployment),
            agents=config.agents or (),
        )
        _ensure_process_drains()
    return None


def _abandon(config: _Config, exporters: Sequence[SpanProcessor]) -> None:
    """Release the claim when the caller could not attach the processor.

    ``install()`` claims the process and then asks the provider to carry the
    processor. If that call raises, the provider never carried it, so the claim
    is rolled back and the exporter is removed from the drain list.
    """
    global _config, _provider_source, _state, _processors, _registration_triggers
    with _lock:
        if _config == config:
            _config = None
            _provider_source = None
            _registration_triggers.pop(config, None)
            _state = Snapshot()
        for processor in exporters:
            try:
                _processors.remove(processor)
            except ValueError:
                pass


def _link_deployment(deployment: Mapping[str, str], linked: Sequence[str] | None) -> None:
    """Record what registration answered, so ``check()`` names the deployment."""
    global _state
    with _lock:
        _state = replace(
            _state,
            deployment=dict(deployment),
            agents=tuple(linked) if linked is not None else _state.agents,
        )


def _register_deployment(
    *,
    api_key: str | None,
    endpoint: str | None,
    release: str,
    agents: tuple[str, ...] | None,
) -> tuple[dict[str, str], list[str] | None]:
    """Register the deployment and return its identity and its linked agents.

    The first half is the Resource attributes that carry the deployment's
    identity. The second is the agent names the server says it linked, or ``None``
    when it did not answer that question. That is different from answering that it
    linked none.

    Never raises. On any failure the release still rides every trace as
    ``convergent.deployment.fingerprint``, which the server resolves against the
    same ``(organization_id, fingerprint)`` key registration upserts on. A
    file-only process takes that same fallback without making a request: there
    is no endpoint to reach, and whoever ingests the file resolves the
    fingerprint the same way a live receiver would.
    """
    if not api_key or not endpoint:
        return {"convergent.deployment.fingerprint": release}, None

    body: dict[str, object] = {
        "fingerprint": release,
        "fingerprint_kind": "provided",
        "sdk_version": _sdk_version(),
    }
    if agents is not None:
        body["agents"] = list(agents)
    # Display metadata only, and only when the caller set the standard OTel var --
    # never OTel's "unknown_service" placeholder.
    service_name = _clean(os.environ.get("OTEL_SERVICE_NAME"))
    if service_name:
        body["service_name"] = service_name

    try:
        result = _registry.post_json(
            f"{endpoint.rstrip('/')}/v1/deployments",
            body,
            headers={"Authorization": f"Bearer {api_key}"},
        )
        deployment_id = result.get("deployment_id")
        if isinstance(deployment_id, str) and deployment_id:
            return {"convergent.deployment.id": deployment_id}, _linked_agents(result)
        reason = "no_deployment_id"
        unexpected = False
    except _registry.RegistrationError as exc:
        reason = exc.reason
        unexpected = False
    except Exception:
        # post_json promises to raise only RegistrationError, so anything here is
        # a bug in it -- keep the traceback rather than flattening it to a label.
        reason = "unexpected"
        unexpected = True

    logger.warning(
        "Convergent deployment registration failed; traces will carry the release "
        "fingerprint for the server to resolve",
        extra={"reason": reason},
        exc_info=unexpected,
    )
    return {"convergent.deployment.fingerprint": release}, None


def _linked_agents(result: Mapping[str, object]) -> list[str] | None:
    names = result.get("agents")
    if isinstance(names, list) and all(isinstance(name, str) for name in names):
        return list(names)
    return None


def _sdk_version() -> str:
    # Two names because this module can ship under either distribution name.
    for distribution in ("convergent-sdk", "convergent"):
        try:
            return version(distribution)
        except PackageNotFoundError:
            continue
    return "0.0.0"


def _warn_once(key: str, message: str) -> None:
    with _lock:
        if key in _warned:
            return
        _warned.add(key)
    logger.warning(message)


def _dropped_settings(config: _Config | None, running: _Config) -> str:
    """Name what a losing configuration asked for and did not get.

    A discarded ``agents`` setting is a discarded privacy control, so it is
    named rather than left for the reader to work out from ``Status``. Shared
    by both losing paths: a second ``init()`` and a ``ConvergentSpanProcessor``
    that lost the claim.

    ``None`` is a call whose own arguments could not be read as a configuration at
    all, which names nothing.
    """
    if config is None:
        return ""
    lost: list[str] = []
    if config.agents is not None and config.agents != running.agents:
        lost.append("the agents it declared")
    if (config.api_key, config.endpoint) != (running.api_key, running.endpoint):
        lost.append("a different api key or endpoint")
    return f" It dropped {' and '.join(lost)}." if lost else ""


def _warn_conflicting_config(config: _Config | None, running: _Config) -> None:
    # Keyed by the losing configuration so a second caller asking for something
    # different still hears about it, rather than being silenced by the first.
    _warn_once(
        f"conflicting_config:{hash(config)}",
        "Convergent tracing is already configured, so this init() call kept the first "
        f"setup and its own settings are not running.{_dropped_settings(config, running)} "
        "Configure tracing once, with init() or with one processor.",
    )


def _warn_no_provider() -> None:
    _warn_once(
        "no_provider",
        "Convergent cannot find the tracer provider your ConvergentSpanProcessor was "
        "added to, so span() and observe() are recording nothing. Spans your own "
        "tracers record still reach us. Pass your provider to "
        "convergent.otel.install(), or install it with trace.set_tracer_provider().",
    )


def _warn_unopenable_file(target: Path) -> None:
    _warn_once(
        f"spans_file:{target}",
        f"Convergent could not open {target}; spans are not written there",
    )


def _warn_setup_failed() -> None:
    _warn_once("setup_failed", "Convergent tracing could not start and is disabled")


def _warn_flush_inside_span() -> None:
    """Warn when this context still has a Convergent span open.

    Interpreter exit drains through :func:`_drain_all_processors` rather than
    ``flush()``, and a forked child forgets the count it inherited, so neither of the
    two ways a correct program reaches this emits the warning.

    A long-running agent may drain its model and tool spans on purpose while its own
    ``agent_run`` span is still open, so the message names the expectation that would
    be wrong instead of telling the caller to move the call.
    """
    if _semantic.has_open_span():
        _warn_once(
            "flush_inside_span",
            "Convergent flushed inside a traced function, so the enclosing span has "
            "not ended and is not in this flush. If you expected this run's own span "
            "in it, flush after the function that observe() or span() wraps returns.",
        )


def _ensure_process_drains() -> None:
    global _after_fork_registered, _atexit_registered, _finalize_registered
    if not _atexit_registered:
        atexit.register(_drain_all_processors)
        _atexit_registered = True

    try:
        import multiprocessing.util as mp_util

        if not _after_fork_registered:
            mp_util.register_after_fork(_after_fork_sentinel, _register_child_drain)
            _after_fork_registered = True
        if not _finalize_registered:
            mp_util.Finalize(None, _drain_all_processors, exitpriority=10)
            _finalize_registered = True
    except Exception:
        logger.warning("Convergent could not install its worker-process trace drain")


def _register_child_drain(_: object) -> None:
    try:
        import multiprocessing.util as mp_util

        mp_util.Finalize(None, _drain_all_processors, exitpriority=10)
    except Exception:
        pass


def _reset_lock_after_fork() -> None:
    """Replace _lock in a forked child.

    fork() copies memory but only the calling thread, so a lock another thread
    held at fork time arrives locked in the child with nobody left to release it
    -- the next snapshot() or flush() would block forever. Registered
    unconditionally at import: init()'s registration POST is the widest window,
    but any lock holder at the wrong moment is enough.
    """
    global _lock
    _lock = threading.RLock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_lock_after_fork)


def _drain_all_processors() -> None:
    # One budget for the whole drain, not 5s each: this runs at interpreter exit,
    # and a per-processor timeout would let a hung receiver hold exit for 5s per
    # destination.
    deadline = time.monotonic() + _DRAIN_BUDGET_S
    for processor in list(_processors):
        remaining_ms = max(0, int((deadline - time.monotonic()) * 1_000))
        try:
            processor.force_flush(remaining_ms)
            processor.shutdown()
        except Exception:
            pass


def _span_limits() -> SpanLimits:
    """OpenTelemetry's own limits, and no content cap of ours.

    This capped one attribute at 32 KiB so a runaway value could not push a whole
    export past a body limit. The intent was right and the mechanism was not.
    OpenTelemetry enforces an attribute cap by cutting the *finished* string, and
    a ``gen_ai.*.messages`` value is JSON, so the cut lands mid-token and the
    reader loses all of the content rather than the overflow. Large content is
    common on real traffic, so that loss was not a rare case.

    The limit that actually exists is on the request, not on one value, and it is
    enforced where requests are made: compression and the receiver's own body
    limit. Content too large to keep on a span is moved to the blob store at
    ingest and replaced by a reference.

    ``OTEL_ATTRIBUTE_VALUE_LENGTH_LIMIT`` still works for a caller who wants a
    cap, because ``SpanLimits()`` reads it.
    """
    return SpanLimits()


def _reset_for_tests() -> None:
    global _config, _dropped, _provider_source, _startup_failure, _state, _registration_triggers
    with _lock:
        _shut_down(_processors)
        _transport.dropped_spans()
        _transport.lost_spans()
        _state = Snapshot()
        _config = None
        _startup_failure = None
        _dropped = None
        _provider_source = None
        _processors.clear()
        _registration_triggers.clear()
        _warned.clear()
        _semantic._reported.clear()
        _semantic.forget_open_spans()
        logger.setLevel(logging.NOTSET)


__all__ = [
    "Credentials",
    "Snapshot",
    "Status",
    "_reset_for_tests",
    "active_provider",
    "credentials",
    "flush",
    "init",
    "live_status",
    "snapshot",
    "status_and_credentials",
    "tracer_provider",
]
