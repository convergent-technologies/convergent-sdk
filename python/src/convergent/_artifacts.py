"""Files a span records with ``input_artifact()`` and ``output_artifact()``, and
the background thread that uploads them.

A call copies and hashes the file at once, because the file can change or
disappear before the span ends. The span lists its files in one JSON attribute
per role, and the copies wait here, keyed by span id, until the span ends.
:class:`UploadProcessor` sits behind the span filters, so only a span that
leaves the process hands its files to the upload queue. ``span()`` calls
:func:`release` after the span ends, which closes what a withheld span left.

A file's digest is ``sha256:`` plus the SHA-256 of the original bytes. A file
is zstd-compressed on the wire unless its leading bytes name a format that is
already compressed, and the receiver hashes it after it decompresses.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict, deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from http.client import HTTPException
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Literal, get_args

import zstandard
from opentelemetry import context
from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor
from opentelemetry.trace import Span, format_trace_id

from . import _transport
from ._registry import _OPENER, _RETRYABLE_STATUS, backoff_delay

Role = Literal["input", "output"]
#: Who supplied a file: an end user, the agent's own code, a model, a tool, or
#: any other system such as storage, the web, or another service.
ArtifactOrigin = Literal["user", "agent", "model", "tool", "external"]
_ORIGINS: tuple[str, ...] = get_args(ArtifactOrigin)

#: What the SDK does after one answer from ``PUT /v1/artifacts/{digest}``.
#: ``refused`` is a final answer about the bytes, so the file is never sent
#: again. ``fail`` drops it this time only, and a later span can send it again.
Step = Literal["stored", "refused", "retry", "pause", "stop", "fail"]

#: ``open`` takes files. ``stopped`` means the upload route told this process to
#: stop, and ``closed`` means the SDK shut down. Neither takes files again.
_State = Literal["open", "stopped", "closed"]

LIST_KEYS: Mapping[Role, str] = {
    "input": "convergent.artifacts.input",
    "output": "convergent.artifacts.output",
}
DROPPED_KEY = "convergent.artifacts.dropped"

#: The route says uploads are off for the organization with this error reason, and
#: only with it, so a 403 or 404 from a proxy or a load balancer cannot turn
#: uploads off. Uploading and file lists pause for ``_PAUSE_S``, then the next
#: file asks again.
_UPLOADS_OFF_REASON = "artifact_uploads_disabled"
#: Carries the reason on answers that have no body, such as a HEAD.
_REASON_HEADER = "X-Convergent-Reason"
_PAUSE_S = 600.0
#: A bad key ends uploading and file lists for the rest of the process.
_STOP_STATUS = frozenset({401})
#: Answers that depend only on the bytes and the organization. Any 2xx is stored,
#: a retryable status is retried, and any other status drops that one file.
_REFUSED_STATUS = frozenset({413, 422})
#: Read of an error body, enough for its ``details.reason``.
_ERROR_BODY_BYTES = 4096


def _user_agent() -> str:
    # Two names because this module can ship under either distribution name.
    for distribution in ("convergent-sdk", "convergent"):
        try:
            return f"convergent-sdk-python/{version(distribution)}"
        except PackageNotFoundError:
            continue
    return "convergent-sdk-python/0.0.0"


#: Names the SDK and its version on every upload, so the server can tell clients apart.
_USER_AGENT = _user_agent()

#: OpenTelemetry drops span events past 128 and the normalizer rejects a span
#: past 1,024 attributes and events, so files are packed into one attribute per
#: role and capped here.
_FILES_PER_SPAN = 100
#: The receiver's cap on one file's decompressed bytes.
_FILE_BYTES = 16 * 1024 * 1024
#: A copy past this spills from memory to an unlinked temporary file.
_MEMORY_BYTES = 1024 * 1024
#: Ingestion's limits on one listed ``media_type`` and ``filename``.
_MEDIA_TYPE_CHARS = 255
_FILENAME_CHARS = 1024
#: Bytes copied for spans that have not ended yet, across the process.
_HELD_BYTES = 128 * 1024 * 1024
#: Bytes waiting for upload, the one in flight included.
_QUEUE_BYTES = 64 * 1024 * 1024
#: Digests the route stored or refused, so a file attached on every turn is sent once.
_ANSWERED_DIGESTS = 4096
_CHUNK_BYTES = 64 * 1024
_ATTEMPTS = 6
_RETRY_BASE_S = 2.0
_RETRY_CAP_S = 60.0
#: The ingest edge blocks an over-limit address with 429 and ``Retry-After: 300``.
_RETRY_AFTER_CAP_S = 300.0
_REQUEST_TIMEOUT_S = 30.0
#: Leading bytes of image and archive formats zstd cannot shrink: PNG, JPEG,
#: GIF, WebP, gzip, zip (DOCX, XLSX, EPUB), zstd, xz, bzip2, and 7z.
_COMPRESSED = re.compile(
    rb"\x89PNG|\xff\xd8\xff|GIF8|RIFF.{4}WEBP|\x1f\x8b|PK\x03\x04"
    rb"|\x28\xb5\x2f\xfd|\xfd7zXZ\x00|BZh|7z\xbc\xaf\x27\x1c",
    re.DOTALL,
)


@dataclass(frozen=True)
class _Capture:
    role: Role
    digest: str
    size_bytes: int
    media_type: str
    filename: str | None
    origin: ArtifactOrigin | None
    trace_id: str
    body: tempfile.SpooledTemporaryFile[bytes]

    def listing(self) -> dict[str, str | int]:
        entry: dict[str, str | int] = {
            "digest": self.digest,
            "size_bytes": self.size_bytes,
            "media_type": self.media_type,
        }
        if self.filename:
            entry["filename"] = self.filename
        if self.origin:
            entry["origin"] = self.origin
        return entry


@dataclass
class _Held:
    """One running span's files."""

    captures: list[_Capture] = field(default_factory=list)
    dropped: int = 0


