from __future__ import annotations

import logging
import os
import threading
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

from opentelemetry.context import Context
from opentelemetry.metrics import Counter, Meter, MeterProvider, NoOpCounter, NoOpMeter
from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter, SpanExportResult
from opentelemetry.semconv.attributes.error_attributes import ERROR_TYPE
from opentelemetry.util.types import Attributes

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from opentelemetry.exporter.otlp.proto.http import Compression
    from requests import Session

logger = logging.getLogger("convergent.sdk")

_INTERNAL_METRICS = "OTEL_PYTHON_SDK_INTERNAL_METRICS_ENABLED"
_QUEUE_FULL = "queue_full"
_CONTENT_TOO_LARGE = 413


class AuthRejectingSession:
    def __init__(self, session: Any) -> None:
        self._session = session
        self._rejected = False
        self._reported_too_large = False
        self.headers = session.headers

    def is_rejected(self) -> bool:
        return self._rejected

    def post(self, *args: Any, **kwargs: Any) -> Any:
        if self._rejected:
            return _accepted()
        response = self._session.post(*args, **kwargs)
        if response.status_code == _CONTENT_TOO_LARGE:
            # The exporter does not retry a 413, so the batch is simply gone. Named
            # once per process: a producer whose spans are reliably too large would
            # otherwise log on every export forever.
            if not self._reported_too_large:
                self._reported_too_large = True
                logger.error(
                    "Convergent lost a span batch: the collector refused it as too "
                    "large, and a 413 is not retried. Lower "
                    "OTEL_BSP_MAX_EXPORT_BATCH_SIZE so each request carries fewer "
                    "spans. A single span whose own content exceeds the receiver's "
                    "limit cannot be exported at any batch size.",
                    extra={"event": "convergent.sdk.batch_too_large"},
                )
            return response
        if response.status_code not in (401, 403):
            return response
        self._rejected = True
        close = getattr(response, "close", None)
        if callable(close):
            close()
        logger.warning(
            "Convergent tracing is disabled because the collector rejected its credentials"
        )
        return _accepted()

    def close(self) -> None:
        self._session.close()


def _accepted() -> SimpleNamespace:
    return SimpleNamespace(ok=True, status_code=200, reason="disabled")


class _DropCounter(NoOpCounter):
    """Counts the spans OpenTelemetry threw away because a queue was full.

    ``BatchProcessor`` adds to one counter both for a finished export and for a
    drop, so ``error.type`` is what tells the two apart.
    """

    def __init__(self) -> None:
        super().__init__("convergent.sdk.spans.dropped")
        self._lock = threading.Lock()
        self._count = 0

    def add(
        self,
        amount: int | float,
        attributes: Attributes | None = None,
        context: Context | None = None,
    ) -> None:
        if not attributes or attributes.get(ERROR_TYPE) != _QUEUE_FULL:
            return
        with self._lock:
            self._count += int(amount)

    def take(self) -> int:
        with self._lock:
            count, self._count = self._count, 0
        return count

    def reset_after_fork(self) -> None:
        self._lock = threading.Lock()
        self._count = 0


_drops = _DropCounter()

_lost = 0
_lost_lock = threading.Lock()


def _record_lost(count: int) -> None:
    global _lost
    with _lost_lock:
        _lost += count


def lost_spans() -> int:
    """Spans lost to a failed export since this was last called.

    ``BatchSpanProcessor.force_flush`` answers True whatever the exporter said,
    so this counter is how ``flush()`` learns a batch never landed.
    """
    global _lost
    with _lost_lock:
        count, _lost = _lost, 0
    return count


class _LossCountingExporter(SpanExporter):
    """Counts the spans the wrapped exporter failed to deliver.

    ``rejected`` is how a refused credential becomes a counted loss: once the
    collector answers 401 or 403, :class:`AuthRejectingSession` reports success
    to the exporter so it neither retries nor logs per batch, and this wrapper
    is the only layer that still knows the batch never landed.
    """

    def __init__(self, exporter: SpanExporter, rejected: Callable[[], bool] | None) -> None:
        self._exporter = exporter
        self._rejected = rejected

    def _credentials_rejected(self) -> bool:
        return self._rejected is not None and self._rejected()

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        if self._credentials_rejected():
            # The post-export check below counts a withheld batch too; this
            # branch only skips the pointless encode and post once the key is
            # known dead.
            _record_lost(len(spans))
            return SpanExportResult.FAILURE
        try:
            result = self._exporter.export(spans)
        except Exception:
            _record_lost(len(spans))
            raise
        if result is not SpanExportResult.SUCCESS:
            _record_lost(len(spans))
            return result
        if self._credentials_rejected():
            # The rejection happened on this batch: the session answered the
            # exporter with a success so nothing retries, but nothing landed.
            _record_lost(len(spans))
            return SpanExportResult.FAILURE
        return result

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return self._exporter.force_flush(timeout_millis)

    def shutdown(self) -> None:
        self._exporter.shutdown()


