"""Writes spans to a file as OTLP/JSON, for a process with no route to the
receiver.

``init(destinations=[File(...)])`` installs this. Spans leave as a file, someone
outside the sandbox collects it, and ingest happens there.

The file is OpenTelemetry's own format rather than a shape of ours: newline
delimited ``ExportTraceServiceRequest`` JSON, the same message an OTLP/HTTP
request carries. That matters twice over. A customer who cannot reach us can
hand the file to any OTLP tool instead, and the format carries the
instrumentation scope, which a Convergent-shaped record has nowhere to put --
so a trace ingested from a file describes itself as completely as one that
arrived over the wire.

Two deliberate choices about framing and encoding, both easy to get wrong:

- **One span per line, not one batch per line.** A request may hold many spans
  under one resource, and that is how the network path packs them. Here a
  truncated write would then cost the whole batch rather than one span, in the
  one environment where the file is the only copy of the trace. One span per
  line keeps a partial file a strict prefix of usable spans. It costs about 7%
  in size on a real agent trace, measured, because the resource block repeats --
  which is what the format this replaces already did.
- **Hex ids, not base64.** ``MessageToJson`` follows protobuf's rules and
  base64-encodes ``bytes`` fields, but the OTLP/JSON specification requires
  ``traceId`` and ``spanId`` as hex. A file with base64 ids is not OTLP/JSON:
  our own decoder rejects it and so would a collector.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections.abc import Sequence
from pathlib import Path

from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

logger = logging.getLogger("convergent.sdk")

SPANS_FILENAME = "spans.jsonl"
"""The default name the SDK writes under a ``File`` destination's directory.

Kept from the format this replaces so a collector that pulls a fixed path does
not have to learn a second one. The reader tells the two apart by content, not
by name.
"""

_ID_FIELDS = ("traceId", "spanId", "parentSpanId")


def _to_hex(value: str) -> str:
    """Re-encode one base64 id as the hex the OTLP/JSON spec asks for."""
    import base64

    return base64.b64decode(value).hex()


def span_to_otlp_json(span: ReadableSpan, *, indent: int | None = None) -> str:
    """One span as a single-span OTLP/JSON export request.

    On one line by default, which is what both file destinations need. ``indent``
    is for the console destination's ``pretty`` mode and is the only caller that
    wants a span spread over many lines; a file written that way would not be
    readable by the newline-delimited reader.
    """
    from google.protobuf.json_format import MessageToJson
    from opentelemetry.exporter.otlp.proto.common._internal.trace_encoder import encode_spans

    # Integer enums rather than names: both are valid OTLP/JSON, and integers are
    # what our receiver already reads off the wire, so the file path and the
    # receiver path share one decoder instead of one plus a translation table.
    payload = json.loads(
        MessageToJson(encode_spans([span]), indent=None, use_integers_for_enums=True)
    )
    for resource_spans in payload.get("resourceSpans", []):
        for scope_spans in resource_spans.get("scopeSpans", []):
            for encoded in scope_spans.get("spans", []):
                for field in _ID_FIELDS:
                    if field in encoded:
                        encoded[field] = _to_hex(encoded[field])
                for link in encoded.get("links", []):
                    for field in _ID_FIELDS:
                        if field in link:
                            link[field] = _to_hex(link[field])
    if indent is not None:
        return json.dumps(payload, indent=indent)
    return json.dumps(payload, separators=(",", ":"))


class OtlpFileSpanExporter(SpanExporter):
    """Writes each ended span as one OTLP/JSON line to `target`.

    Several exporters routinely share one target. One directory handed to every
    worker of a service is one file that all of them append to. The lock
    below only orders this exporter's own writes; what keeps two exporters
    from splicing a line is `O_APPEND`, under which the kernel makes the
    seek and the write one step for a regular file. That does not hold on
    NFS, so a spans directory on a network mount is not supported.

    Known limit: `os.write`'s return value is not checked, so a short write
    -- a full disk, an interrupted syscall -- drops the tail of a batch and
    leaves the last line half-written. Rare on a regular file, and the
    lenient reader skips such a line, but the strict one rejects the file.
    """

    def __init__(self, target: Path, *, mode: int = 0o600) -> None:
        self._target = target
        self._lock = threading.Lock()
        self._shutdown = False
        target.parent.mkdir(parents=True, exist_ok=True)
        # O_APPEND so several exporters -- in this process or another -- share one
        # target without truncating each other. O_NOFOLLOW because the directory
        # is caller-supplied, from a public argument and environment variable: a
        # symlink pre-planted at this path by anyone else on the box would
        # redirect captured prompts and outputs somewhere we never chose. Failing
        # loudly is the right answer; nothing legitimately symlinks this file.
        self._fd = os.open(
            target,
            os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW,
            mode,
        )
        # The mode argument above applies only when *this* call creates the file,
        # and umask masks it even then. fchmod is unconditional and exact, so a file
        # left by an earlier run does not keep appending under its old mode.
        try:
            os.fchmod(self._fd, mode)
        except OSError:
            # A filesystem that cannot represent the mode is not a reason to lose
            # the trace -- the alternative is no telemetry at all from a sandbox.
            logger.warning(
                "Convergent could not set mode %o on %s; its permissions are "
                "whatever the filesystem gave it",
                mode,
                target,
            )

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        if self._shutdown:
            return SpanExportResult.FAILURE
        buffer = bytearray()
        for span in spans:
            buffer.extend(span_to_otlp_json(span).encode("utf-8"))
            buffer.append(0x0A)  # b'\n'
        with self._lock:
            if self._shutdown:
                return SpanExportResult.FAILURE
            os.write(self._fd, bytes(buffer))
        return SpanExportResult.SUCCESS

    def force_flush(self, timeout_millis: int = 30000) -> bool:  # noqa: ARG002
        # Writes are already synchronous -- nothing buffers past `export`.
        with self._lock:
            if not self._shutdown:
                os.fsync(self._fd)
        return True

    def shutdown(self) -> None:
        with self._lock:
            if self._shutdown:
                return
            try:
                os.fsync(self._fd)
            except OSError:
                # Closing wins over fsync errors during shutdown -- better to
                # release the fd than wedge here.
                pass
            os.close(self._fd)
            self._shutdown = True


__all__ = ["SPANS_FILENAME", "OtlpFileSpanExporter", "span_to_otlp_json"]