_lock = threading.Lock()
_held: dict[int, _Held] = {}
_held_bytes = 0
_uploader: _Uploader | None = None


def attach(
    span: Span,
    role: Role,
    source: object,
    *,
    filename: str | None,
    media_type: str | None,
    origin: ArtifactOrigin | None,
) -> None:
    """Copy ``source``, list it on ``span``, and hold the copy for upload. Never raises.

    Does nothing without a Convergent destination to upload to, or once uploads
    stopped or shut down.
    """
    global _held_bytes
    uploader = _uploader
    if uploader is None or not uploader.accepting():
        return
    try:
        span_context = span.get_span_context()
        # Checked under the lock that ``_take`` uses, so a span that ends now
        # cannot leave an entry no one takes.
        with _lock:
            if not span.is_recording():
                return
            held = _held.setdefault(span_context.span_id, _Held())
            span_full = len(held.captures) >= _FILES_PER_SPAN
            budget_full = _held_bytes >= _HELD_BYTES
        capture = (
            None
            if span_full or budget_full
            else _capture(
                role, source, filename, media_type, origin, format_trace_id(span_context.trace_id)
            )
        )
        # Threads attaching to one span each rewrite the whole list, so the
        # append and the rewrite share the lock.
        with _lock:
            if _held.get(span_context.span_id) is not held or not span.is_recording():
                if capture is not None:
                    capture.body.close()
                return
            if capture is not None:
                span_full = len(held.captures) >= _FILES_PER_SPAN
                budget_full = _held_bytes + capture.size_bytes > _HELD_BYTES
                if span_full or budget_full:
                    capture.body.close()
                    capture = None
            if capture is None:
                held.dropped += 1
                span.set_attribute(DROPPED_KEY, held.dropped)
            else:
                held.captures.append(capture)
                _held_bytes += capture.size_bytes
                listed = [entry.listing() for entry in held.captures if entry.role == role]
                span.set_attribute(
                    LIST_KEYS[role], json.dumps(listed, separators=(",", ":"), ensure_ascii=False)
                )
        if budget_full:
            _warn_once(
                "artifact_held_full",
                f"Convergent did not record a file because files held for spans that "
                f"have not ended passed {_HELD_BYTES // (1024 * 1024)} MiB. The span "
                f"counts it in {DROPPED_KEY}.",
            )
    except Exception:  # noqa: BLE001 - a raise here would break the caller's request
        _warn_once("artifact_attach_failed", "Convergent could not record a file on a span.")


