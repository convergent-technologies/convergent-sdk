"""``input_artifact()`` and ``output_artifact()`` against a local fake of
``HEAD`` and ``PUT /v1/artifacts/{digest}`` (CON-7077, CON-7079).

The fake checks what the real route checks: the digest in the path must be
``sha256:`` and the SHA-256 of the decompressed body, or it answers 400. ``HEAD``
answers 200 and the stored ``Content-Length`` for a digest a ``PUT`` stored or a
test put in ``held``.
It also takes span batches
on ``/v1/traces``, so each test reads the file lists off the spans that really
left the process.
"""

from __future__ import annotations

import gzip
import hashlib
import http.server
import json
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest
import zstandard
from opentelemetry import trace
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

import convergent
from convergent import _artifacts, _core, _registry

_KEY = "k-artifacts"  # pragma: allowlist secret


@dataclass(frozen=True)
class _Put:
    digest: str
    headers: dict[str, str]
    body: bytes


@dataclass
class _Route:
    """A local receiver for span batches and file uploads."""

    endpoint: str = ""
    puts: list[_Put] = field(default_factory=list)
    held: dict[str, int] = field(default_factory=dict)
    batches: list[bytes] = field(default_factory=list)
    answers: list[tuple[int, dict[str, str]]] = field(default_factory=list)
    gate: threading.Event = field(default_factory=threading.Event)

    def __post_init__(self) -> None:
        self.gate.set()

    def spans(self) -> dict[str, dict[str, str | int]]:
        found: dict[str, dict[str, str | int]] = {}
        for batch in self.batches:
            request = ExportTraceServiceRequest.FromString(gzip.decompress(batch))
            for resource in request.resource_spans:
                for scope in resource.scope_spans:
                    for span in scope.spans:
                        found[span.name] = {
                            item.key: item.value.string_value or item.value.int_value
                            for item in span.attributes
                        }
        return found


@pytest.fixture
def route(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Route]:
    monkeypatch.delenv("CONVERGENT_API_KEY", raising=False)
    monkeypatch.delenv("CONVERGENT_ENDPOINT", raising=False)
    monkeypatch.setattr(_registry, "post_json", lambda *a, **k: {"deployment_id": "dep_artifacts"})
    _reset_otel()
    _core._reset_for_tests()
    state = _Route()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            state.batches.append(self.rfile.read(int(self.headers["Content-Length"])))
            self.send_response(200)
            self.end_headers()

        def do_PUT(self) -> None:
            state.gate.wait()
            body = self.rfile.read(int(self.headers["Content-Length"]))
            if self.headers.get("Content-Encoding") == "zstd":
                body = zstandard.decompress(body)
            digest = self.path.removeprefix("/v1/artifacts/")
            state.puts.append(_Put(digest, dict(self.headers), body))
            status, headers = state.answers.pop(0) if state.answers else (201, {})
            if _sha(body) != digest:
                status, headers = 400, {}
            if 200 <= status < 300:
                state.held[digest] = len(body)
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            self.end_headers()

        def do_HEAD(self) -> None:
            size = state.held.get(self.path.removeprefix("/v1/artifacts/"))
            self.send_response(404 if size is None else 200)
            if size is not None:
                self.send_header("Content-Length", str(size))
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None: ...

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    state.endpoint = f"http://127.0.0.1:{server.server_port}"
    try:
        yield state
    finally:
        state.gate.set()
        _core._reset_for_tests()
        _reset_otel()
        server.shutdown()
        server.server_close()


def _reset_otel() -> None:
    trace._TRACER_PROVIDER_SET_ONCE._done = False  # type: ignore[attr-defined]
    trace._TRACER_PROVIDER = None  # type: ignore[attr-defined]


def _init(route: _Route, **kwargs: object) -> None:
    convergent.init(api_key=_KEY, endpoint=route.endpoint, release="r1", **kwargs)  # type: ignore[arg-type]


