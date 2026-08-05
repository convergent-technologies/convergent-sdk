"""Process-lifecycle guards for the SDK: a fork must not inherit a held lock,
the exit drain must stay inside one budget, and a normal interpreter exit must
flush what is still queued."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import convergent
from convergent import _core, _otel, _semantic, _transport


@pytest.fixture(autouse=True)
def reset_sdk() -> Iterator[None]:
    _core._reset_for_tests()
    yield
    _core._reset_for_tests()


def _armed_processor(monkeypatch: pytest.MonkeyPatch) -> _otel.ConvergentSpanProcessor:
    """A processor with the credentials it needs, and nothing on the network."""
    monkeypatch.setattr(
        _transport, "build_processor", lambda **_: SimpleSpanProcessor(InMemorySpanExporter())
    )
    return _otel.ConvergentSpanProcessor(api_key="k-fork", release="r1")  # pragma: allowlist secret


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires fork")
@pytest.mark.filterwarnings("ignore:.*multi-threaded.*fork.*:DeprecationWarning")
def test_forked_child_does_not_inherit_a_held_lock() -> None:
    """A child forked while another thread held _lock must still be able to take
    it. Without the after-fork reset the lock arrives locked with no thread left
    to release it, and the child blocks forever.

    The holder has to be a *second* thread: an RLock records its owner by thread
    ident, so a lock held by the forking thread itself would simply be re-entered
    in the child and would never reproduce the hang.
    """
    holding = threading.Event()
    release = threading.Event()

    def _hold() -> None:
        with _core._lock:
            holding.set()
            release.wait(timeout=30)

    holder = threading.Thread(target=_hold, daemon=True)
    holder.start()
    assert holding.wait(timeout=5)

    pid = os.fork()
    if pid == 0:
        # Child: never raise out of here, and never run pytest teardown.
        try:
            _core.snapshot()
            os._exit(0)
        except BaseException:
            os._exit(2)

    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            waited, status = os.waitpid(pid, os.WNOHANG)
            if waited == pid:
                assert os.waitstatus_to_exitcode(status) == 0
                return
            time.sleep(0.05)
        os.kill(pid, 9)
        os.waitpid(pid, 0)
        pytest.fail("forked child deadlocked on _lock")
    finally:
        release.set()
        holder.join(timeout=5)


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires fork")
@pytest.mark.filterwarnings("ignore:.*multi-threaded.*fork.*:DeprecationWarning")
def test_a_forked_child_does_not_inherit_an_open_span(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A child forked inside an observed function inherits the open-span count but
    not the frame that would end that span, so its own correct ``flush()`` would
    report an enclosing span the child does not have. Forking inside an observed
    function and flushing in the child is a documented pattern, so the child forgets
    the count it inherited.
    """
    for name in ("CONVERGENT_API_KEY", "CONVERGENT_ENDPOINT", "CONVERGENT_SPANS_DIR"):
        monkeypatch.delenv(name, raising=False)
    convergent.init(release="r1", destinations=[convergent.File(str(tmp_path))])

    with convergent.span(name="support-agent", operation="agent_run"):
        assert _semantic.has_open_span(), "the parent has to be inside a span to fork from"
        pid = os.fork()
        if pid == 0:
            # Child: never raise out of here, and never run pytest teardown.
            try:
                os._exit(1 if _semantic.has_open_span() else 0)
            except BaseException:
                os._exit(2)
        _, status = os.waitpid(pid, 0)

    assert os.waitstatus_to_exitcode(status) == 0, "the child inherited the parent's count"