def release(span: Span) -> None:
    """Close the copies a span still holds after it ended. Never raises."""
    if not _held:
        return
    held = _take(span.get_span_context().span_id)
    if held is not None:
        for capture in held.captures:
            capture.body.close()


def start(api_key: str, endpoint: str) -> UploadProcessor:
    """Point uploads at ``endpoint`` and return the processor that feeds them."""
    global _uploader
    if _uploader is not None:
        _uploader.close()
    _uploader = _Uploader(api_key, endpoint)
    return UploadProcessor(_uploader)


def current() -> _Uploader | None:
    return _uploader


class UploadProcessor(SpanProcessor):
    """Hands each finished span's files to the upload queue.

    It is a destination behind the span filters, so a span they withhold never
    reaches it and its files are never sent.
    """

    def __init__(self, uploader: _Uploader) -> None:
        self._uploader = uploader

    def on_end(self, span: ReadableSpan) -> None:
        try:
            span_context = span.get_span_context()
            held = _take(span_context.span_id) if span_context is not None else None
            if held is not None:
                self._uploader.offer(held.captures)
        except Exception:  # noqa: BLE001 - a raise here would break the caller's request
            _warn_once("artifact_offer_failed", "Convergent could not queue a span's files.")

    def force_flush(self, timeout_millis: int = 30_000) -> bool:  # noqa: ARG002
        """Uploads drain through ``flush(uploads=True)``, on their own budget."""
        return True

    def shutdown(self) -> None:
        self._uploader.close()