def _sha(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def _upload_threads() -> list[threading.Thread]:
    return [thread for thread in threading.enumerate() if thread.name == "convergent-artifacts"]


def test_a_span_the_filters_withhold_uploads_nothing(route: _Route) -> None:
    _init(route, reject_span_attributes={"tier": ["secret"]})

    with convergent.span(name="kept", operation="tool_call") as kept:
        kept.input_artifact(b"kept bytes", filename="kept.txt", origin="user")
    with convergent.span(name="held", operation="tool_call", attributes={"tier": "secret"}) as held:
        held.output_artifact(b"secret bytes", filename="secret.txt")
    result = convergent.flush(uploads=True)

    assert [put.digest for put in route.puts] == [_sha(b"kept bytes")]
    spans = route.spans()
    assert "execute_tool held" not in spans
    assert json.loads(str(spans["execute_tool kept"]["convergent.artifacts.input"])) == [
        {
            "digest": _sha(b"kept bytes"),
            "size_bytes": 10,
            "media_type": "application/octet-stream",
            "filename": "kept.txt",
            "origin": "user",
        }
    ]
    assert (result.ok, result.pending_uploads, result.dropped_uploads) == (True, 0, 0)


def test_an_unknown_origin_keeps_the_file_without_the_label(route: _Route) -> None:
    _init(route)

    with convergent.span(name="labelled", operation="tool_call") as labelled:
        labelled.input_artifact(b"labelled bytes", origin="subagent")  # type: ignore[arg-type]
    convergent.flush(uploads=True)

    assert [put.digest for put in route.puts] == [_sha(b"labelled bytes")]
    span = route.spans()["execute_tool labelled"]
    assert json.loads(str(span["convergent.artifacts.input"])) == [
        {
            "digest": _sha(b"labelled bytes"),
            "size_bytes": 14,
            "media_type": "application/octet-stream",
        }
    ]
    assert "convergent.artifacts.dropped" not in span


def test_any_file_uploads_under_the_callers_type_zstd_unless_already_compressed(
    route: _Route,
) -> None:
    _init(route)
    docx = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    docx_bytes = b"PK\x03\x04 docx"

    with convergent.span(name="report", operation="tool_call") as report:
        report.output_artifact(docx_bytes, filename="report.docx", media_type=docx)
        report.output_artifact(b"# notes", filename="my notes é.md")
    convergent.flush(uploads=True)

    sent = {put.digest: put.headers for put in route.puts}
    assert sent[_sha(docx_bytes)]["Content-Type"] == docx
    assert "Content-Encoding" not in sent[_sha(docx_bytes)]
    assert sent[_sha(b"# notes")]["Content-Type"] == "application/octet-stream"
    assert sent[_sha(b"# notes")]["Content-Encoding"] == "zstd"
    assert sent[_sha(b"# notes")]["Content-Disposition"] == (
        "attachment; filename*=UTF-8''my%20notes%20%C3%A9.md"
    )
    span = route.spans()["execute_tool report"]
    assert json.loads(str(span["convergent.artifacts.output"])) == [
        {
            "digest": _sha(docx_bytes),
            "size_bytes": len(docx_bytes),
            "media_type": docx,
            "filename": "report.docx",
        },
        {
            "digest": _sha(b"# notes"),
            "size_bytes": 7,
            "media_type": "application/octet-stream",
            "filename": "my notes é.md",
        },
    ]


def test_a_listing_keeps_within_what_the_server_reads(route: _Route) -> None:
    _init(route)

    with convergent.span(name="named", operation="tool_call") as named:
        named.input_artifact(b"long name", filename="é" * 1025)
        named.input_artifact(b"short name", filename="é" * 1024)
        named.input_artifact(b"long type", media_type="text/" + "x" * 300)
    convergent.flush(uploads=True)

    span = route.spans()["execute_tool named"]
    listed = str(span["convergent.artifacts.input"])
    assert [entry.get("filename") for entry in json.loads(listed)] == [None, "é" * 1024]
    assert "\\u00e9" not in listed
    assert span["convergent.artifacts.dropped"] == 1


def test_only_spans_the_sdk_opened_hold_files(route: _Route) -> None:
    _init(route, reject_span_attributes={"tier": ["secret"]})

    @convergent.observe(name="decorated", operation="tool_call")
    def decorated() -> None:
        convergent.current_span().input_artifact(b"decorated bytes")

    decorated()
    tracer = trace.get_tracer("convergent.sdk")
    with tracer.start_as_current_span("foreign", attributes={"tier": "secret"}):
        convergent.current_span().input_artifact(b"foreign bytes")
    convergent.flush(uploads=True)

    assert [put.digest for put in route.puts] == [_sha(b"decorated bytes")]
    assert _artifacts._held == {}


def test_a_file_on_three_spans_uploads_once_as_copied_at_call_time(
    route: _Route, tmp_path: Path
) -> None:
    _init(route)
    original = b"invoice,total\n1,20\n" * 50
    path = tmp_path / "invoices.csv"
    path.write_bytes(original)
    route.gate.clear()

    trace_ids = []
    for turn in range(3):
        with convergent.span(name=f"turn-{turn}", operation="model_call") as turn_span:
            turn_span.input_artifact(path)
            trace_ids.append(turn_span.trace_id)
    path.unlink()
    route.gate.set()
    convergent.flush(uploads=True)

    (put,) = route.puts
    assert (put.digest, put.body) == (_sha(original), original)
    assert put.headers["Content-Encoding"] == "zstd"
    assert put.headers["Authorization"] == f"Bearer {_KEY}"
    assert put.headers["X-Convergent-Trace-Id"] == trace_ids[0]


def test_a_file_the_workspace_already_holds_sends_no_bytes(route: _Route) -> None:
    _init(route)
    route.held[_sha(b"uploaded by an earlier process")] = len(b"uploaded by an earlier process")

    with convergent.span(name="tool", operation="tool_call") as tool:
        tool.input_artifact(b"uploaded by an earlier process")
    result = convergent.flush(uploads=True)

    assert route.puts == []
    assert "convergent.artifacts.input" in route.spans()["execute_tool tool"]
    assert (result.ok, result.dropped_uploads) == (True, 0)


def test_a_held_answer_with_another_size_still_uploads(route: _Route) -> None:
    _init(route)
    route.held[_sha(b"twelve bytes")] = 0

    with convergent.span(name="tool", operation="tool_call") as tool:
        tool.input_artifact(b"twelve bytes")
    result = convergent.flush(uploads=True)

    assert [put.body for put in route.puts] == [b"twelve bytes"]
    assert (result.ok, result.dropped_uploads) == (True, 0)


def test_a_full_queue_drops_the_upload_and_counts_it(
    route: _Route, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_artifacts, "_QUEUE_BYTES", 10)
    _init(route)
    route.gate.clear()

    with convergent.span(name="first", operation="tool_call") as first:
        first.input_artifact(b"8 bytes!")
    with convergent.span(name="second", operation="tool_call") as second:
        second.input_artifact(b"8 more!!")
    waiting = convergent.flush(timeout_ms=1_000)
    route.gate.set()
    drained = convergent.flush(uploads=True)

    assert (waiting.pending_uploads, waiting.dropped_uploads) == (1, 1)
    assert (drained.pending_uploads, drained.dropped_uploads) == (0, 0)
    assert [put.digest for put in route.puts] == [_sha(b"8 bytes!")]


def test_a_503_is_retried_after_retry_after(route: _Route) -> None:
    _init(route)
    route.answers.append((503, {"Retry-After": "0"}))

    with convergent.span(name="tool", operation="tool_call") as tool:
        tool.output_artifact(b"%PDF-1.7 report", filename="report.pdf")
    result = convergent.flush(uploads=True)

    assert [put.digest for put in route.puts] == [_sha(b"%PDF-1.7 report")] * 2
    assert (result.ok, result.dropped_uploads) == (True, 0)


def test_a_404_stops_uploading_and_listing_for_the_process(route: _Route) -> None:
    _init(route)
    route.answers.append((404, {}))

    with convergent.span(name="before", operation="tool_call") as before:
        before.input_artifact(b"first file")
    convergent.flush(uploads=True)
    with convergent.span(name="after", operation="tool_call") as after:
        after.input_artifact(b"second file")
    convergent.flush(uploads=True)

    assert [put.digest for put in route.puts] == [_sha(b"first file")]
    spans = route.spans()
    assert "convergent.artifacts.input" in spans["execute_tool before"]
    assert "convergent.artifacts.input" not in spans["execute_tool after"]
    for thread in _upload_threads():
        thread.join(timeout=2)
    assert not any(thread.is_alive() for thread in _upload_threads())


def test_a_closed_queue_copies_no_files_and_flush_does_not_wait(route: _Route) -> None:
    _init(route)
    route.gate.clear()

    with convergent.span(name="in flight", operation="tool_call") as first:
        first.input_artifact(b"in flight")
    convergent.flush()
    uploader = _artifacts.current()
    assert uploader is not None
    uploader.close()
    with convergent.span(name="after close", operation="tool_call") as after:
        after.input_artifact(b"after close")
    started = time.monotonic()
    convergent.flush(uploads=True, upload_timeout_ms=5_000)

    assert time.monotonic() - started < 2
    assert "convergent.artifacts.input" not in route.spans()["execute_tool after close"]


def test_a_later_span_resends_a_file_that_ran_out_of_retries_but_not_one_refused(
    route: _Route, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_artifacts, "_ATTEMPTS", 1)
    _init(route)
    route.answers += [(503, {}), (422, {})]

    for turn in range(2):
        with convergent.span(name=f"turn-{turn}", operation="model_call") as turn_span:
            turn_span.input_artifact(b"system prompt")
            turn_span.input_artifact(b"refused type")
        result = convergent.flush(uploads=True)

    assert [put.digest for put in route.puts] == [
        _sha(b"system prompt"),
        _sha(b"refused type"),
        _sha(b"system prompt"),
    ]
    assert result.dropped_uploads == 0


def test_a_bad_file_does_not_stop_later_uploads(
    route: _Route, monkeypatch: pytest.MonkeyPatch
) -> None:
    encoded = _artifacts._encoded

    def broken(capture: _artifacts._Capture, authorization: str) -> tuple[bytes, dict[str, str]]:
        if capture.digest == _sha(b"breaks the worker"):
            raise MemoryError
        return encoded(capture, authorization)

    monkeypatch.setattr(_artifacts, "_encoded", broken)
    _init(route)

    with convergent.span(name="bad type", operation="tool_call") as bad:
        bad.input_artifact(b"newline type", media_type="text/plain\n")
    with convergent.span(name="breaks", operation="tool_call") as breaks:
        breaks.input_artifact(b"breaks the worker")
    with convergent.span(name="good", operation="tool_call") as good:
        good.input_artifact(b"good file")
    result = convergent.flush(uploads=True, upload_timeout_ms=5_000)

    assert [put.digest for put in route.puts] == [_sha(b"good file")]
    assert route.spans()["execute_tool bad type"]["convergent.artifacts.dropped"] == 1
    assert (result.pending_uploads, result.dropped_uploads) == (0, 1)


def test_a_span_lists_at_most_100_files_and_counts_the_rest(route: _Route) -> None:
    _init(route)

    with convergent.span(name="bulk", operation="tool_call") as bulk:
        for index in range(101):
            bulk.input_artifact(f"file {index}".encode())
    convergent.flush(uploads=True)

    attributes = route.spans()["execute_tool bulk"]
    assert len(json.loads(str(attributes["convergent.artifacts.input"]))) == 100
    assert attributes["convergent.artifacts.dropped"] == 1
    assert len(route.puts) == 100