class _CountingMeter(NoOpMeter):
    def create_counter(self, name: str, unit: str = "", description: str = "") -> Counter:
        return _drops


class _CountingMeterProvider(MeterProvider):
    def get_meter(
        self,
        name: str,
        version: str | None = None,
        schema_url: str | None = None,
        attributes: Attributes | None = None,
    ) -> Meter:
        return _CountingMeter(name)


_meters = _CountingMeterProvider()
_metrics_gate = threading.Lock()


def batch_processor(
    exporter: SpanExporter, *, rejected: Callable[[], bool] | None = None
) -> BatchSpanProcessor:
    """A batch processor whose dropped spans are counted by :func:`dropped_spans`.

    ``meter_provider`` is the only supported way to receive those counts, and
    OpenTelemetry builds the object it reports them through only while
    ``OTEL_PYTHON_SDK_INTERNAL_METRICS_ENABLED`` is set. The variable is set for
    this one construction and then put back. Left on, every ``BatchSpanProcessor``
    the caller builds afterwards would report its own internals through their meter
    provider, which they never asked for.

    ``meter_provider`` reached ``BatchSpanProcessor`` only in a later opentelemetry-sdk,
    and a box can carry an older one. There, fall back to a processor with no drop-count
    telemetry rather than let the whole trace be lost.
    """
    wrapped = _LossCountingExporter(exporter, rejected)
    with _metrics_gate:
        previous = os.environ.get(_INTERNAL_METRICS)
        os.environ[_INTERNAL_METRICS] = "true"
        try:
            return BatchSpanProcessor(wrapped, meter_provider=_meters)
        except TypeError:
            return BatchSpanProcessor(wrapped)
        finally:
            if previous is None:
                del os.environ[_INTERNAL_METRICS]
            else:
                os.environ[_INTERNAL_METRICS] = previous


def dropped_spans() -> int:
    """Spans dropped since this was last called, summed over every processor."""
    return _drops.take()


def build_processor(*, api_key: str, endpoint: str) -> SpanProcessor:
    """The network processor for this destination.

    Compressed by default. Agent spans carry whole conversations, which are JSON
    text and compress several times over, and OpenTelemetry's own default of no
    compression means every prompt goes out at full size. Passing nothing here
    would leave it off, so it is set rather than left to the default;
    ``OTEL_EXPORTER_OTLP_COMPRESSION`` still overrides it.
    """
    # The imports are deferred so that merely importing the SDK does not pull
    # requests and its tree. A file-only or disabled process never exports over
    # the network at all.
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from requests import Session

    warn_on_conflicting_auth_header()
    session = Session()
    session.headers.update({"Authorization": f"Bearer {api_key}"})
    guard = AuthRejectingSession(session)
    exporter = OTLPSpanExporter(
        endpoint=f"{endpoint.rstrip('/')}/v1/traces",
        session=cast("Session", guard),
        compression=_compression(),
    )
    return batch_processor(exporter, rejected=guard.is_rejected)


def _compression() -> Compression:
    """Gzip, or none if the standard variable asked for that.

    The exporter reads these itself, but only when the argument is absent, and
    the argument cannot be absent and defaulted at the same time. So they are
    read here to keep them authoritative, in the exporter's own order: the
    traces-specific variable wins over the generic one. Reading only the generic
    one would make the documented off-switch not work for anyone who set the
    specific one.

    Gzip and none are the whole vocabulary because they are what the collector
    inflates. The exporter also accepts ``deflate``, and honouring that would
    send every batch in an encoding the collector answers 400 to, forever, with
    nothing in the failure naming the variable that caused it.
    """
    from opentelemetry.exporter.otlp.proto.http import Compression

    for name in ("OTEL_EXPORTER_OTLP_TRACES_COMPRESSION", "OTEL_EXPORTER_OTLP_COMPRESSION"):
        chosen = (os.environ.get(name) or "").strip().lower()
        if not chosen:
            continue
        if chosen == "gzip":
            return Compression.Gzip
        if chosen == "none":
            return Compression.NoCompression
        logger.warning(
            "%s=%r is not a compression the Convergent collector reads; sending gzip. "
            "Set it to 'gzip' or 'none'",
            name,
            chosen,
        )
        return Compression.Gzip
    return Compression.Gzip


