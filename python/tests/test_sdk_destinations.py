"""Destinations: the console stream and the spans file's options and permissions."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import stat
import sys
from collections.abc import AsyncIterator, Generator, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from pathlib import Path
from threading import Barrier, Event
from types import SimpleNamespace
from typing import Any, cast

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import convergent
from convergent import _console_export, _core, _registry, _transport
from convergent._console_export import ConsoleSpanExporter
from convergent._file_export import SPANS_FILENAME


@pytest.fixture(autouse=True)
def reset_sdk(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name in (
        "CONVERGENT_API_KEY",
        "CONVERGENT_ENDPOINT",
        "CONVERGENT_SPANS_DIR",
        "CONVERGENT_TRACES_EXPORTER",
        "CONVERGENT_STRICT",
        "OTEL_EXPORTER_OTLP_HEADERS",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(
        _registry, "post_json", lambda *a, **k: {"deployment_id": "dep_test", "is_new": True}
    )
    _reset_otel()
    _core._reset_for_tests()
    yield
    _core._reset_for_tests()
    _reset_otel()


def _reset_otel() -> None:
    trace._TRACER_PROVIDER_SET_ONCE._done = False  # type: ignore[attr-defined]
    trace._TRACER_PROVIDER = None  # type: ignore[attr-defined]


def _emit(name: str = "support-agent") -> None:
    with convergent.span(name=name, operation="agent_run"):
        pass
    convergent.flush()


def _lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.strip()]


def _readable_span(name: str) -> ReadableSpan:
    """One real ended span, for handing straight to an exporter."""
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    with provider.get_tracer("test").start_as_current_span(name):
        pass
    return exporter.get_finished_spans()[0]


# --------------------------------------------------------------------------
# Console
# --------------------------------------------------------------------------


def test_console_writes_one_span_per_line_to_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    convergent.init(release="r1", destinations=[convergent.Console()])
    _emit()

    captured = capsys.readouterr()
    lines = _lines(captured.out)
    assert lines, "expected at least one span on stdout"
    assert not captured.err
    for line in lines:
        # One line is one complete OTLP/JSON export request, not a fragment.
        assert "resourceSpans" in json.loads(line)


def test_console_stderr_goes_to_stderr(capsys: pytest.CaptureFixture[str]) -> None:
    convergent.init(release="r1", destinations=[convergent.Console(stream="stderr")])
    _emit()

    captured = capsys.readouterr()
    assert _lines(captured.err)
    assert not captured.out


def test_console_pretty_indents_and_compact_does_not(
    capsys: pytest.CaptureFixture[str],
) -> None:
    convergent.init(release="r1", destinations=[convergent.Console(pretty=True)])
    _emit()
    pretty = capsys.readouterr().out
    assert "\n  " in pretty, "pretty output should be indented"

    _core._reset_for_tests()
    _reset_otel()
    convergent.init(release="r1", destinations=[convergent.Console()])
    _emit()
    compact = capsys.readouterr().out
    assert "\n  " not in compact
    assert len(_lines(compact)) >= 1


def test_console_output_reads_back_through_the_spans_file_reader(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """Captured stdout is a spans file. Same format, different descriptor."""
    convergent.init(release="r1", destinations=[convergent.Console()])
    _emit()
    captured = capsys.readouterr().out

    from _otlp import records_from_line

    records = [record for line in _lines(captured) for record in records_from_line(line)]
    assert records, "the spans-file reader should decode console output unchanged"


def test_console_is_additive_alongside_the_spans_file(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """Both destinations receive every span; neither replaces the other."""
    convergent.init(
        release="r1",
        destinations=[convergent.File(tmp_path), convergent.Console()],
    )
    _emit()

    on_stdout = _lines(capsys.readouterr().out)
    on_disk = _lines((tmp_path / SPANS_FILENAME).read_text())
    assert len(on_stdout) == len(on_disk) >= 1


def test_traces_exporter_env_adds_console_rather_than_replacing(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The deliberate divergence from the platform's own tracing setup.

    There, ``CONVERGENT_TRACES_EXPORTER=console`` takes precedence over an OTLP
    endpoint. Here it adds a destination, because a customer debugging delivery
    needs to see what is being sent *and* keep sending it.
    """
    monkeypatch.setenv("CONVERGENT_TRACES_EXPORTER", "console")
    convergent.init(release="r1", destinations=[convergent.File(tmp_path)])
    _emit()

    assert _lines(capsys.readouterr().out), "console should have been added"
    assert _lines((tmp_path / SPANS_FILENAME).read_text()), "file should still receive spans"


