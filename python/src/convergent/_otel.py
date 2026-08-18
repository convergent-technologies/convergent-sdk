"""Everything the SDK does to a span, behind one OpenTelemetry span processor.

``init()`` creates a tracer provider, or attaches to the one it finds or the one
it was handed, and wires the semantic stamps, the context stamper, the egress
filters, and the network exporter onto it. A caller who builds a provider and
wants no ``init()`` at all adds this processor instead and gets the same pieces
in the same order.
"""

from __future__ import annotations

import logging
import os
import threading
import weakref
from collections.abc import Mapping

from opentelemetry import trace
from opentelemetry.context import (
    _SUPPRESS_INSTRUMENTATION_KEY,
    Context,
    attach,
    detach,
    set_value,
)
from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor, TracerProvider

from . import _config, _core, _processors, _transport
from ._semantic import SemanticSpanProcessor

logger = logging.getLogger("convergent.sdk")

#: The suppression key that predates the per-process one in ``opentelemetry.context``.
#: ``is_instrumentation_enabled`` reads both, so a request that must record no span
#: sets both. Written out rather than imported, because it lives in the
#: instrumentation package and this SDK depends on the API and the SDK only.
_SUPPRESS_INSTRUMENTATION_KEY_PLAIN = "suppress_instrumentation"

#: Every live processor, so a fork can repair the state its child inherited. Weak
#: so a processor a test threw away does not stay alive here.
_instances: weakref.WeakSet[ConvergentSpanProcessor] = weakref.WeakSet()


def install(
    provider: TracerProvider,
    *,
    api_key: str | None = None,
    endpoint: str | None = None,
    release: str | None = None,
    agents: list[str] | None = None,
    require_span_attributes: Mapping[str, object] | None = None,
    reject_span_attributes: Mapping[str, object] | None = None,
    strict: bool = False,
) -> ConvergentSpanProcessor:
    """Add Convergent to ``provider``, and record ``span()`` and ``observe()``
    through it.

        provider = TracerProvider()
        convergent.otel.install(provider, release="1.4.0")

    This is the way to use Convergent with no ``init()`` call at all. Handing the
    provider over is what makes ``span()`` and ``observe()`` work: a span processor
    is never told which provider holds it, so one added with
    ``add_span_processor`` alone has to find it, and it can only find one you
    installed globally.

    An API key uses ``https://ingest.convergent.dev`` unless ``endpoint`` or
    ``CONVERGENT_ENDPOINT`` supplies another receiver.

    Returns the processor, which is already on ``provider``. ``strict`` works the
    way ``init()``'s does: off, a configuration that cannot work is logged at
    ERROR and the processor sends nothing; on (argument or ``CONVERGENT_STRICT``),
    it raises. ``provider`` is the exception and always raises when it is not a
    provider: there is nothing to install into, and nothing to degrade to. A
    provider that will not carry the processor is a different matter, and that
    one is logged.

    ``agents``, ``require_span_attributes``, and ``reject_span_attributes``
    filter what is sent the way ``init()``'s do.

    Raises:
        TypeError: ``provider`` is not an ``opentelemetry.sdk.trace.TracerProvider``
            (always), or an argument has the wrong type (strict only).
        ValueError: strict only -- no api key or no release is configured, or an
            argument's value cannot work.
    """
    api_key = _config._masked_key(api_key)
    _config._require_provider("provider", provider, required=True)
    processor = ConvergentSpanProcessor(
        api_key=api_key,
        endpoint=endpoint,
        release=release,
        agents=agents,
        require_span_attributes=require_span_attributes,
        reject_span_attributes=reject_span_attributes,
        tracer_provider=provider,
        strict=strict,
    )
    try:
        provider.add_span_processor(processor)
    except Exception:
        config = processor._config
        if config is not None:
            _core._record_setup_failure(config)
            # Shuts the destinations down itself, so this path needs no second call.
            processor._unadopt()
        else:
            processor.shutdown()
    return processor