def _reset_locks_after_fork() -> None:
    """Replace this module's locks and zero its span counts in a forked child.

    ``fork()`` copies memory but only the calling thread, so a lock another thread
    held at fork time arrives locked in the child with nobody left to release it.
    ``_core._reset_lock_after_fork`` does the same for its own lock, and
    ``docs/configuration.md`` states it as a guarantee. ``_metrics_gate`` sits on
    the child's entry path, because a worker that calls ``init()`` after forking
    builds a batch processor and would block here; the window is short, since the
    expensive exporter build happens outside the lock. ``_lost_lock`` and the drop
    counter's lock are taken by the export thread, which does not survive the fork,
    and both sit on the child's ``flush()`` path.

    Both counts go back to zero as well. They describe exports the parent made, so
    a child that inherited them would report the parent's lost spans as its own on
    its first ``flush()``, and the parent would still report them too.
    """
    global _metrics_gate, _lost_lock, _lost
    _metrics_gate = threading.Lock()
    _lost_lock = threading.Lock()
    _lost = 0
    _drops.reset_after_fork()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_locks_after_fork)


def warn_on_conflicting_auth_header() -> None:
    """Warn when ``OTEL_EXPORTER_OTLP_HEADERS`` carries an ``authorization`` key.

    The standard exporter adds those headers to every request, on top of the
    bearer token we set from ``api_key`` -- so an ``authorization`` entry there
    replaces our credentials and every export comes back 401. Nothing in the
    resulting failure names the environment variable, which makes it close to
    undiagnosable from the symptom alone.
    """
    raw = os.environ.get("OTEL_EXPORTER_OTLP_HEADERS", "")
    if not raw:
        return
    for pair in raw.split(","):
        name, _, _value = pair.partition("=")
        if name.strip().lower() == "authorization":
            logger.warning(
                "OTEL_EXPORTER_OTLP_HEADERS sets an 'authorization' header, which "
                "replaces the Convergent API key on every export and will return 401. "
                "Remove that entry; the SDK sets its own credentials"
            )
            return


def is_shut_down(processor: SpanProcessor) -> bool:
    """Whether ``processor``'s exporter is already closed.

    A closed exporter has nothing buffered, so flushing it succeeded. But
    ``BatchSpanProcessor.force_flush`` answers False once it is shut down, which
    would make ``flush()`` report a failure for a process that tore its provider
    down cleanly. This reads OpenTelemetry's private state, like
    :func:`pending_spans`, so it is guarded and answers False if that shape moves.
    That failure mode is accepted: an OTel rename would make ``flush()`` report
    failure for a clean teardown again, which is loud enough to get this lookup
    updated.
    """
    return getattr(getattr(processor, "_batch_processor", None), "_shutdown", False) is True


def pending_spans(processor: SpanProcessor) -> int:
    """Best-effort count of spans still queued in ``processor``.

    Reads OpenTelemetry's own queue, which is private and may move between
    versions, so this is guarded and reports 0 rather than failing a flush. It is
    the only way to tell "drained everything" from "gave up with work left":
    ``force_flush`` returns a bare bool.
    """
    # The `_batch_processor` / `_queue` walk is OpenTelemetry's private API, which
    # is why it is guarded rather than typed.
    batch = getattr(processor, "_batch_processor", None)
    queue = getattr(batch, "_queue", None)
    if queue is None:
        return 0
    try:
        return len(queue)
    except TypeError:
        return 0


__all__ = [
    "AuthRejectingSession",
    "batch_processor",
    "build_processor",
    "dropped_spans",
    "is_shut_down",
    "lost_spans",
    "pending_spans",
    "warn_on_conflicting_auth_header",
]
