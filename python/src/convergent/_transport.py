from __future__ import annotations

import functools
import logging
import os
import threading
import time
from typing import TYPE_CHECKING, Any

from opentelemetry.attributes import BoundedAttributes
from opentelemetry.context import _SUPPRESS_INSTRUMENTATION_KEY, Context, set_value
from opentelemetry.metrics import Counter, Meter, MeterProvider, NoOpCounter, NoOpMeter
from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter, SpanExportResult
from opentelemetry.semconv.attributes.error_attributes import ERROR_TYPE
from opentelemetry.util.types import Attributes

from ._litellm import without_request_copies
from ._processors import evicted_stamp_keys, trajectory_stamp, with_attributes
from ._receiver_routes import TRACES_PATH
from ._telemetry_keys import CONVERGENT_SDK_VERSION
from ._user_agent import USER_AGENT
from ._version import sdk_version

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from opentelemetry.exporter.otlp.proto.http import Compression
    from requests import Response

logger = logging.getLogger("convergent.sdk")

_INTERNAL_METRICS = "OTEL_PYTHON_SDK_INTERNAL_METRICS_ENABLED"
_QUEUE_FULL = "queue_full"
_CONTENT_TOO_LARGE = 413
# The managed ingest ALB retires idle connections after 60 seconds.
_IDLE_CONNECTION_REFRESH_S = 55.0
_FRESH_CONNECTION_RETRY_RESERVE_S = 5.0
_DEFAULT_EXPORT_TIMEOUT_S = 10.0
#: The suppression key that predates the per-process one in ``opentelemetry.context``.
#: ``is_instrumentation_enabled`` reads both, so a request that must record no span
#: sets both. Written out rather than imported, because it lives in the
#: instrumentation package and this SDK depends on the API and the SDK only.
_SUPPRESS_INSTRUMENTATION_KEY_PLAIN = "suppress_instrumentation"


class _ExportDiagnostics:
    """Safe facts from one network export, without request or response content."""

    def __init__(self) -> None:
        self._started = 0.0
        self._attempt_count = 0
        self._status_code: int | None = None
        self._exception_type: str | None = None
        self._cause_type: str | None = None

    def begin(self) -> None:
        self._started = time.monotonic()
        self._attempt_count = 0
        self._status_code = None
        self._exception_type = None
        self._cause_type = None

    def record_attempt(self) -> int:
        self._attempt_count += 1
        return self._attempt_count

    def record_response(self, status_code: int) -> None:
        self._status_code = status_code
        self._exception_type = None
        self._cause_type = None

    def record_exception(self, error: Exception) -> None:
        self._status_code = None
        self._exception_type = type(error).__name__
        cause = error
        seen = {id(error)}
        while (next_error := cause.__cause__ or cause.__context__) is not None:
            if id(next_error) in seen:
                break
            seen.add(id(next_error))
            cause = next_error
        cause_type = type(cause).__name__
        self._cause_type = cause_type if cause_type != self._exception_type else None

    def report_failure(self, span_count: int) -> None:
        elapsed_ms = max(0, int((time.monotonic() - self._started) * 1_000))
        if self._status_code is not None:
            last_outcome = "http_status"
            detail = f"Last observed collector response was HTTP {self._status_code}"
        elif self._exception_type is not None:
            last_outcome = "request_exception"
            detail = f"Last observed request exception was {self._exception_type}"
            if self._cause_type is not None:
                detail += f" from {self._cause_type}"
        else:
            last_outcome = "exporter_failure"
            detail = (
                "OpenTelemetry returned failure without an observed response or request exception"
            )
        logger.error(
            "Convergent lost %d span(s) after %d export attempt(s) in %dms. %s. "
            "The application continued.",
            span_count,
            self._attempt_count,
            elapsed_ms,
            detail,
            extra={
                "event": "convergent.sdk.export_failed",
                "last_outcome": last_outcome,
                "exception_type": self._exception_type,
                "cause_type": self._cause_type,
                "status_code": self._status_code,
                "attempt_count": self._attempt_count,
                "span_count": span_count,
                "elapsed_ms": elapsed_ms,
            },
        )


def suppressed() -> Context:
    """A context that turns OpenTelemetry instrumentation off for the SDK's own requests.

    A caller who traces their HTTP client then records no span for our request,
    and nothing that captures request headers sees the ``Authorization`` header.
    """
    return set_value(
        _SUPPRESS_INSTRUMENTATION_KEY_PLAIN,
        True,
        set_value(_SUPPRESS_INSTRUMENTATION_KEY, True),
    )


