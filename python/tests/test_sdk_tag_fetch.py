"""The workspace tag fetch: when the SDK asks ``GET /v1/check``, retries, pauses and stops."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import convergent
from convergent import _core, _registry, _tag_fetch, _transport
from convergent._user_agent import USER_AGENT

#: The real fetch, read at import: tests/conftest.py stubs it out for every test.
_FETCH_WORKSPACE_TAG = _tag_fetch.fetch_workspace_tag
_PROCESS_ENV = {**os.environ, "PYTHONPATH": str(Path(convergent.__file__).parents[1])}


_TAG_PENDING = r"""
import json
import os
import threading
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
import convergent
from convergent import _registry, _tag_fetch, _transport

answer = threading.Event()
asked = []
parent = os.getpid()

def check(url, headers, **kwargs):
    asked.append((os.getpid(), url, headers))
    if os.getpid() == parent:
        assert answer.wait(timeout=10)
    return {"workspace_tag": "aaaaaaaa"}

def joined():
    (thread := _tag_fetch._state.thread) and thread.join(timeout=5)

_registry.get_json = check
_transport.build_processor = lambda **kwargs: SimpleSpanProcessor(InMemorySpanExporter())
convergent.init(
    api_key="test-key",  # pragma: allowlist secret
    endpoint="https://example.test",
    release="test",
)
__SCENARIO__
"""


def _tag_pending(scenario: str) -> dict[str, Any]:
    result = subprocess.run(
        [sys.executable, "-c", _TAG_PENDING.replace("__SCENARIO__", scenario)],
        env=_PROCESS_ENV,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return json.loads(result.stdout.splitlines()[-1])


def test_init_returns_before_the_tag_arrives_and_holds_it_once_it_does() -> None:
    """init() returns while ``GET /v1/check`` is still out; the tag reads None until it answers."""
    result = _tag_pending(
        """
before = _tag_fetch._current_tag()
answer.set()
joined()
after = _tag_fetch._current_tag()
print(json.dumps({"before": before, "after": after, "asked": [a[1:] for a in asked]}))
"""
    )
    assert result["before"] is None
    assert result["after"] == "aaaaaaaa"
    assert result["asked"] == [
        [
            "https://example.test/v1/check",
            {"Authorization": "Bearer test-key", "user-agent": USER_AGENT},
        ]
    ], "one request, made once, with the SDK's user agent"


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires fork")
def test_a_child_forked_before_the_tag_arrived_fetches_its_own() -> None:
    """The parent's request has no thread in the child, so the child asks for itself."""
    result = _tag_pending(
        """
while not asked:  # fork only once the parent's request is out
    threading.Event().wait(0.01)
pid = os.fork()
if pid == 0:
    first = _tag_fetch._current_tag()
    joined()
    then = _tag_fetch._current_tag()
    mine = sum(1 for who, *_ in asked if who == os.getpid())
    print(json.dumps({"first": first, "then": then, "asked_by_child": mine}), flush=True)
    os._exit(0)
os.waitpid(pid, 0)
answer.set()
"""
    )
    assert result == {"first": None, "then": "aaaaaaaa", "asked_by_child": 1}


@pytest.fixture
def _no_tag_yet(monkeypatch: pytest.MonkeyPatch) -> None:
    """A process that has not fetched its tag, retrying without waiting."""
    monkeypatch.setattr(_tag_fetch, "fetch_workspace_tag", _FETCH_WORKSPACE_TAG)
    monkeypatch.setattr(_tag_fetch._state, "tag", None)
    monkeypatch.setattr(_tag_fetch._state, "request", None)
    monkeypatch.setattr(_tag_fetch._state, "thread", None)
    monkeypatch.setattr(_core, "_warned", set())
    monkeypatch.setattr(_tag_fetch._state, "paused_until", 0.0)
    monkeypatch.setattr(_tag_fetch, "time", _Clock())


class _Clock:
    """The module's clock, moved by hand: ``monotonic`` reads ``now``, ``sleep`` returns."""

    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        pass


def _await_tag() -> str | None:
    thread = _tag_fetch._state.thread
    if thread is not None:
        thread.join(timeout=5)
        assert not thread.is_alive(), "the tag fetch did not end within 5 s"
    return _tag_fetch._current_tag()