class _Uploader:
    """One worker thread draining a queue bounded by bytes.

    A file over the bound is dropped and counted rather than waited for. A digest
    already queued, in flight, or recently stored or refused is not queued
    again. A digest that ran out of retries or hit an error is sent again when a
    later span lists it.
    """

    def __init__(self, api_key: str, endpoint: str) -> None:
        self._url = f"{endpoint.rstrip('/')}/v1/artifacts/"
        self._authorization = f"Bearer {api_key}"
        self._state: _State = "open"
        #: Until this ``time.monotonic()`` reading, the route said uploads are off.
        self._paused_until = 0.0
        self._answered: OrderedDict[str, None] = OrderedDict()
        self._reset_runtime()

    def _reset_runtime(self) -> None:
        self._changed = threading.Condition()
        self._queue: deque[_Capture] = deque()
        self._sending: _Capture | None = None
        self._pending: set[str] = set()
        self._queued_bytes = 0
        self._dropped = 0
        self._thread: threading.Thread | None = None

    def accepting(self) -> bool:
        return self._state == "open" and time.monotonic() >= self._paused_until

    def offer(self, captures: list[_Capture]) -> None:
        with self._changed:
            for capture in captures:
                if capture.digest in self._answered:
                    self._answered.move_to_end(capture.digest)
                if capture.digest in self._pending or capture.digest in self._answered:
                    capture.body.close()
                    continue
                if self._state == "open" and time.monotonic() < self._paused_until:
                    # Uploads are off, which is not a failure: skipped, not dropped.
                    capture.body.close()
                    continue
                if not self.accepting() or self._queued_bytes + capture.size_bytes > _QUEUE_BYTES:
                    self._dropped += 1
                    capture.body.close()
                    continue
                self._pending.add(capture.digest)
                self._queued_bytes += capture.size_bytes
                self._queue.append(capture)
            if self._queue and self._thread is None:
                self._thread = threading.Thread(
                    target=self._run, name="convergent-artifacts", daemon=True
                )
                self._thread.start()
            self._changed.notify_all()

    def pending(self) -> int:
        with self._changed:
            return len(self._queue) + (self._sending is not None)

    def take_dropped(self) -> int:
        with self._changed:
            dropped, self._dropped = self._dropped, 0
        return dropped

    def wait(self, timeout_s: float) -> None:
        with self._changed:
            self._changed.wait_for(
                lambda: self._state != "open" or (not self._queue and self._sending is None),
                timeout=timeout_s,
            )

    def close(self) -> None:
        with self._changed:
            self._state = "closed"
            self._discard_queued_locked()

    def reset_after_fork(self) -> None:
        """Forget the parent's queue and thread, but keep what the server told it."""
        self._reset_runtime()

    def _run(self) -> None:
        context.attach(_transport.suppressed())
        while (capture := self._next()) is not None:
            try:
                step = self._deliver(capture)
            except Exception:  # noqa: BLE001 - one bad file must not end uploads for the process
                _warn_once("artifact_upload_failed", "Convergent could not upload a file.")
                step = "fail"
            if step == "stop":
                self._stop()
            elif step == "pause":
                self._pause()
            with self._changed:
                self._sending = None
                self._pending.discard(capture.digest)
                self._queued_bytes -= capture.size_bytes
                if step in ("stored", "refused"):
                    self._answered[capture.digest] = None
                    if len(self._answered) > _ANSWERED_DIGESTS:
                        self._answered.popitem(last=False)
                if step not in ("stored", "pause"):
                    self._dropped += 1
                self._changed.notify_all()
            capture.body.close()

    def _next(self) -> _Capture | None:
        with self._changed:
            self._changed.wait_for(lambda: self._queue or self._state != "open")
            if self._state != "open":
                return None
            self._sending = self._queue.popleft()
            return self._sending

    def _deliver(self, capture: _Capture) -> Step:
        held = _route_holds(self._url + capture.digest, self._authorization, capture.size_bytes)
        if held != "absent":
            # The off answer comes before any bytes, which matters: a large PUT
            # refused before its body is read ends in a broken pipe, not the 403.
            return "stored" if held == "held" else "pause"
        body, headers = _encoded(capture, self._authorization)
        request = urllib.request.Request(
            self._url + capture.digest, data=body, headers=headers, method="PUT"
        )
        step: Step = "retry"
        for attempt in range(_ATTEMPTS):
            step, retry_after = _put(request)
            if step != "retry" or attempt == _ATTEMPTS - 1:
                break
            delay = (
                backoff_delay(attempt, base=_RETRY_BASE_S, cap=_RETRY_CAP_S)
                if retry_after is None
                else min(max(retry_after, 1.0), _RETRY_AFTER_CAP_S)
            )
            with self._changed:
                if self._changed.wait_for(lambda: self._state != "open", timeout=delay):
                    break
        return step

    def _stop(self) -> None:
        with self._changed:
            if self._state != "open":
                return
            self._state = "stopped"
            self._discard_queued_locked()
        _warn_once(
            "artifact_uploads_stopped",
            "Convergent stopped uploading files for this process: the upload route "
            "refused the API key. Spans keep exporting and list no more files.",
        )

    def _pause(self) -> None:
        with self._changed:
            self._paused_until = time.monotonic() + _PAUSE_S
            self._discard_queued_locked(count=False)
        _warn_once(
            "artifact_uploads_paused",
            "Convergent paused file uploads: the upload route says they are off. "
            "Spans keep exporting and list no files until it asks again in "
            f"{_PAUSE_S / 60:g} minutes.",
        )

    def _discard_queued_locked(self, *, count: bool = True) -> None:
        """Drop every queued file, counting them unless uploads are off. The
        caller holds ``_changed``."""
        for capture in self._queue:
            self._pending.discard(capture.digest)
            self._queued_bytes -= capture.size_bytes
            capture.body.close()
        if count:
            self._dropped += len(self._queue)
        self._queue.clear()
        self._changed.notify_all()


