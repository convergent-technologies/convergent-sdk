"""Writes spans to stdout or stderr as OTLP/JSON.

Two jobs, and the second is the one that matters.

For a person, this answers "what am I actually sending" without standing up a
collector. The alternative today is running an OTLP collector container just to
look at one span.

For a machine it is a **transport**. In Lambda, Cloud Run, and Modal, stdout is
collected off the container automatically, which makes it the one channel that
still works when the process cannot open a socket and nobody is going to come
fetch a file. That is the gap the spans file leaves: a file helps only when
something collects it, and a Lambda has no collector and an ephemeral ``/tmp``.

Same serializer as the file destination, so a captured stdout stream is a spans
file -- the reader tells them apart by content, not by which descriptor they
arrived on.
"""

from __future__ import annotations

import sys
import threading
from collections.abc import Sequence
from typing import Literal, TextIO

from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

from ._file_export import span_to_otlp_json

StreamName = Literal["stdout", "stderr"]

#: One lock per stream, shared by every exporter writing to it. Deliberately not
#: per-instance: each provider builds its own exporter (``_build_destination`` is
#: per-provider by design), so a per-instance lock guards nothing between them and
#: two exporters can interleave mid-record. A concatenated line is not valid JSON,
#: which breaks the promise this module makes -- that captured stdout reads back
#: through the spans-file reader.
_STREAM_LOCKS: dict[StreamName, threading.Lock] = {
    "stdout": threading.Lock(),
    "stderr": threading.Lock(),
}


class ConsoleSpanExporter(SpanExporter):
    """Writes each ended span as one OTLP/JSON line to stdout or stderr.

    The stream is resolved by name on every write rather than captured at
    construction. Test harnesses and anything else that replaces
    ``sys.stdout`` after ``init()`` -- pytest's capture, a logging redirect --
    would otherwise keep writing to a descriptor nobody is reading.
    """

    def __init__(self, stream: StreamName = "stdout", *, pretty: bool = False) -> None:
        self._stream_name: StreamName = stream
        self._pretty = pretty
        self._shutdown = False

    def _stream(self) -> TextIO:
        return sys.stderr if self._stream_name == "stderr" else sys.stdout

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        if self._shutdown:
            return SpanExportResult.FAILURE
        indent = 2 if self._pretty else None
        # One string, one write. Rendering the batch into a single payload keeps a
        # record from being split across two write calls, which is where another
        # exporter's output could land in the middle of ours.
        payload = "".join(f"{span_to_otlp_json(span, indent=indent)}\n" for span in spans)
        with _STREAM_LOCKS[self._stream_name]:
            if self._shutdown:
                return SpanExportResult.FAILURE
            stream = self._stream()
            stream.write(payload)
            stream.flush()
        return SpanExportResult.SUCCESS

    def force_flush(self, timeout_millis: int = 30000) -> bool:  # noqa: ARG002
        # `export` already flushed the stream; nothing buffers past it.
        return True

    def shutdown(self) -> None:
        with _STREAM_LOCKS[self._stream_name]:
            self._shutdown = True


__all__ = ["ConsoleSpanExporter", "StreamName"]