def test_normal_interpreter_exit_flushes_pending_spans(tmp_path: Path) -> None:
    """A process that ends a span and exits normally without calling flush()
    must still get that span out, at the default 5s batch delay.

    Slow (subprocess + Python startup), but pytest's own process never exits
    between tests, so this is the only way to run the real atexit path.
    """
    spans_dir = tmp_path / "spans"
    script = tmp_path / "short_lived.py"
    script.write_text(
        textwrap.dedent("""
        import convergent

        convergent.init(release="atexit-test")

        with convergent.span(name="short-lived-work", operation="workflow"):
            pass
        # No flush(), no shutdown -- interpreter exit must drain the batch.
        """)
    )
    env = dict(os.environ, CONVERGENT_SPANS_DIR=str(spans_dir))
    for name in ("CONVERGENT_API_KEY", "CONVERGENT_ENDPOINT"):
        env.pop(name, None)
    env["PYTHONPATH"] = str(Path(convergent.__file__).parents[1])
    result = subprocess.run(  # noqa: S603 -- child is our own script
        [sys.executable, str(script)],
        env=env,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr.decode()
    spans_file = spans_dir / "spans.jsonl"
    assert spans_file.exists(), "the exit drain did not flush the span"
    names = [
        span["name"]
        for line in spans_file.read_text().splitlines()
        if line.strip()
        for resource in json.loads(line)["resourceSpans"]
        for scope in resource["scopeSpans"]
        for span in scope["spans"]
    ]
    assert "short-lived-work" in names


def test_exit_drain_shares_one_budget_across_processors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each processor gets what is left of a single budget, not a fresh one, so a
    process that traced many agents cannot stall exit by budget x processors."""
    monkeypatch.setattr(_core, "_DRAIN_BUDGET_S", 0.3)
    granted: list[int] = []

    class _Slow:
        def force_flush(self, timeout_millis: int | None = None) -> bool:
            assert isinstance(timeout_millis, int), "the drain always passes a budget"
            granted.append(timeout_millis)
            time.sleep(0.15)
            return True

        def shutdown(self) -> None: ...

    monkeypatch.setattr(_core, "_processors", [_Slow(), _Slow(), _Slow()])

    _core._drain_all_processors()

    assert len(granted) == 3
    assert granted[0] > 0
    assert granted == sorted(granted, reverse=True), f"budget must shrink, got {granted}"
    assert granted[-1] == 0, "an exhausted budget grants nothing, it does not reset"


def test_exit_drain_still_shuts_every_processor_down_after_the_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An exhausted flush budget must not skip shutdown -- the exporter still has
    to release its thread and socket."""
    monkeypatch.setattr(_core, "_DRAIN_BUDGET_S", 0.0)
    shutdowns: list[int] = []

    class _Recorder:
        def __init__(self, index: int) -> None:
            self._index = index

        def force_flush(self, timeout_millis: int | None = None) -> bool:
            return True

        def shutdown(self) -> None:
            shutdowns.append(self._index)

    monkeypatch.setattr(_core, "_processors", [_Recorder(0), _Recorder(1), _Recorder(2)])

    _core._drain_all_processors()

    assert shutdowns == [0, 1, 2]


def test_lock_reset_is_registered_at_import() -> None:
    """The hook is installed at import, not from init(), so a process that forks
    before configuring tracing is covered too."""
    assert hasattr(_core, "_reset_lock_after_fork")
    if sys.platform != "win32":
        assert hasattr(os, "register_at_fork")


def test_lock_reset_swaps_in_a_usable_lock() -> None:
    original = _core._lock
    try:
        original.acquire()
        _core._reset_lock_after_fork()
        assert _core._lock is not original
        assert _core._lock.acquire(timeout=1), "the replacement lock is already held"
        _core._lock.release()
    finally:
        original.release()
        _core._lock = original


def test_a_span_processor_is_repaired_after_fork_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """A child that inherited the gate locked would block on every span start. It
    also inherits the flag saying registration is under way, and the thread running
    that registration does not exist in the child, so the child gets to start its
    own."""
    processor = _armed_processor(monkeypatch)
    gate = processor._gate
    gate.acquire()
    processor._registration_started = True

    _otel._reset_after_fork()

    assert processor._gate is not gate
    assert processor._gate.acquire(timeout=1), "the replacement is already held"
    processor._gate.release()
    assert not processor._registration_started, "the child registers for itself"
    gate.release()


def test_a_fork_after_registration_landed_does_not_register_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The deployment is already known, and it is the same one in the child."""
    processor = _armed_processor(monkeypatch)
    processor._registration_started = True
    processor._registered = True

    _otel._reset_after_fork()

    assert processor._registration_started


def test_the_transport_locks_are_reset_after_fork_too() -> None:
    """``_transport`` has its own module locks, on the child's entry paths.

    A forked worker calling ``init()`` acquires ``_metrics_gate`` to build a batch
    processor, ``flush()`` acquires ``_lost_lock``, and the drop counter takes its
    own lock on the same path, so any of them held at fork time hangs the child
    rather than failing loudly. ``_core`` already guards its own lock this way, and
    ``docs/configuration.md`` states it as a guarantee, so this module needs the
    same treatment.
    """
    assert hasattr(_transport, "_reset_locks_after_fork")

    gate = _transport._metrics_gate
    lost_lock = _transport._lost_lock
    drop_lock = _transport._drops._lock
    try:
        gate.acquire()
        lost_lock.acquire()
        drop_lock.acquire()
        _transport._reset_locks_after_fork()
        assert _transport._metrics_gate is not gate
        assert _transport._metrics_gate.acquire(timeout=1), "the replacement is already held"
        _transport._metrics_gate.release()
        assert _transport._lost_lock is not lost_lock
        assert _transport._lost_lock.acquire(timeout=1), "the replacement is already held"
        _transport._lost_lock.release()
        assert _transport._drops._lock is not drop_lock
        assert _transport._drops._lock.acquire(timeout=1), "the replacement is already held"
        _transport._drops._lock.release()
    finally:
        gate.release()
        lost_lock.release()
        drop_lock.release()
        _transport._metrics_gate = gate
        _transport._lost_lock = lost_lock
        _transport._drops._lock = drop_lock


def test_a_forked_child_does_not_report_the_parents_lost_spans() -> None:
    """The counters are taken and reset, so an uncollected loss belongs to whichever
    process recorded it. A child that kept the parent's count would blame its own
    first ``flush()`` for spans the parent lost, and the parent would still report
    them when it flushed."""
    _transport.lost_spans()
    _transport.dropped_spans()
    try:
        _transport._record_lost(7)
        _transport._drops.add(3, {_transport.ERROR_TYPE: _transport._QUEUE_FULL})
        assert _transport._lost == 7, "the loss is uncollected at fork time"
        assert _transport._drops._count == 3, "the drop is uncollected at fork time"

        _transport._reset_locks_after_fork()

        assert _transport.lost_spans() == 0, "the child starts its own loss count"
        assert _transport.dropped_spans() == 0, "the child starts its own drop count"
    finally:
        _transport.lost_spans()
        _transport.dropped_spans()