def test_console_exporter_resolves_the_stream_on_every_write(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The stream is looked up per write, not captured at construction.

    pytest replaces sys.stdout after init() would have run; an exporter holding
    the original object would write where nobody is looking.
    """
    exporter = ConsoleSpanExporter("stdout")
    provider = TracerProvider()
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    provider.add_span_processor(SimpleSpanProcessor(exporter))
    provider.get_tracer("t").start_span("s").end()
    assert _lines(capsys.readouterr().out)


# --------------------------------------------------------------------------
# File options and permissions
# --------------------------------------------------------------------------


def test_a_batch_is_written_in_a_single_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """One ``write`` per batch, so no record can be split across two calls.

    Writing the record and then its newline separately is the window another
    exporter's output lands inside, producing a concatenated line that no JSON
    parser accepts -- which breaks this module's promise that captured stdout reads
    back through the spans-file reader.

    Counted rather than raced: a threaded test does not reliably reproduce the
    interleaving, so it would read as coverage without being any.
    """
    writes: list[str] = []

    class Recorder:
        def write(self, text: str) -> int:
            writes.append(text)
            return len(text)

        def flush(self) -> None:
            pass

    monkeypatch.setattr(sys, "stdout", Recorder())
    spans = [_readable_span(f"span-{index}") for index in range(5)]
    ConsoleSpanExporter("stdout").export(spans)

    assert len(writes) == 1, "a record split across writes can be interleaved"
    assert len(_lines(writes[0])) == 5


def test_every_exporter_on_a_stream_shares_one_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two exporters on one stream serialise through the *same* lock.

    ``_build_destination`` is per-provider by design, so a lock created in
    ``__init__`` serialises nothing between two providers writing to one stream.

    Asserted by watching the module lock actually get acquired. Checking that no
    ``self._lock`` attribute exists does not prove the shared one is the lock being
    held, and that weaker assertion survived a mutation which swapped in a
    throwaway lock per call.
    """
    assert _console_export._STREAM_LOCKS["stdout"] is not _console_export._STREAM_LOCKS["stderr"], (
        "the two streams are independent targets"
    )

    real = _console_export._STREAM_LOCKS["stdout"]
    acquisitions = 0

    class Watched:
        def __enter__(self) -> None:
            nonlocal acquisitions
            acquisitions += 1
            real.acquire()

        def __exit__(self, *_: object) -> None:
            real.release()

    monkeypatch.setitem(_console_export._STREAM_LOCKS, "stdout", cast("Any", Watched()))
    span = _readable_span("s")
    for exporter in (ConsoleSpanExporter("stdout"), ConsoleSpanExporter("stdout")):
        exporter.export([span])

    assert acquisitions == 2, "both exporters must go through the stream's shared lock"


def test_concurrent_exporters_produce_only_parseable_lines(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """End-to-end net for the above. Cannot be relied on to force the race."""
    exporters = [ConsoleSpanExporter("stdout") for _ in range(4)]
    spans = [_readable_span(f"span-{index}") for index in range(6)]

    with ThreadPoolExecutor(max_workers=len(exporters)) as pool:
        barrier = Barrier(len(exporters))

        def hammer(exporter: ConsoleSpanExporter) -> None:
            barrier.wait()
            for _ in range(12):
                exporter.export(spans)

        list(pool.map(hammer, exporters))

    lines = _lines(capsys.readouterr().out)
    assert len(lines) == len(exporters) * 12 * len(spans)
    for line in lines:
        json.loads(line)


def test_duplicate_destinations_are_folded(capsys: pytest.CaptureFixture[str]) -> None:
    """The same destination named twice is one destination, not two exporters.

    Two exporters on one target write every span twice. The
    ``CONVERGENT_TRACES_EXPORTER`` branch already guarded against a second
    ``Console``, so the caller's own list was the inconsistent case.
    """
    convergent.init(
        release="r1",
        destinations=[convergent.Console(), convergent.Console()],
    )
    _emit()

    assert len(_lines(capsys.readouterr().out)) == 1


def test_the_spans_dir_environment_and_an_equal_file_destination_collapse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """``CONVERGENT_SPANS_DIR=X`` plus ``File(X)`` is one file, written once.

    Paths are made absolute before deduplication precisely so these compare equal;
    otherwise two exporters append every span to the same path.
    """
    monkeypatch.setenv("CONVERGENT_SPANS_DIR", str(tmp_path))
    with caplog.at_level(logging.WARNING, logger="convergent.sdk"):
        convergent.init(release="r1", destinations=[convergent.File(str(tmp_path))])
    _emit()

    assert len(_lines((tmp_path / SPANS_FILENAME).read_text())) == 1
    assert not [r for r in caplog.records if "more than one file" in r.getMessage()]


def test_an_explicit_file_path_is_resolved_at_init(tmp_path: Path) -> None:
    """A relative ``File`` path is made absolute before it is stored.

    Two ``init()`` calls naming the same directory, one relatively and one
    absolutely, are the same configuration. Comparing them unresolved made the
    second warn about a conflict that did not exist.
    """
    original = os.getcwd()
    start = tmp_path / "start"
    start.mkdir()
    try:
        os.chdir(start)
        convergent.init(release="r1", destinations=[convergent.File("relative")])

        stored = _core._config.destinations  # type: ignore[union-attr]
        assert os.path.isabs(str(stored[0].path))  # type: ignore[union-attr]

        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        os.chdir(elsewhere)
        _emit("agent-after-chdir")

        # One run, one file, in the directory that was current at init().
        assert (start / "relative" / SPANS_FILENAME).exists()
        assert not (elsewhere / "relative").exists()
    finally:
        os.chdir(original)


def test_an_equal_path_spelled_differently_is_not_a_config_conflict(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``File("/a")`` and ``File(Path("/a"))`` are the same destination.

    ``_Config`` equality drives the repeat-``init()`` warning, so comparing paths
    unresolved warned about a conflict that did not exist.
    """
    convergent.init(release="r1", destinations=[convergent.File(str(tmp_path))])
    with caplog.at_level(logging.WARNING, logger="convergent.sdk"):
        convergent.init(release="r1", destinations=[convergent.File(tmp_path)])

    assert not [r for r in caplog.records if "already configured" in r.getMessage()], (
        "a str and a Path naming one directory are the same destination"
    )


def test_a_third_party_processor_with_an_inner_attribute_is_not_unwrapped() -> None:
    """``pending_spans`` names our own class instead of duck-typing ``.inner``.

    A duck-typed lookup would reach into any processor that happens to expose an
    ``inner``, reading a queue that is not the one being flushed.
    """

    class Decoy:
        inner = SimpleNamespace(_batch_processor=SimpleNamespace(_queue=[1, 2, 3]))
        _batch_processor = SimpleNamespace(_queue=[1])

    assert _transport.pending_spans(cast("Any", Decoy())) == 1


def test_spans_file_is_created_owner_only(tmp_path: Path) -> None:
    convergent.init(release="r1", destinations=[convergent.File(tmp_path)])
    _emit()

    target = tmp_path / SPANS_FILENAME
    mode = stat.S_IMODE(target.stat().st_mode)
    assert mode == 0o600, f"expected owner-only, got {oct(mode)}"


def test_the_file_mode_asked_for_lands_on_the_file(tmp_path: Path) -> None:
    """The default is owner read and write, and a caller can widen it. A directory
    shared with a collector running as another user needs group read, and nothing
    else in the SDK can grant it.
    """
    convergent.init(release="r1", destinations=[convergent.File(tmp_path, mode=0o640)])
    _emit()

    assert stat.S_IMODE((tmp_path / SPANS_FILENAME).stat().st_mode) == 0o640


def test_two_file_modes_for_one_path_are_still_one_exporter(tmp_path: Path) -> None:
    """Deduplication keys on the file, not on the whole destination. Two exporters
    on one file append every span twice.
    """
    convergent.init(
        release="r1",
        destinations=[convergent.File(tmp_path), convergent.File(tmp_path, mode=0o640)],
    )
    _emit()

    assert len(_lines((tmp_path / SPANS_FILENAME).read_text())) == 1


def test_a_pre_existing_world_readable_spans_file_is_tightened(tmp_path: Path) -> None:
    """The upgrade path. O_CREAT does not change the mode of a file that already
    exists, so a box that ran before this default changed would keep appending
    prompts to a 0o644 file.
    """
    target = tmp_path / SPANS_FILENAME
    target.write_text("")
    target.chmod(0o644)

    convergent.init(release="r1", destinations=[convergent.File(tmp_path)])
    _emit()

    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_file_filename_lets_two_processes_share_one_directory(tmp_path: Path) -> None:
    convergent.init(
        release="r1", destinations=[convergent.File(tmp_path, filename="worker-1.jsonl")]
    )
    _emit()

    assert _lines((tmp_path / "worker-1.jsonl").read_text())
    assert not (tmp_path / SPANS_FILENAME).exists()


def test_the_spans_dir_environment_variable_is_shorthand_for_a_file_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CONVERGENT_SPANS_DIR", str(tmp_path))
    status = convergent.init(release="r1")
    _emit()

    assert _lines((tmp_path / SPANS_FILENAME).read_text())
    assert status.destinations == [f"file:{tmp_path / SPANS_FILENAME}"]


def test_the_status_reports_every_resolved_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CONVERGENT_API_KEY", "k")
    monkeypatch.setenv("CONVERGENT_ENDPOINT", "http://127.0.0.1:1")

    status = convergent.init(
        release="r1",
        destinations=[convergent.File(tmp_path), convergent.Console(stream="stderr")],
    )

    assert status.destinations == [
        "convergent",
        f"file:{tmp_path / SPANS_FILENAME}",
        "console:stderr",
    ]


def test_flush_after_a_clean_provider_teardown_still_reports_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A closed exporter has nothing buffered, so flushing it trivially succeeded.

    The closed processor stays in ``_core._drain``, and ``BatchSpanProcessor``
    answers False once shut down -- so a process that tore its provider down
    cleanly, then called ``flush()``, would be told the flush failed.
    """
    monkeypatch.setenv("CONVERGENT_API_KEY", "k")
    monkeypatch.setenv("CONVERGENT_ENDPOINT", "http://127.0.0.1:1")
    convergent.init(release="r1")

    provider = _core.active_provider()
    assert provider is not None
    provider.shutdown()

    assert convergent.flush(timeout_ms=200).ok is True


def test_the_destination_alias_is_importable() -> None:
    """A caller annotating their own list needs the alias, so it is public."""
    assert convergent.Destination is not None
    assert "Destination" in convergent.__all__


def test_flush_reports_what_it_did() -> None:
    convergent.init(release="r1", destinations=[convergent.Console()])
    with convergent.span(name="a", operation="agent_run"):
        pass

    result = convergent.flush(timeout_ms=5_000)
    assert result.ok is True
    assert result.pending == 0
    assert result.elapsed_ms >= 0
    assert bool(result) is True, "if flush(): must keep working"


def _inside_span_warnings(caplog: pytest.LogCaptureFixture) -> int:
    return len([r for r in caplog.records if "flushed inside a traced function" in r.getMessage()])


def test_flush_inside_a_traced_function_warns(caplog: pytest.LogCaptureFixture) -> None:
    """A flush written at the end of an ``observe()`` body misses that run's span.

    The agent span has not ended when the drain happens, so it is not in the batch,
    and the spans file the caller collects holds the model and tool spans without
    the run they belong to. Writing the file exists to stop exactly that, so it is
    not allowed to happen quietly.
    """
    convergent.init(release="r1", destinations=[convergent.Console()])

    @convergent.observe(name="support-agent", operation="agent_run")
    def run() -> str:
        convergent.flush(timeout_ms=200)
        return "done"

    with caplog.at_level(logging.WARNING, logger="convergent.sdk"):
        assert run() == "done"

    assert _inside_span_warnings(caplog) == 1


def test_flush_after_the_traced_function_returns_does_not_warn(
    caplog: pytest.LogCaptureFixture,
) -> None:
    convergent.init(release="r1", destinations=[convergent.Console()])

    @convergent.observe(name="support-agent", operation="agent_run")
    def run() -> None:
        pass

    with caplog.at_level(logging.WARNING, logger="convergent.sdk"):
        run()
        convergent.flush(timeout_ms=200)

    assert _inside_span_warnings(caplog) == 0


def test_a_traced_body_that_raises_leaves_no_span_count_behind(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A leaked count would make every later ``flush()`` in this context warn."""
    convergent.init(release="r1", destinations=[convergent.Console()])

    @convergent.observe(name="support-agent", operation="agent_run")
    def run() -> None:
        raise RuntimeError("boom")

    with caplog.at_level(logging.WARNING, logger="convergent.sdk"):
        with pytest.raises(RuntimeError):
            run()
        convergent.flush(timeout_ms=200)

    assert _inside_span_warnings(caplog) == 0


def test_flush_between_two_yields_of_a_traced_generator_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A generator's span stays open across the whole iteration, not one ``next()``."""
    convergent.init(release="r1", destinations=[convergent.Console()])

    @convergent.observe(name="support-agent", operation="agent_run")
    def run() -> Iterator[int]:
        yield 1
        yield 2

    with caplog.at_level(logging.WARNING, logger="convergent.sdk"):
        items = run()
        assert next(items) == 1
        convergent.flush(timeout_ms=200)
        assert list(items) == [2]

    assert _inside_span_warnings(caplog) == 1


def test_flush_inside_a_traced_coroutine_warns(caplog: pytest.LogCaptureFixture) -> None:
    """The task awaiting the body carries the count, so a flush in it reads one."""
    convergent.init(release="r1", destinations=[convergent.Console()])

    @convergent.observe(name="support-agent", operation="agent_run")
    async def run() -> str:
        convergent.flush(timeout_ms=200)
        return "done"

    with caplog.at_level(logging.WARNING, logger="convergent.sdk"):
        assert asyncio.run(run()) == "done"

    assert _inside_span_warnings(caplog) == 1


def test_flush_inside_a_traced_async_generator_warns(caplog: pytest.LogCaptureFixture) -> None:
    """The messiest of the four wrapper shapes to finalize, so it gets its own test."""
    convergent.init(release="r1", destinations=[convergent.Console()])

    @convergent.observe(name="support-agent", operation="agent_run")
    async def run() -> AsyncIterator[int]:
        yield 1
        convergent.flush(timeout_ms=200)
        yield 2

    async def consume() -> list[int]:
        return [item async for item in run()]

    with caplog.at_level(logging.WARNING, logger="convergent.sdk"):
        assert asyncio.run(consume()) == [1, 2]

    assert _inside_span_warnings(caplog) == 1


def test_a_flush_on_another_thread_than_the_span_does_not_warn(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """This is why the count is a ContextVar and not a plain process-wide int.

    A service whose spans and whose flush live on different threads is doing nothing
    wrong. A process-wide count would warn at it on every flush.
    """
    convergent.init(release="r1", destinations=[convergent.Console()])

    with caplog.at_level(logging.WARNING, logger="convergent.sdk"):
        with convergent.span(name="support-agent", operation="agent_run"):
            with ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(convergent.flush, 200).result(10)

    assert _inside_span_warnings(caplog) == 0


def test_a_span_closed_in_a_context_that_never_opened_one_leaves_the_count_usable(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The clamp on the decrement, and what goes wrong without it.

    Advancing a traced generator inside a copied context raises the count there, and
    closing it out here lowers it here. Unclamped this context reaches -1, the next
    genuinely open span only brings it back to 0, and the flush that should warn
    stays quiet.
    """
    convergent.init(release="r1", destinations=[convergent.Console()])

    @convergent.observe(name="support-agent", operation="agent_run")
    def counted() -> Generator[int, None, None]:
        yield 1
        yield 2

    def start() -> Generator[int, None, None]:
        items = counted()
        next(items)
        return items

    copy_context().run(start).close()

    with caplog.at_level(logging.WARNING, logger="convergent.sdk"):
        with convergent.span(name="support-agent", operation="agent_run"):
            convergent.flush(timeout_ms=200)

    assert _inside_span_warnings(caplog) == 1


def test_the_exit_drain_does_not_warn_about_an_open_span(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Interpreter exit drains each processor directly instead of calling ``flush()``.

    A program being torn down with a span still on the stack has nothing to fix, so
    the warning would be noise on a correct program.
    """
    convergent.init(release="r1", destinations=[convergent.Console()])

    with caplog.at_level(logging.WARNING, logger="convergent.sdk"):
        with convergent.span(name="support-agent", operation="agent_run"):
            _core._drain_all_processors()

    assert _inside_span_warnings(caplog) == 0


def test_a_span_produced_during_the_flush_does_not_report_failure(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``ok`` answers "did the flush succeed", not "is the queue empty now".

    ``pending`` is sampled after the last ``force_flush`` returns, so a live
    process always has spans queued again by then. Folding that into ``ok``
    reported failure for a flush that worked, and logged a warning on every call
    in a service that flushes per request.
    """
    convergent.init(release="r1", destinations=[convergent.Console()])
    # Every force_flush succeeds, and the queue is non-empty when sampled after.
    # Summed across processors, so assert it is reported rather than its exact value.
    monkeypatch.setattr(_transport, "pending_spans", lambda _processor: 3)

    with caplog.at_level(logging.WARNING, logger="convergent.sdk"):
        result = convergent.flush(timeout_ms=5_000)

    assert result.ok is True, "a successful flush is not a failure just because work arrived"
    assert result.pending > 0, "the count is still reported, as the informational half"
    assert not [r for r in caplog.records if "left" in r.getMessage()]


def test_flush_with_nothing_configured_is_a_clean_no_op() -> None:
    result = convergent.flush()
    assert result.ok is True
    assert result.pending == 0
    assert result.dropped == 0


def test_flush_reports_the_spans_a_full_queue_threw_away(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OpenTelemetry throws away the oldest span once a processor's queue is full,
    and no ``force_flush`` result says so. ``dropped`` is where a caller sees it.

    The exporter here blocks until the test releases it, so the drop happens on
    demand instead of the test waiting for one.
    """
    monkeypatch.setenv("OTEL_BSP_MAX_QUEUE_SIZE", "2")
    monkeypatch.setenv("OTEL_BSP_MAX_EXPORT_BATCH_SIZE", "1")
    exporting = Event()
    finish = Event()

    class Blocked(SpanExporter):
        def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
            exporting.set()
            finish.wait(10)
            return SpanExportResult.SUCCESS

        def shutdown(self) -> None:
            finish.set()

    monkeypatch.setattr(
        _transport, "build_processor", lambda **_: _transport.batch_processor(Blocked())
    )
    convergent.init(api_key="k", endpoint="http://127.0.0.1:1", release="r1")

    tracer = convergent.tracer_provider().get_tracer("test")
    tracer.start_span("first").end()
    assert exporting.wait(10), "the exporter has to be holding the queue for a drop to happen"
    for index in range(20):
        tracer.start_span(f"span-{index}").end()
    finish.set()

    assert convergent.flush(timeout_ms=5_000).dropped > 0
    assert convergent.flush().dropped == 0, "dropped counts since the last flush"


# --------------------------------------------------------------------------
# The undiagnosable 401
# --------------------------------------------------------------------------


def test_authorization_header_in_otel_env_is_reported(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """OTEL_EXPORTER_OTLP_HEADERS lands on top of our bearer token, so every
    export comes back 401 and nothing in the failure names the variable.
    """
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "authorization=Bearer someone-elses-key")
    with caplog.at_level(logging.WARNING, logger="convergent.sdk"):
        _transport.warn_on_conflicting_auth_header()

    assert any("OTEL_EXPORTER_OTLP_HEADERS" in record.message for record in caplog.records)


@pytest.mark.parametrize(
    "value",
    ["x-tenant=acme", "", "authorisation=nope"],
)
def test_unrelated_otel_headers_are_not_reported(
    value: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", value)
    with caplog.at_level(logging.WARNING, logger="convergent.sdk"):
        _transport.warn_on_conflicting_auth_header()

    assert not [r for r in caplog.records if "OTEL_EXPORTER_OTLP_HEADERS" in r.message]


def test_authorization_header_is_matched_case_insensitively(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "x-a=1, Authorization =Bearer k")
    with caplog.at_level(logging.WARNING, logger="convergent.sdk"):
        _transport.warn_on_conflicting_auth_header()

    assert any("OTEL_EXPORTER_OTLP_HEADERS" in record.message for record in caplog.records)


def test_os_umask_does_not_widen_the_spans_file(tmp_path: Path) -> None:
    """fchmod is exact, so the mode holds whatever umask the process has."""
    previous = os.umask(0o000)
    try:
        convergent.init(release="r1", destinations=[convergent.File(tmp_path)])
        _emit()
    finally:
        os.umask(previous)

    assert stat.S_IMODE((tmp_path / SPANS_FILENAME).stat().st_mode) == 0o600