class ConvergentSpanProcessor(SpanProcessor):
    """Convergent as one processor, for a tracer provider you own.

    :func:`install` is the shorter way to reach this. Build it yourself when you
    want to place it in your pipeline by hand:

        provider.add_span_processor(ConvergentSpanProcessor(release="1.4.0"))

    The arguments are ``init()``'s, without ``destinations``, because the provider
    and everything else on it are yours. ``api_key``, ``endpoint``, ``release``,
    ``require_span_attributes``, ``reject_span_attributes``, and ``strict``
    fall back to the environment variable ``init()`` reads; ``agents`` and
    ``tracer_provider`` have none. The endpoint then falls back to the managed
    ingest endpoint when an API key is present.

    Construction makes no network call, and no span ever waits for one. The first
    span starts the deployment registration on a thread of its own. Until it
    lands, spans carry your ``release`` as ``convergent.deployment.fingerprint``,
    which the server resolves against the same key registration upserts on. After
    it lands, spans carry ``convergent.deployment.id``.

    ``tracer_provider`` is the provider you are adding this to, and it is what
    ``span()`` and ``observe()`` record through. Leave it out and they use the
    provider you installed with ``trace.set_tracer_provider``, once that provider
    is carrying this processor. They record nothing while neither is true, and one
    warning says so.

    Configure tracing once. A processor added to a process that ``init()`` or
    another processor already configured shuts its own exporter down and sends
    nothing, and one warning names any setting that went with it. That call cannot
    raise, whatever its arguments say, because the configuration it would have
    described is not the one running.

    Construction is startup, so bad arguments raise here, on your own thread,
    before anything is built. Everything after it degrades instead: a failed
    registration and an exporter that will not start are each logged once, and your
    application keeps running.

    Raises:
        Only when ``strict`` is on, from the argument or ``CONVERGENT_STRICT``.
        TypeError: ``api_key``, ``endpoint`` or ``release`` is not a string,
            ``agents`` is not a list of names, ``require_span_attributes`` or
            ``reject_span_attributes`` is not a mapping of attribute names to
            values, or ``tracer_provider`` is not an
            ``opentelemetry.sdk.trace.TracerProvider``.
        ValueError: no api key or no release is configured, an agent name is
            empty or oversized, there are more names than registration accepts,
            the ``require_span_attributes`` or ``reject_span_attributes``
            mapping or an attribute name in it is empty, a
            ``require_span_attributes`` or ``reject_span_attributes`` value is
            ``None`` -- pass an empty list to match nothing -- a
            ``CONVERGENT_REQUIRE_SPAN_ATTRIBUTES`` or
            ``CONVERGENT_REJECT_SPAN_ATTRIBUTES`` that is not JSON, or the
            endpoint is not an http or https URL.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        endpoint: str | None = None,
        release: str | None = None,
        agents: list[str] | None = None,
        require_span_attributes: Mapping[str, object] | None = None,
        reject_span_attributes: Mapping[str, object] | None = None,
        tracer_provider: TracerProvider | None = None,
        strict: bool = False,
    ) -> None:
        self._gate = threading.Lock()
        self._registration_started = False
        self._registered = False
        self._registration: threading.Thread | None = None
        #: The stamping processor, and the flag for whether this one is armed. It
        #: stays ``None`` for a processor with no credentials and for one that lost
        #: the claim on this process.
        self._semantic: SemanticSpanProcessor | None = None
        self._exporter: SpanProcessor | None = None
        self._destinations: tuple[SpanProcessor, ...] = ()
        self._config: _config._Config | None = None

        # Rebound before anything else, so this frame's locals stay masked
        # whatever path the call takes out of here.
        api_key = _config._masked_key(api_key)
        _instances.add(self)
        running = _core._running_config()
        if running is not None:
            asked = _asked_for(
                api_key,
                endpoint,
                release,
                agents,
                require_span_attributes,
                reject_span_attributes,
                tracer_provider,
            )
            self._drop(asked, running, None)
            return
        try:
            config = _config._processor_config(
                api_key=api_key,
                endpoint=endpoint,
                release=release,
                agents=agents,
                require_span_attributes=require_span_attributes,
                reject_span_attributes=reject_span_attributes,
                tracer_provider=tracer_provider,
                strict=strict,
            )
        except Exception as error:
            if _config._strict_enabled(strict):
                raise
            _core._reject_config(error)
            return
        try:
            self._build(config)
        except Exception:
            _core._warn_setup_failed()

    def on_start(self, span: Span, parent_context: Context | None = None) -> None:
        try:
            semantic = self._semantic
            if semantic is None:
                return
            self._start_registration()
            semantic.on_start(span, parent_context)
            _processors._STAMPER.on_start(span, parent_context)
            for destination in self._destinations:
                destination.on_start(span, parent_context)
        except Exception:
            _core._warn_once("otel_on_start", "Convergent could not record a span that started")

    def on_end(self, span: ReadableSpan) -> None:
        try:
            for destination in self._destinations:
                destination.on_end(span)
        except Exception:
            _core._warn_once("otel_on_end", "Convergent could not record a span that ended")

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        self._start_registration()
        try:
            # Materialized first: all() would stop flushing after the first False.
            flushed = [
                destination.force_flush(timeout_millis) for destination in self._destinations
            ]
            return all(flushed)
        except Exception:
            logger.warning("Convergent could not flush all pending traces")
            return False

    def shutdown(self) -> None:
        for destination in self._destinations:
            try:
                destination.shutdown()
            except Exception:
                _core._warn_once("otel_shutdown", "Convergent could not shut a destination down")

    def _build(self, config: _config._Config) -> None:
        api_key, endpoint = config.api_key, config.endpoint
        if api_key is None or endpoint is None:
            raise RuntimeError("a processor config always carries credentials")

        running = _core._running_config()
        if running is not None:
            self._drop(config, running, None)
            return

        try:
            exporter = _transport.build_processor(api_key=api_key, endpoint=endpoint)
        except Exception:
            _core._record_setup_failure(config)
            _core._warn_once("otlp_exporter", "Convergent could not start its network exporter")
            return

        self._exporter = exporter
        deployment = _fingerprint(config.release)
        running = _core._adopt(
            config, deployment, [exporter], self._find_provider, self._start_registration
        )
        if running is not None:
            self._drop(config, running, exporter)
            return
        # The filters sit in front of the exporter, and the exporter itself is
        # handed to _core, for the reason _attach_processors gives: flush() reads
        # a queue that lives on the exporter rather than on a filter wrapping it.
        self._destinations = _processors.wrap(config.policy, config.agents, (exporter,))
        self._config = config
        self._semantic = SemanticSpanProcessor(config.release, deployment)

    def _unadopt(self) -> None:
        """Release the claim if this processor is the one that made it."""
        config = self._config
        if config is None:
            return
        # _core._drain holds the raw exporter, not any filter that may wrap
        # it in self._destinations.
        _core._abandon(config, [self._exporter] if self._exporter is not None else ())
        self._config = None
        # Disarmed the way a processor that lost the claim is disarmed, and shut down
        # before the destinations are dropped so the exporter's thread goes with it.
        # Left armed, this one would still stamp convergent.* onto a caller's spans
        # while sending nothing.
        self.shutdown()
        self._semantic = None
        self._destinations = ()

    def _drop(
        self,
        config: _config._Config | None,
        running: _config._Config,
        exporter: SpanProcessor | None,
    ) -> None:
        """Shut down, because another configuration already claimed this process.

        ``init()`` keeps the first setup and drops the second, and this does the
        same. A processor left armed would keep exporting on its own key, past the
        running configuration's ``agents=``, while ``Status`` named only the
        running one.
        """
        if exporter is not None:
            try:
                exporter.shutdown()
            except Exception:
                pass
        # Key the warning by the dropped configuration so a second loser with a
        # different agents or endpoint still gets its own warning.
        _core._warn_once(f"processor_dropped:{hash(config)}", _dropped_message(config, running))

    def _find_provider(self) -> TracerProvider | None:
        """The provider that holds this processor, when it can be named.

        A span processor is never told which provider it was added to, so the
        globally installed one is accepted only once it is carrying this
        processor. Anything else would point ``span()`` at a pipeline that drops
        what it records. Pass ``tracer_provider`` and none of this runs.
        """
        current = trace.get_tracer_provider()
        return current if isinstance(current, TracerProvider) and _carries(current, self) else None

    def _start_registration(self) -> None:
        """Start the deployment registration once, on a thread of its own.

        No lock is held across the request and no span waits for it, which is the
        rule ``init()`` follows for the same call. The gate here is held for a flag
        flip, so exactly one thread starts exactly one registration.
        """
        if self._registration_started:
            return
        with self._gate:
            if self._registration_started:
                return
            self._registration_started = True
        try:
            self._registration = threading.Thread(
                target=self._register, name="convergent-register", daemon=True
            )
            self._registration.start()
        except Exception:
            _core._warn_once(
                "registration_thread",
                "Convergent could not start its deployment registration; traces carry the "
                "release fingerprint for the server to resolve",
            )

    def _register(self) -> None:
        """Register the deployment, then stamp its id on every span from here on.

        The request runs with OpenTelemetry instrumentation suppressed, so a caller
        who traces their HTTP client records no span for it. That keeps our request
        out of their trace, keeps this processor from being handed its own request,
        and keeps the ``Authorization`` header out of anything that captures
        request headers.

        One attempt, whose own retries and backoff belong to
        ``_registry.post_json``. A failure keeps the fingerprint that spans already
        carry and is never tried again, so a control plane that is down costs one
        request rather than one per span.
        """
        config = self._config
        if config is None:
            return
        token = attach(_suppressed())
        try:
            deployment, linked = _core._register_deployment(
                api_key=config.api_key,
                endpoint=config.endpoint,
                release=config.release,
                agents=config.agents,
            )
        finally:
            detach(token)
        if "convergent.deployment.id" not in deployment:
            return
        # One assignment, so a span reading this gets one identity or the other
        # whole rather than a half-written mapping.
        self._semantic = SemanticSpanProcessor(config.release, deployment)
        self._registered = True
        _core._link_deployment(deployment, linked)


def _fingerprint(release: str) -> dict[str, str]:
    """The deployment identity that needs no network call.

    The server resolves it against the ``(organization_id, fingerprint)`` key
    registration upserts on, so a span stamped with it reaches the same deployment
    a registered span reaches.
    """
    return {"convergent.deployment.fingerprint": release}


def _suppressed() -> Context:
    """A context that turns OpenTelemetry instrumentation off for one call."""
    return set_value(
        _SUPPRESS_INSTRUMENTATION_KEY_PLAIN,
        True,
        set_value(_SUPPRESS_INSTRUMENTATION_KEY, True),
    )


def _carries(provider: TracerProvider, processor: SpanProcessor) -> bool:
    """Whether ``provider`` has ``processor`` on it.

    Reads OpenTelemetry's own composite processor, which is private and may move
    between versions, so this is guarded and answers False if that shape moves.
    ``_transport.pending_spans`` reads the same kind of private state. Answering
    False costs ``span()`` and ``observe()``, and one warning names that, which is
    the safe direction: the other one sends a caller's spans somewhere they did
    not ask for.
    """
    members = getattr(getattr(provider, "_active_span_processor", None), "_span_processors", ())

    def _contains(items: object) -> bool:
        try:
            for member in items:  # type: ignore[operator]
                if member is processor:
                    return True
                nested = getattr(member, "_span_processors", None)
                if nested is not None and _contains(nested):
                    return True
        except TypeError:
            pass
        return False

    return _contains(members)


def _asked_for(
    api_key: str | None,
    endpoint: str | None,
    release: str | None,
    agents: list[str] | None,
    require_span_attributes: Mapping[str, object] | None,
    reject_span_attributes: Mapping[str, object] | None,
    tracer_provider: TracerProvider | None,
) -> _config._Config | None:
    """The configuration a dropped processor asked for, or ``None`` when its own
    arguments do not describe one.

    Only for naming what the drop cost. The process is already configured, so
    nothing here can raise on the caller.
    """
    try:
        return _config._processor_config(
            api_key=api_key,
            endpoint=endpoint,
            release=release,
            agents=agents,
            require_span_attributes=require_span_attributes,
            reject_span_attributes=reject_span_attributes,
            tracer_provider=tracer_provider,
            strict=False,
        )
    except Exception:
        return None


def _dropped_message(config: _config._Config | None, running: _config._Config) -> str:
    """Say that a processor is sending nothing, and name what went with it."""
    named = _core._dropped_settings(config, running)
    return (
        "Convergent tracing is already configured, so the ConvergentSpanProcessor you "
        f"added sends nothing and has shut its exporter down.{named} Configure tracing "
        "once, with init() or with one processor."
    )


def _reset_after_fork() -> None:
    """Repair every processor's state in a forked child.

    fork() copies memory but only the calling thread, so a gate held at fork time
    arrives locked in the child with nobody left to release it, and a registration
    still in flight has no thread running it in the child. The child gets a fresh
    gate, and it gets to start its own registration when the parent's had not
    landed. ``_core`` and ``_egress`` do the same for their own locks.
    """
    for instance in _instances:
        instance._gate = threading.Lock()
        if not instance._registered:
            instance._registration_started = False
            instance._registration = None


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_after_fork)


__all__ = ["ConvergentSpanProcessor", "install"]