def _capture(
    role: Role,
    source: object,
    filename: str | None,
    media_type: str | None,
    origin: ArtifactOrigin | None,
    trace_id: str,
) -> _Capture | None:
    # The upload sends media_type as a header, which rejects line breaks and
    # characters outside Latin-1.
    if media_type and not (
        media_type.isascii() and media_type.isprintable() and len(media_type) <= _MEDIA_TYPE_CHARS
    ):
        _warn_once(
            "artifact_media_type",
            f"Convergent did not record a file whose media_type is not printable "
            f"ASCII of at most {_MEDIA_TYPE_CHARS} characters, such as 'text/csv'. "
            f"The span counts it in {DROPPED_KEY}.",
        )
        return None
    if origin is not None and origin not in _ORIGINS:
        _warn_once(
            "artifact_origin",
            f"Convergent recorded a file without its origin, which is not one of "
            f"{', '.join(_ORIGINS)}.",
        )
        origin = None
    if isinstance(source, bytes | bytearray | memoryview):
        copied = _copy(io.BytesIO(source).read)
    elif isinstance(source, str) and source.startswith(("https://", "http://")):
        _warn_once(
            "artifact_url",
            "Convergent did not record a file given as a URL. Pre-signed URLs are not "
            "supported yet; pass a local path or bytes.",
        )
        return None
    elif isinstance(source, str | os.PathLike):
        path = Path(source)
        filename = filename or path.name
        try:
            with path.open("rb") as file:
                copied = _copy(file.read)
        except OSError as error:
            _warn_once(
                "artifact_unreadable",
                f"Convergent could not read a file passed to input_artifact() or "
                f"output_artifact() ({type(error).__name__}). The span counts it in "
                f"{DROPPED_KEY}.",
            )
            return None
    else:
        _warn_once(
            "artifact_source_type",
            f"Convergent could not record a file given as {type(source).__name__}. "
            "Pass a local path or bytes.",
        )
        return None
    if copied is None:
        _warn_once(
            "artifact_too_large",
            f"Convergent did not record a file larger than {_FILE_BYTES // (1024 * 1024)} "
            f"MiB. The span counts it in {DROPPED_KEY}.",
        )
        return None
    body, sha256, size = copied
    if filename and len(filename) > _FILENAME_CHARS:
        _warn_once(
            "artifact_filename",
            f"Convergent recorded a file without its filename, which is longer than "
            f"{_FILENAME_CHARS} characters.",
        )
        filename = None
    return _Capture(
        role=role,
        digest=f"sha256:{sha256}",
        size_bytes=size,
        media_type=media_type or "application/octet-stream",
        filename=filename,
        origin=origin,
        trace_id=trace_id,
        body=body,
    )


def _copy(
    read: Callable[[int], bytes],
) -> tuple[tempfile.SpooledTemporaryFile[bytes], str, int] | None:
    body = tempfile.SpooledTemporaryFile(max_size=_MEMORY_BYTES)
    digest = hashlib.sha256()
    size = 0
    while chunk := read(_CHUNK_BYTES):
        size += len(chunk)
        if size > _FILE_BYTES:
            body.close()
            return None
        digest.update(chunk)
        body.write(chunk)
    return body, digest.hexdigest(), size


def _encoded(capture: _Capture, authorization: str) -> tuple[bytes, dict[str, str]]:
    capture.body.seek(0)
    body = capture.body.read()
    headers = {
        "Authorization": authorization,
        "User-Agent": _USER_AGENT,
        "Content-Type": capture.media_type,
        "X-Convergent-Trace-Id": capture.trace_id,
    }
    if capture.filename:
        # A name read from disk can hold surrogates, which must not fail the upload.
        filename = urllib.parse.quote(capture.filename, safe="", errors="replace")
        headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{filename}"
    if _COMPRESSED.match(body):
        return body, headers
    headers["Content-Encoding"] = "zstd"
    return zstandard.compress(body), headers