class _ResponseHook:
    """Reads the collector's answer to each export request the session makes."""

    def __init__(self, diagnostics: _ExportDiagnostics) -> None:
        self._rejected = False
        self._reported_too_large = False
        self._diagnostics = diagnostics

    def is_rejected(self) -> bool:
        return self._rejected

    def __call__(self, response: Response, **_kwargs: Any) -> None:
        status = response.status_code
        self._diagnostics.record_response(status)
        if status >= 400:
            # Supported OpenTelemetry exporter versions log either ``reason`` or
            # the response body. Neither is safe to copy into a customer's logs.
            _ = response.content
            response.reason = f"HTTP {status}"
            response._content = b""
        if status in (401, 403) and not self._rejected:
            self._rejected = True
            logger.warning(
                "Convergent tracing is disabled because the collector rejected its credentials"
            )
        elif status == _CONTENT_TOO_LARGE and not self._reported_too_large:
            # The exporter does not retry a 413, so the batch is simply gone. Named
            # once per process: a producer whose spans are reliably too large would
            # otherwise log on every export forever.
            self._reported_too_large = True
            logger.error(
                "Convergent lost a span batch: the collector refused it as too "
                "large, and a 413 is not retried. Lower "
                "OTEL_BSP_MAX_EXPORT_BATCH_SIZE so each request carries fewer "
                "spans. A single span whose own content exceeds the receiver's "
                "limit cannot be exported at any batch size.",
                extra={"event": "convergent.sdk.batch_too_large"},
            )


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


@functools.cache
def _sdk_version_label() -> str:
    return f"python/{sdk_version()}"


def _naming_the_sdk(span: ReadableSpan) -> ReadableSpan:
    """A copy of ``span`` that carries ``convergent.sdk.version`` and its whole stamp.

    The span itself is shared with every other processor on the provider and is
    frozen once it ends, so the key goes only on this copy: the customer's own
    exporters never see it. The copy has no attribute limit, so a span that
    filled its limit still names the SDK, and it keeps the span's drop counts.
    The stamp is written back from the span's private field, because a span that
    filled its limit after it started has lost the stamp from its attributes,
    and a span naming the SDK without one would read as unstamped to ingest.
    """
    attributes = BoundedAttributes(
        attributes={
            **(span.attributes or {}),
            **trajectory_stamp(span),
            CONVERGENT_SDK_VERSION: _sdk_version_label(),
        }
    )
    # The stamp keys written back here arrive, so they are not lost.
    attributes.dropped = max(span.dropped_attributes - evicted_stamp_keys(span), 0)
    return with_attributes(span, attributes)


def _named_or_as_it_came(span: ReadableSpan) -> ReadableSpan:
    """``span`` named for the SDK, or as it came when the copy fails.

    One span the SDK cannot copy goes out without the version key rather than
    taking its whole batch down, as the litellm trim falls back to the batch as
    it came.
    """
    try:
        return _naming_the_sdk(span)
    except Exception:
        from . import _core  # deferred: _core imports this module

        logger.debug("Convergent could not copy a span to name the SDK", exc_info=True)
        _core._warn_once("sdk_version_copy", "Convergent could not add its SDK version to a span")
        return span