@pytest.mark.usefixtures("_no_tag_yet")
def test_a_failed_tag_fetch_warns_once_and_a_retry_turns_propagation_on(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One timeout at startup must not turn propagation off for the process quietly."""
    answers: list[object] = [
        _registry.RegistrationError("TimeoutError"),
        _registry.RegistrationError("TimeoutError"),
        {"workspace_tag": "aaaaaaaa"},
    ]

    def check(url: str, *, headers: dict[str, str], **kwargs: object) -> dict[str, object]:
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        assert isinstance(answer, dict)
        return answer

    monkeypatch.setattr(_registry, "get_json", check)
    with caplog.at_level("WARNING", logger="convergent.sdk"):
        _tag_fetch.fetch_workspace_tag("https://example.test", "test-key")
        tag = _await_tag()

    assert tag == "aaaaaaaa"
    warnings = [
        record.getMessage() for record in caplog.records if "workspace tag" in record.getMessage()
    ]
    assert len(warnings) == 1
    assert "TimeoutError" in warnings[0]
    assert "test-key" not in warnings[0]


def _tag_warnings(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch, answers: list[object]
) -> tuple[str | None, list[str]]:
    def check(url: str, *, headers: dict[str, str], **kwargs: object) -> dict[str, object]:
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        assert isinstance(answer, dict)
        return answer

    monkeypatch.setattr(_registry, "get_json", check)
    with caplog.at_level("WARNING", logger="convergent.sdk"):
        _tag_fetch.fetch_workspace_tag("https://example.test", "test-key")
        tag = _await_tag()
    return tag, [
        record.getMessage()
        for record in caplog.records
        if record.levelname == "WARNING" and "workspace tag" in record.getMessage()
    ]


def _pause_over(seconds: float = _tag_fetch._PAUSE_S) -> str | None:
    """The tag once ``seconds`` have passed and the next use asked again."""
    clock = _tag_fetch.time
    assert isinstance(clock, _Clock)
    clock.now += seconds
    _tag_fetch._current_tag()
    return _await_tag()


@pytest.mark.usefixtures("_no_tag_yet")
def test_a_failed_tag_fetch_asks_again_after_the_pause(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two warnings for the process, then the next use after 10 minutes asks again."""
    timeout = _registry.RegistrationError("TimeoutError")
    answers: list[object] = [timeout, timeout, timeout, {"workspace_tag": "aaaaaaaa"}]
    tag, warnings = _tag_warnings(caplog, monkeypatch, answers)

    assert tag is None
    assert len(warnings) == 2
    assert "will retry" in warnings[0]
    assert "ask again in 10 minutes" in warnings[1]
    assert _pause_over(_tag_fetch._PAUSE_S - 1) is None
    assert _tag_fetch._state.thread is None, "no fetch starts during the pause"
    assert len(answers) == 1
    assert _pause_over(1) == "aaaaaaaa"
    assert answers == []


@pytest.mark.usefixtures("_no_tag_yet")
def test_a_status_not_worth_retrying_pauses_at_once(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 404 or 403 will not change in seconds: no retries, one warning, then the pause."""
    answers: list[object] = [_registry.RegistrationError("http_404"), {"workspace_tag": "aaaaaaaa"}]
    tag, warnings = _tag_warnings(caplog, monkeypatch, answers)

    assert tag is None
    assert len(answers) == 1, "not retried"
    assert len(warnings) == 1
    assert "http_404" in warnings[0]
    assert "ask again in 10 minutes" in warnings[0]
    assert _pause_over() == "aaaaaaaa"


@pytest.mark.usefixtures("_no_tag_yet")
def test_an_answer_without_a_tag_asks_again_after_the_pause_quietly(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A workspace with propagation off answers without a tag; turning it back on
    needs no restart."""
    answers: list[object] = [{}, {"workspace_tag": "aaaaaaaa"}]
    tag, warnings = _tag_warnings(caplog, monkeypatch, answers)

    assert tag is None
    assert warnings == []
    assert _pause_over() == "aaaaaaaa"


@pytest.mark.usefixtures("_no_tag_yet")
def test_a_held_tag_is_checked_again_every_pause(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A workspace turned off reaches a running process at its next check: the
    tag is asked for again every 10 minutes while held. A failed check keeps
    the tag quietly; only an answer without one, or a 401, drops it."""
    timeout = _registry.RegistrationError("TimeoutError")
    answers: list[object] = [
        {"workspace_tag": "aaaaaaaa"},
        timeout,
        {},
        {"workspace_tag": "aaaaaaaa"},
        _registry.RegistrationError("http_401"),
        {"workspace_tag": "aaaaaaaa"},
    ]
    tag, warnings = _tag_warnings(caplog, monkeypatch, answers)

    assert tag == "aaaaaaaa"
    assert _pause_over(_tag_fetch._PAUSE_S - 1) == "aaaaaaaa"
    assert len(answers) == 5, "no check during the pause"
    assert _pause_over(1) == "aaaaaaaa", "a failed check keeps the tag"
    assert len(answers) == 4, "one attempt, no retries, while a tag is held"
    assert _pause_over() is None, "an answer without a tag turns the entry off"
    assert _pause_over() == "aaaaaaaa"
    assert _pause_over() is None, "a refused key drops the tag"
    assert _pause_over() is None
    assert len(answers) == 1, "and stops asking"
    assert warnings == []
    later = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert len(later) == 1
    assert "will not ask again" in later[0]


@pytest.mark.usefixtures("_no_tag_yet")
def test_a_malformed_tag_warns_once(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    tag, warnings = _tag_warnings(caplog, monkeypatch, [{"workspace_tag": "not-a-tag"}])

    assert tag is None
    assert len(warnings) == 1
    assert "malformed" in warnings[0]


@pytest.mark.usefixtures("_no_tag_yet")
def test_a_tag_fetch_that_raises_clears_its_thread_and_pauses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An error the fetch does not catch still ends it and still pauses, so the
    next use after the pause asks again, and no use before it starts a thread."""
    monkeypatch.setattr(_registry, "get_json", lambda *a, **k: {"workspace_tag": "aaaaaaaa"})
    escaped: list[type[BaseException]] = []
    monkeypatch.setattr(threading, "excepthook", lambda args: escaped.append(args.exc_type))
    clock = _tag_fetch.time
    assert isinstance(clock, _Clock)
    sleeps: list[float] = []
    go = threading.Event()

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 1:
            assert go.wait(timeout=5)
            raise RuntimeError("unexpected")

    monkeypatch.setattr(clock, "sleep", sleep)
    _tag_fetch.fetch_workspace_tag("https://example.test", "test-key")
    thread = _tag_fetch._state.thread
    assert thread is not None
    go.set()
    thread.join(timeout=5)
    assert escaped == [RuntimeError]
    assert _tag_fetch._state.thread is None
    assert _tag_fetch._current_tag() is None
    assert _await_tag() is None, "no fetch starts during the pause"
    assert _pause_over() == "aaaaaaaa"


@pytest.mark.usefixtures("_no_tag_yet")
def test_an_abandoned_install_forgets_its_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """install() on a provider that refuses the processor leaves no request behind:
    the next setup asks with its own key, and the abandoned fetch's answer is dropped."""
    _core._reset_for_tests()
    asked: list[str] = []
    fetches: list[threading.Thread] = []
    answer = threading.Event()

    def check(url: str, *, headers: dict[str, str], **kwargs: object) -> dict[str, object]:
        asked.append(headers["Authorization"])
        fetches.append(threading.current_thread())
        if len(asked) == 1:
            assert answer.wait(timeout=30)
            return {"workspace_tag": "aaaaaaaa"}
        return {"workspace_tag": "bbbbbbbb"}

    monkeypatch.setattr(_registry, "get_json", check)
    monkeypatch.setattr(
        _transport, "build_processor", lambda **_: SimpleSpanProcessor(InMemorySpanExporter())
    )
    refusing = TracerProvider()

    def refuse(*_: object, **__: object) -> None:
        raise RuntimeError("provider is shut down")

    monkeypatch.setattr(refusing, "add_span_processor", refuse)
    try:
        convergent.otel.install(
            refusing,
            api_key="key-a",  # pragma: allowlist secret
            endpoint="https://example.test",
            release="r1",
        )
        convergent.otel.install(
            TracerProvider(),
            api_key="key-b",  # pragma: allowlist secret
            endpoint="https://example.test",
            release="r1",
        )
        tag = _await_tag()
        answer.set()
        for fetch in fetches:
            fetch.join(timeout=5)
        assert asked == ["Bearer key-a", "Bearer key-b"]
        assert tag == "bbbbbbbb"
        assert _tag_fetch._current_tag() == "bbbbbbbb", "the abandoned fetch's tag is dropped"
    finally:
        answer.set()
        _core._reset_for_tests()