def _route_holds(url: str, authorization: str, size_bytes: int) -> Literal["held", "absent", "off"]:
    """Whether the workspace already holds the file: a 200 whose ``Content-Length``
    is its size. ``off`` when the route says uploads are off. Any other answer
    sends it, so a proxy that answers 200 to everything cannot make uploads stop."""
    request = urllib.request.Request(
        url, headers={"Authorization": authorization, "User-Agent": _USER_AGENT}, method="HEAD"
    )
    try:
        with _OPENER.open(request, timeout=_REQUEST_TIMEOUT_S) as response:
            same_size = response.headers.get("Content-Length") == str(size_bytes)
            return "held" if response.status == 200 and same_size else "absent"
    except urllib.error.HTTPError as error:
        error.close()
        return "off" if error.headers.get(_REASON_HEADER) == _UPLOADS_OFF_REASON else "absent"
    except (OSError, HTTPException):
        return "absent"


def _put(request: urllib.request.Request) -> tuple[Step, float | None]:
    """One attempt, and the ``Retry-After`` seconds the route asked for, if any."""
    try:
        with _OPENER.open(request, timeout=_REQUEST_TIMEOUT_S) as response:
            return _step(response.status), None
    except urllib.error.HTTPError as error:
        try:
            reason = error.headers.get(_REASON_HEADER) or _error_reason(
                error.read(_ERROR_BODY_BYTES)
            )
        except (OSError, HTTPException):
            reason = None
        finally:
            error.close()
        return _step(error.code, reason), _seconds(error.headers.get("Retry-After"))
    except (OSError, HTTPException):
        return "retry", None


def _error_reason(body: bytes) -> str | None:
    """``details.reason`` of the receiver's error body, if it has one."""
    try:
        parsed = json.loads(body)
    except ValueError:
        return None
    details = parsed.get("details") if isinstance(parsed, dict) else None
    reason = details.get("reason") if isinstance(details, dict) else None
    return reason if isinstance(reason, str) else None


def _step(status: int, reason: str | None = None) -> Step:
    if 200 <= status < 300:
        return "stored"
    if reason == _UPLOADS_OFF_REASON:
        return "pause"
    if status in _STOP_STATUS:
        return "stop"
    if status in _REFUSED_STATUS:
        return "refused"
    return "retry" if status in _RETRYABLE_STATUS else "fail"


def _seconds(value: str | None) -> float | None:
    try:
        return max(0.0, float(value)) if value is not None else None
    except ValueError:
        return None


def _take(span_id: int) -> _Held | None:
    global _held_bytes
    with _lock:
        held = _held.pop(span_id, None)
        if held is not None:
            _held_bytes -= sum(capture.size_bytes for capture in held.captures)
        return held


def _warn_once(key: str, message: str) -> None:
    from . import _core  # deferred: _core imports this module

    _core._warn_once(key, message)


def _reset_after_fork() -> None:
    """Replace the lock and drop the parent's held copies in a forked child.

    The child never ends the parent's spans, and the parent's upload thread did
    not survive the fork.
    """
    global _lock, _held_bytes
    _lock = threading.Lock()
    _held.clear()
    _held_bytes = 0
    if _uploader is not None:
        _uploader.reset_after_fork()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_after_fork)


def _reset_for_tests() -> None:
    global _uploader, _held_bytes
    if _uploader is not None:
        _uploader.close()
    _uploader = None
    with _lock:
        _held.clear()
        _held_bytes = 0


__all__ = ["DROPPED_KEY", "LIST_KEYS", "UploadProcessor", "attach", "current", "release", "start"]