class _LossCountingExporter(SpanExporter):
    """Counts the spans the wrapped exporter failed to deliver.

    Each span goes out as a copy that names the SDK (:func:`_naming_the_sdk`).
    Ingest tells a stamping SDK's spans apart by that key. Each batch also goes
    out without litellm's second copy of a model call's request
    (:func:`without_request_copies`), so every destination sends it once.

    ``rejected`` says whether the collector has refused the credentials. Once it
    has, a batch is counted as lost without being encoded or sent.
    """

    def __init__(
        self,
        exporter: SpanExporter,
        rejected: Callable[[], bool] | None,
        diagnostics: _ExportDiagnostics | None,
    ) -> None:
        self._exporter = exporter
        self._rejected = rejected
        self._diagnostics = diagnostics

    def _credentials_rejected(self) -> bool:
        return self._rejected is not None and self._rejected()

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        if self._credentials_rejected():
            _record_lost(len(spans))
            return SpanExportResult.FAILURE
        if self._diagnostics is not None:
            self._diagnostics.begin()
        try:
            result = self._exporter.export(
                without_request_copies([_named_or_as_it_came(span) for span in spans])
            )
        except Exception:
            _record_lost(len(spans))
            if self._diagnostics is not None:
                self._diagnostics.report_failure(len(spans))
            raise
        if result is not SpanExportResult.SUCCESS:
            _record_lost(len(spans))
            if self._diagnostics is not None:
                self._diagnostics.report_failure(len(spans))
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
    exporter: SpanExporter,
    *,
    rejected: Callable[[], bool] | None = None,
    diagnostics: _ExportDiagnostics | None = None,
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
    wrapped = _LossCountingExporter(exporter, rejected, diagnostics)
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
    from requests.exceptions import (
        ConnectionError as RequestConnectionError,
    )
    from requests.exceptions import RequestException, Timeout

    warn_on_conflicting_auth_header()
    diagnostics = _ExportDiagnostics()
    session = Session()
    send = session.send
    last_send_finished_at: float | None = None
    request_timeout = _configured_export_timeout()
    retry_reserve = min(_FRESH_CONNECTION_RETRY_RESERVE_S, max(0.0, request_timeout / 2))

    def safe_request_error(error: RequestException) -> RequestException:
        if isinstance(error, Timeout):
            return Timeout()
        if isinstance(error, RequestConnectionError):
            return RequestConnectionError()
        return RequestException()

    def send_attempt(request: Any, kwargs: dict[str, Any]) -> Any:
        try:
            return send(request, **kwargs)
        except RequestException as error:
            diagnostics.record_exception(error)
            if isinstance(error, (RequestConnectionError, Timeout)):
                session.close()
            raise safe_request_error(error) from None
        except Exception as error:
            diagnostics.record_exception(error)
            raise

    def send_with_diagnostics(request: Any, **kwargs: Any) -> Any:
        nonlocal last_send_finished_at
        now = time.monotonic()
        if (
            last_send_finished_at is not None
            and now - last_send_finished_at >= _IDLE_CONNECTION_REFRESH_S
        ):
            session.close()
        attempt = diagnostics.record_attempt()
        timeout = kwargs.get("timeout")
        started_at = time.monotonic()
        if isinstance(timeout, int | float):
            kwargs["timeout"] = min(timeout, request_timeout)
        try:
            return send_attempt(request, kwargs)
        except Timeout:
            if attempt == 1 and isinstance(timeout, int | float) and retry_reserve > 0:
                remaining = timeout - (time.monotonic() - started_at)
                retry_timeout = min(retry_reserve, remaining)
                if retry_timeout > 0:
                    kwargs["timeout"] = retry_timeout
                    diagnostics.record_attempt()
                    return send_attempt(request, kwargs)
            raise
        finally:
            last_send_finished_at = time.monotonic()

    session.send = send_with_diagnostics
    session.headers.update({"Authorization": f"Bearer {api_key}"})
    hook = _ResponseHook(diagnostics)
    session.hooks["response"].append(hook)
    exporter = OTLPSpanExporter(
        endpoint=f"{endpoint.rstrip('/')}{TRACES_PATH}",
        session=session,
        headers={"User-Agent": USER_AGENT},
        compression=_compression(),
        timeout=request_timeout + retry_reserve,
    )
    # Older exporters overwrite constructor headers with their own defaults.
    session.headers["User-Agent"] = USER_AGENT
    return batch_processor(exporter, rejected=hook.is_rejected, diagnostics=diagnostics)


def _configured_export_timeout() -> float:
    raw = (
        os.environ.get("OTEL_EXPORTER_OTLP_TRACES_TIMEOUT")
        or os.environ.get("OTEL_EXPORTER_OTLP_TIMEOUT")
        or str(_DEFAULT_EXPORT_TIMEOUT_S)
    )
    try:
        return float(raw)
    except ValueError:
        logger.warning(
            "Invalid OTLP export timeout %r; using %.0f seconds",
            raw,
            _DEFAULT_EXPORT_TIMEOUT_S,
        )
        return _DEFAULT_EXPORT_TIMEOUT_S


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
    "batch_processor",
    "build_processor",
    "dropped_spans",
    "is_shut_down",
    "lost_spans",
    "pending_spans",
    "suppressed",
    "warn_on_conflicting_auth_header",
]
