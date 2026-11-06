"""The SDK carries trajectory and session context over OTel carriers."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from collections.abc import Iterator, MutableMapping
from pathlib import Path
from typing import Any

import pytest
from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.sdk.trace import SpanLimits, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

import convergent
from convergent import _core, _processors, _propagation, _tag_fetch
from convergent._propagation import ConvergentPropagator

_PROCESS_ENV = {**os.environ, "PYTHONPATH": str(Path(convergent.__file__).parents[1])}

_PRODUCER = r"""
import json
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
import convergent
from convergent import _processors, _propagation, _tag_fetch

_tag_fetch._state.tag = "aaaaaaaa"
provider = TracerProvider()
for processor in [
    _processors._STAMPER,
    *_processors.wrap(None, None, [SimpleSpanProcessor(InMemorySpanExporter())]),
]:
    provider.add_span_processor(processor)
carrier = {}
with convergent.session("session-from-producer"):
    with provider.get_tracer("producer").start_as_current_span(
        "root-agent", attributes={"gen_ai.operation.name": "invoke_agent"}
    ) as root:
        root_id = f"{root.get_span_context().span_id:016x}"
        convergent.carrier.inject(carrier, session=True)
cases = {"valid": carrier}
cases["missing"] = {key: value for key, value in carrier.items() if key != "tracestate"}
cases["malformed"] = {**carrier, "tracestate": "convergent=!"}
# Cut inside the fixed-width fields, before the session.
cases["incomplete"] = {**carrier, "tracestate": carrier["tracestate"][: len("convergent=") + 30]}
mismatched = dict(carrier)
parts = mismatched["traceparent"].split("-")
parts[2] = "f" * 16 if parts[2] != "f" * 16 else "e" * 16
mismatched["traceparent"] = "-".join(parts)
cases["mismatched"] = mismatched
print(json.dumps({"root_id": root_id, "cases": cases}, sort_keys=True))
"""

_CONSUMER = r"""
import json
import sys
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
import convergent
from convergent import _processors, _propagation, _tag_fetch

_tag_fetch._state.tag = "aaaaaaaa"
payload = json.load(sys.stdin)
provider = TracerProvider()
exporter = InMemorySpanExporter()
wrapped = _processors.wrap(None, None, [SimpleSpanProcessor(exporter)])
for processor in [_processors._STAMPER, *wrapped]:
    provider.add_span_processor(processor)
results = {}
for case, carrier in payload["cases"].items():
    extracted = convergent.carrier.extract(carrier)
    with provider.get_tracer("consumer").start_as_current_span(
        f"agent-{case}", context=extracted, attributes={"gen_ai.operation.name": "invoke_agent"}
    ) as span:
        own = f"{span.get_span_context().span_id:016x}"
    results[case] = {"own": own, **dict(exporter.get_finished_spans()[-1].attributes or {})}
print(json.dumps(results, sort_keys=True))
"""

_HTTP_CONSUMER = r"""
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
import convergent
from convergent import _propagation, _registry, _tag_fetch, _transport

exporter = InMemorySpanExporter()
_registry.get_json = __CHECK__
_transport.build_processor = lambda **kwargs: SimpleSpanProcessor(exporter)
convergent.init(
    api_key="__KEY__",  # pragma: allowlist secret
    endpoint="https://example.test",
    release="test",
)
# A fetch that ends without a tag clears its thread when it pauses.
if (thread := _tag_fetch._state.thread) is not None:
    thread.join(timeout=5)

class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        carrier = {key.lower(): value for key, value in self.headers.items()}
        extracted = convergent.carrier.extract(carrier)
        with convergent.tracer_provider().get_tracer("consumer").start_as_current_span(
            "remote-agent",
            context=extracted,
            attributes={"gen_ai.operation.name": "invoke_agent"},
        ) as span:
            own = f"{span.get_span_context().span_id:016x}"
        attributes = dict(exporter.get_finished_spans()[-1].attributes or {})
        body = json.dumps({"own": own, **attributes}).encode()
        self.send_response(200)
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *args):
        pass

server = HTTPServer(("127.0.0.1", 0), Handler)
print(server.server_port, flush=True)
server.handle_request()
"""

_HTTP_PRODUCER = r"""
import json
import urllib.request
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind
import convergent
from convergent import _propagation, _registry, _tag_fetch, _transport

exporter = InMemorySpanExporter()
_registry.get_json = __CHECK__
_transport.build_processor = lambda **kwargs: SimpleSpanProcessor(exporter)
convergent.init(
    api_key="__KEY__",  # pragma: allowlist secret
    endpoint="https://example.test",
    release="test",
    session_hosts=["127.0.0.1"],
)
# A fetch that ends without a tag clears its thread when it pauses.
if (thread := _tag_fetch._state.thread) is not None:
    thread.join(timeout=5)
tracer = convergent.tracer_provider().get_tracer("producer")
url = "http://127.0.0.1:__PORT__/"
results = {}
with convergent.session("session-from-producer"):
    with tracer.start_as_current_span(
        "root-agent", attributes={"gen_ai.operation.name": "invoke_agent"}
    ) as root:
        root_id = f"{root.get_span_context().span_id:016x}"
        # The CLIENT span names a listed host, so the session goes without session=True.
        with tracer.start_as_current_span(
            "POST", kind=SpanKind.CLIENT, attributes={"url.full": url}
        ):
            carrier = {}
            convergent.carrier.inject(carrier)
            request = urllib.request.Request(url, data=b"", headers=carrier, method="POST")
            with urllib.request.urlopen(request, timeout=5) as response:
                results["valid"] = json.loads(response.read())
print(json.dumps({"root_id": root_id, "cases": results}, sort_keys=True))
"""


def _provider(agents: list[str] | None = None) -> tuple[TracerProvider, InMemorySpanExporter]:
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    wrapped = _processors.wrap(None, agents, [SimpleSpanProcessor(exporter)])
    for processor in [_processors._STAMPER, *wrapped]:
        provider.add_span_processor(processor)
    return provider, exporter


def _member(carrier: dict[str, str]) -> str:
    return next(item for item in carrier["tracestate"].split(",") if item.startswith("convergent="))


def _entry(carrier: dict[str, str]) -> dict[str, Any]:
    """The entry's invocation id ``i``, sender id ``c`` and, when sent, session ``s``."""
    decoded = _propagation._decode(_member(carrier).split("=", 1)[1])
    assert decoded is not None
    stamp, sender_id, session_id, tag = decoded
    assert stamp is not None
    entry = {"i": stamp["convergent.invocation.span_id"], "c": sender_id, "w": tag}
    return entry if session_id is None else {**entry, "s": session_id}


def _assert_received_agents(results: dict[str, dict[str, Any]], sender_id: str) -> None:
    """A valid entry files the receiving agent under the sender. Under any other entry the
    agent has a remote parent it knows nothing about, so it stays unstamped for ingest."""
    valid = results.pop("valid")
    assert valid["convergent.invocation.span_id"] == valid["own"]
    assert valid["convergent.root_invocation.span_id"] == sender_id
    assert valid["convergent.parent_invocation.span_id"] == sender_id
    assert valid["convergent.session.id"] == "session-from-producer"
    for case, received in results.items():
        _assert_unstamped(received, case)


def _assert_unstamped(attributes: dict[str, Any], case: str) -> None:
    for key in (
        "convergent.invocation.span_id",
        "convergent.root_invocation.span_id",
        "convergent.parent_invocation.span_id",
        "convergent.session.id",
    ):
        assert key not in attributes, (case, key)


#: ``GET /v1/check`` answers by key: two keys in one workspace, one in another,
#: and one from a server too old to return a tag.
_CHECK = """(lambda url, headers, **kwargs: {"key": {"organization_id": "org_test"}, **(
    {"workspace_tag": tag}
    if url == "https://example.test/v1/check"
    and (tag := {"key-a1": "aaaaaaaa", "key-a2": "aaaaaaaa", "key-b1": "bbbbbbbb"}.get(
        headers["Authorization"].removeprefix("Bearer ")
    ))
    else {}
)})"""


@pytest.fixture(autouse=True)
def _workspace_tag(monkeypatch: pytest.MonkeyPatch) -> None:
    """In-process tests run as one workspace whose tag has arrived, and start as a
    process that has received no entry, whatever an earlier test accepted."""
    monkeypatch.setattr(_tag_fetch._state, "tag", "aaaaaaaa")
    monkeypatch.setattr(_processors, "_ever_received", False)


def test_two_sdk_processes_preserve_valid_context_and_reject_missing_or_mismatched_entry() -> None:
    """Two independent SDK processes exchange real OTel carriers as a dict handoff."""
    producer = subprocess.run(
        [sys.executable, "-c", _PRODUCER],
        env=_PROCESS_ENV,
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    )
    received = subprocess.run(
        [sys.executable, "-c", _CONSUMER],
        env=_PROCESS_ENV,
        input=producer.stdout,
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    )

    sent = json.loads(producer.stdout)
    results = json.loads(received.stdout)
    assert set(results) == {"valid", "missing", "malformed", "incomplete", "mismatched"}
    _assert_received_agents(results, sent["root_id"])


def test_received_session_survives_local_work_and_local_scope_wins() -> None:
    """A received session goes out again from the receiver's ordinary local work, opted in
    or to a listed host; a live local session scope replaces it."""
    provider, _ = _provider()
    tracer = provider.get_tracer("propagation")
    listed = ConvergentPropagator(TraceContextTextMapPropagator(), session_hosts=["worker.example"])
    try:
        with convergent.session("session-remote"):
            with tracer.start_as_current_span(
                "invoke_agent producer", attributes={"gen_ai.operation.name": "invoke_agent"}
            ):
                incoming: dict[str, str] = {}
                convergent.carrier.inject(incoming, session=True)

        extracted = convergent.carrier.extract(incoming)
        with tracer.start_as_current_span("worker", context=extracted):
            # The receiver's own agent stamps itself, so the session must come from
            # what the worker received rather than from the stamp it inherited.
            with tracer.start_as_current_span(
                "invoke_agent receiver", attributes={"gen_ai.operation.name": "invoke_agent"}
            ):
                downstream: dict[str, str] = {}
                convergent.carrier.inject(downstream, session=True)
                assert _entry(downstream)["s"] == "session-remote"

                with tracer.start_as_current_span(
                    "POST", kind=SpanKind.CLIENT, attributes={"server.address": "worker.example"}
                ):
                    to_listed: dict[str, str] = {}
                    listed.inject(to_listed)
                assert _entry(to_listed)["s"] == "session-remote"

                with convergent.session("session-local"):
                    overridden: dict[str, str] = {}
                    convergent.carrier.inject(overridden, session=True)
                assert _entry(overridden)["s"] == "session-local"

                unopted: dict[str, str] = {}
                convergent.carrier.inject(unopted)
                assert "s" not in _entry(unopted)
    finally:
        provider.shutdown()


@pytest.mark.parametrize(
    ("producer_key", "consumer_key", "accepted"),
    [
        ("key-a1", "key-a2", True),
        ("key-a1", "key-b1", False),
        ("key-a1", "key-old", False),
    ],
    ids=["same-workspace-other-key", "other-workspace", "receiver-without-tag"],
)
def test_two_sdk_processes_propagate_through_real_http(
    producer_key: str, consumer_key: str, accepted: bool
) -> None:
    """A real local HTTP handoff between two SDK processes. The receiver takes the entry
    only when its workspace tag, from the key check, matches the sender's."""
    consumer_code = _HTTP_CONSUMER.replace("__CHECK__", _CHECK).replace("__KEY__", consumer_key)
    consumer = subprocess.Popen(
        [sys.executable, "-c", consumer_code],
        env=_PROCESS_ENV,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert consumer.stdout is not None
    port_line = consumer.stdout.readline()
    assert port_line.strip().isdigit(), consumer.stderr.read() if consumer.stderr else ""
    producer_code = (
        _HTTP_PRODUCER.replace("__PORT__", port_line.strip())
        .replace("__CHECK__", _CHECK)
        .replace("__KEY__", producer_key)
    )
    try:
        producer = subprocess.run(
            [sys.executable, "-c", producer_code],
            env=_PROCESS_ENV,
            capture_output=True,
            text=True,
            check=True,
            timeout=20,
        )
        assert consumer.wait(timeout=20) == 0
    finally:
        if consumer.poll() is None:
            consumer.kill()
            consumer.wait(timeout=5)
    assert consumer.stderr is not None
    assert consumer.stderr.read() == ""
    sent = json.loads(producer.stdout)
    assert set(sent["cases"]) == {"valid"}
    if accepted:
        _assert_received_agents(sent["cases"], sent["root_id"])
    else:
        _assert_unstamped(sent["cases"]["valid"], consumer_key)


#: A short-lived worker: ``init()``, then ``extract()`` and its agents start straight
#: away, while the key check is still out. The check answers only once both agents
#: are running and a tool call under the first has already ended, so every decision
#: the SDK makes at their start, and at that tool call's end, sees no tag.
_EARLY_WORKER = r"""
import json
import sys
import threading
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
import convergent
from convergent import _registry, _tag_fetch, _transport

answer = threading.Event()

def check(url, headers, **kwargs):
    answer.wait(timeout=10)
    if "__TAG__" in ("http_401", "http_503"):
        raise _registry.RegistrationError("__TAG__")
    return {"workspace_tag": "__TAG__"}

exporter = InMemorySpanExporter()
_registry.get_json = check
_transport.build_processor = lambda **kwargs: SimpleSpanProcessor(exporter)
convergent.init(
    api_key="test-key",  # pragma: allowlist secret
    endpoint="https://example.test",
    release="test",
)
payload = json.load(sys.stdin)
extracted = convergent.carrier.extract(payload["cases"]["valid"])
tracer = convergent.tracer_provider().get_tracer("worker")
own = {}
with tracer.start_as_current_span(
    "remote-agent", context=extracted, attributes={"gen_ai.operation.name": "invoke_agent"}
) as agent:
    own["remote-agent"] = f"{agent.get_span_context().span_id:016x}"
    with tracer.start_as_current_span("tool-call"):
        pass
    with tracer.start_as_current_span(
        "nested-agent", attributes={"gen_ai.operation.name": "invoke_agent"}
    ) as nested:
        own["nested-agent"] = f"{nested.get_span_context().span_id:016x}"
        assert _tag_fetch._state.tag is None
        check_thread = _tag_fetch._state.thread
        answer.set()
        check_thread.join(timeout=20)
        outbound = {}
        convergent.carrier.inject(outbound, session=True)
convergent.tracer_provider().force_flush()
spans = {
    span.name: {
        "attributes": dict(span.attributes or {}),
        "exported": dict(_transport._naming_the_sdk(span).attributes or {}),
    }
    for span in exporter.get_finished_spans()
}
print(json.dumps({"own": own, "spans": spans, "outbound": outbound}, sort_keys=True))
"""


def _early_worker(tag: str) -> tuple[dict[str, Any], dict[str, Any], str]:
    """The real producer's carrier, read by an ``_EARLY_WORKER`` whose check answers
    ``tag``, or fails with it when it is an HTTP status."""
    producer = subprocess.run(
        [sys.executable, "-c", _PRODUCER],
        env=_PROCESS_ENV,
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    )
    worker = subprocess.run(
        [sys.executable, "-c", _EARLY_WORKER.replace("__TAG__", tag)],
        env=_PROCESS_ENV,
        input=producer.stdout,
        capture_output=True,
        text=True,
        check=True,
        timeout=45,
    )
    return json.loads(producer.stdout), json.loads(worker.stdout), worker.stderr


def test_a_worker_that_extracts_before_its_tag_arrives_still_links() -> None:
    """The entry is held while the tag is unknown and checked once it arrives, so both
    agents, and the tool call that ended before the tag, file under the sender with its
    session on every destination; and the worker's own outbound call passes the
    trajectory on once the check has passed."""
    sent, worker, _ = _early_worker("aaaaaaaa")
    sender_id = sent["root_id"]
    remote, nested = worker["own"]["remote-agent"], worker["own"]["nested-agent"]
    expected = {
        "tool-call": {
            "convergent.invocation.span_id": remote,
            "convergent.root_invocation.span_id": sender_id,
            "convergent.parent_invocation.span_id": None,
            "convergent.session.id": "session-from-producer",
        },
        "remote-agent": {
            "convergent.invocation.span_id": remote,
            "convergent.root_invocation.span_id": sender_id,
            "convergent.parent_invocation.span_id": sender_id,
            "convergent.session.id": "session-from-producer",
        },
        "nested-agent": {
            "convergent.invocation.span_id": nested,
            "convergent.root_invocation.span_id": sender_id,
            "convergent.parent_invocation.span_id": remote,
            "convergent.session.id": "session-from-producer",
        },
    }
    for name, keys in expected.items():
        for copy in ("attributes", "exported"):
            got = worker["spans"][name][copy]
            assert {key: got.get(key) for key in keys} == keys, (name, copy)
    assert _entry(worker["outbound"]) == {
        "i": nested,
        "c": nested,
        "w": "aaaaaaaa",
        "s": "session-from-producer",
    }


@pytest.mark.parametrize(
    ("tag", "warning"),
    [
        ("bbbbbbbb", None),
        ("http_401", "the API key was refused"),
        ("http_503", "will ask again in 10 minutes"),
    ],
    ids=["other-workspace", "key-refused", "outage"],
)
def test_an_entry_held_before_the_tag_is_never_used_unchecked(
    tag: str, warning: str | None
) -> None:
    """A held entry whose tag turns out to be another workspace's, or whose check never
    answers with a tag (a refused key, an outage past its retries), is dropped as a
    refused one is: every span under it, the tool call that waited included, goes out
    with neither stamp nor session, so ingest links them all, and so does the worker's
    outbound call. The check's own warning is logged once."""
    _, worker, stderr = _early_worker(tag)
    assert set(worker["spans"]) == {"tool-call", "remote-agent", "nested-agent"}
    for name, copies in worker["spans"].items():
        for copy, attributes in copies.items():
            _assert_unstamped(attributes, f"{name} {copy}")
    assert "convergent=" not in worker["outbound"].get("tracestate", "")
    if warning is not None:
        assert stderr.count(warning) == 1, stderr


#: A worker whose every span ends before its key check answers, about a second after
#: ``init()``. Its exporter writes each span to a file, and like the SDK's own exporters
#: refuses spans once shut down. It then calls ``convergent.flush()`` and stops at once,
#: sits idle until the check answers and stops at once, or exits with no flush, leaving
#: its spans to the exit drain.
_FINISHING_WORKER = r"""
import json
import os
import sys
import time
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult
import convergent
from convergent import _registry, _tag_fetch, _transport

class FileExporter(SpanExporter):
    closed = False

    def export(self, spans):
        if self.closed:
            return SpanExportResult.FAILURE
        with open(sys.argv[1], "a") as out:
            for span in spans:
                out.write(json.dumps({"name": span.name, **dict(span.attributes or {})}) + "\n")
        return SpanExportResult.SUCCESS

    def shutdown(self):
        self.closed = True

def check(url, headers, **kwargs):
    time.sleep(1)
    return {"workspace_tag": "aaaaaaaa"}

_registry.get_json = check
_transport.build_processor = lambda **kwargs: SimpleSpanProcessor(FileExporter())
convergent.init(
    api_key="test-key",  # pragma: allowlist secret
    endpoint="https://example.test",
    release="test",
)
extracted = convergent.carrier.extract(json.load(sys.stdin)["cases"]["valid"])
tracer = convergent.tracer_provider().get_tracer("worker")
with tracer.start_as_current_span(
    "remote-agent", context=extracted, attributes={"gen_ai.operation.name": "invoke_agent"}
):
    pass
if sys.argv[2] == "flush":
    print(json.dumps({"pending": convergent.flush().pending}), flush=True)
    os._exit(0)
if sys.argv[2] == "idle":
    # No later span and no flush: only the check's answer can send the span.
    if (thread := _tag_fetch._state.thread) is not None:
        thread.join(timeout=10)
    os._exit(0)
"""


@pytest.mark.parametrize("ending", ["flush", "idle", "exit"])
def test_a_worker_that_finishes_before_its_tag_arrives_still_sends_its_spans(
    ending: str, tmp_path: Path
) -> None:
    """The spans that ended before the key check answered go out stamped, to an
    exporter that is still open: when the check answers, with nothing else
    happening; from ``flush()``, which waits for it; and at an exit with no flush."""
    producer = subprocess.run(
        [sys.executable, "-c", _PRODUCER],
        env=_PROCESS_ENV,
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    )
    out = tmp_path / "spans.jsonl"
    worker = subprocess.run(
        [sys.executable, "-c", _FINISHING_WORKER, str(out), ending],
        env=_PROCESS_ENV,
        input=producer.stdout,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    spans = [json.loads(line) for line in out.read_text().splitlines()] if out.exists() else []
    assert [span["name"] for span in spans] == ["remote-agent"], worker.stderr
    sender_id = json.loads(producer.stdout)["root_id"]
    assert spans[0]["convergent.root_invocation.span_id"] == sender_id
    assert spans[0]["convergent.session.id"] == "session-from-producer"
    if ending == "flush":
        assert json.loads(worker.stdout) == {"pending": 0}


def test_spans_held_past_the_limit_go_out_unstamped_and_warn_once(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spans waiting for the tag are capped: past the cap, the oldest one's entry is
    refused, so every span under it goes out at once, unstamped, with one warning, and
    the process holds no more of them."""
    producer, _ = _provider()
    consumer, exporter = _provider()
    carrier: dict[str, str] = {}
    try:
        with producer.get_tracer("producer").start_as_current_span(
            "root-agent", attributes={"gen_ai.operation.name": "invoke_agent"}
        ):
            convergent.carrier.inject(carrier)
        # A fetch still out with no tag: entries are held, and nothing answers.
        out = threading.Thread(target=lambda: None)
        out.start()
        out.join()
        monkeypatch.setattr(_tag_fetch._state, "tag", None)
        monkeypatch.setattr(_tag_fetch._state, "thread", out)
        monkeypatch.setattr(_processors, "_WAITING_LIMIT", 2)
        monkeypatch.setattr(_core, "_warned", set())
        tracer = consumer.get_tracer("consumer")
        with caplog.at_level("WARNING", logger="convergent.sdk"):
            with tracer.start_as_current_span(
                "remote-agent",
                context=convergent.carrier.extract(carrier),
                attributes={"gen_ai.operation.name": "invoke_agent"},
            ):
                for index in range(2):
                    with tracer.start_as_current_span(f"held-{index}"):
                        pass
                assert exporter.get_finished_spans() == ()
                for index in range(2, 5):
                    with tracer.start_as_current_span(f"held-{index}"):
                        pass
    finally:
        producer.shutdown()
        consumer.shutdown()

    spans = {span.name: dict(span.attributes or {}) for span in exporter.get_finished_spans()}
    assert set(spans) == {"remote-agent", *(f"held-{index}" for index in range(5))}
    for name, attributes in spans.items():
        _assert_unstamped(attributes, name)
    assert _processors.held_spans() == 0
    full = [
        record
        for record in caplog.records
        if "spans waiting for its workspace tag" in record.getMessage()
    ]
    assert len(full) == 1


def test_a_full_held_span_keeps_its_own_attributes_over_the_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A held span already at its attribute limit keeps every attribute the caller
    set once its entry passes: the received session goes only into free room, so it
    evicts nothing and that span goes without it."""
    producer, _ = _provider()
    consumer = TracerProvider(span_limits=SpanLimits(max_span_attributes=2))
    exporter = InMemorySpanExporter()
    wrapped = _processors.wrap(None, None, [SimpleSpanProcessor(exporter)])
    for processor in [_processors._STAMPER, *wrapped]:
        consumer.add_span_processor(processor)
    carrier: dict[str, str] = {}
    try:
        with convergent.session("session-from-producer"):
            with producer.get_tracer("producer").start_as_current_span(
                "root-agent", attributes={"gen_ai.operation.name": "invoke_agent"}
            ):
                convergent.carrier.inject(carrier, session=True)
        # A fetch still out with no tag: the entry is held.
        out = threading.Thread(target=lambda: None)
        out.start()
        out.join()
        monkeypatch.setattr(_tag_fetch._state, "tag", None)
        monkeypatch.setattr(_tag_fetch._state, "thread", out)
        tracer = consumer.get_tracer("consumer")
        with tracer.start_as_current_span(
            "remote-agent",
            context=convergent.carrier.extract(carrier),
            attributes={"gen_ai.operation.name": "invoke_agent"},
        ):
            with tracer.start_as_current_span("tool-call", attributes={"a": 1, "b": 2}):
                pass
        assert exporter.get_finished_spans() == ()
        monkeypatch.setattr(_tag_fetch._state, "tag", "aaaaaaaa")
        monkeypatch.setattr(_tag_fetch._state, "thread", None)
        _processors.release_answered()
    finally:
        producer.shutdown()
        consumer.shutdown()

    spans = {span.name: span for span in exporter.get_finished_spans()}
    # The agent, with room left, shows the session did arrive.
    room = dict(spans["remote-agent"].attributes or {})
    assert room["convergent.session.id"] == "session-from-producer"
    full = spans["tool-call"]
    assert dict(full.attributes or {}) == {"a": 1, "b": 2}
    assert full.dropped_attributes == 0


def test_exit_ends_within_its_deadline_when_the_tag_never_arrives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exit keeps one 10 s deadline (D-110 A). Held spans wait up to 5 s for a tag that
    never comes, then go out unstamped; when that wait overruns, the exporters get
    only what the deadline leaves, not a fresh 5 s on top."""
    now = [100.0]
    monkeypatch.setattr(_core, "_monotonic", lambda: now[0])
    real_release = _processors.release_held
    asked: list[float] = []

    def overrunning_release(timeout_s: float) -> None:
        asked.append(timeout_s)
        real_release(0.0)
        now[0] += 7.0

    monkeypatch.setattr(_processors, "release_held", overrunning_release)
    granted: list[int] = []
    sent_before_exporters: list[int] = []

    class _Exporter:
        def force_flush(self, timeout_millis: int) -> bool:
            granted.append(timeout_millis)
            sent_before_exporters.append(len(exporter.get_finished_spans()))
            return True

        def shutdown(self) -> None: ...

    monkeypatch.setattr(_core, "_drain", [_Exporter()])
    producer, _ = _provider()
    consumer, exporter = _provider()
    never = threading.Event()
    fetch = threading.Thread(target=never.wait)
    fetch.start()
    carrier: dict[str, str] = {}
    try:
        with producer.get_tracer("producer").start_as_current_span(
            "root-agent", attributes={"gen_ai.operation.name": "invoke_agent"}
        ):
            convergent.carrier.inject(carrier)
        monkeypatch.setattr(_tag_fetch._state, "tag", None)
        monkeypatch.setattr(_tag_fetch._state, "thread", fetch)
        with consumer.get_tracer("consumer").start_as_current_span(
            "remote-agent",
            context=convergent.carrier.extract(carrier),
            attributes={"gen_ai.operation.name": "invoke_agent"},
        ):
            pass
        assert exporter.get_finished_spans() == ()
        _core._drain_all_processors()
    finally:
        never.set()
        fetch.join()
        producer.shutdown()
        consumer.shutdown()

    assert asked == [5.0]
    assert granted == [3_000]
    assert sent_before_exporters == [1]
    spans = {span.name: dict(span.attributes or {}) for span in exporter.get_finished_spans()}
    assert set(spans) == {"remote-agent"}
    _assert_unstamped(spans["remote-agent"], "remote-agent")


def test_init_wraps_the_process_global_propagator() -> None:
    script = r"""
from opentelemetry.propagate import get_global_textmap
from opentelemetry.propagate import inject
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
import convergent
from convergent import _propagation, _registry, _tag_fetch, _transport
from convergent._propagation import ConvergentPropagator

_registry.get_json = lambda *args, **kwargs: {"workspace_tag": "aaaaaaaa"}
_transport.build_processor = lambda **kwargs: SimpleSpanProcessor(InMemorySpanExporter())
convergent.init(
    api_key="test-key",  # pragma: allowlist secret
    endpoint="https://example.test",
    release="test",
    session_hosts=["Worker.Example"],
)
(thread := _tag_fetch._state.thread) and thread.join(timeout=5)
assert isinstance(get_global_textmap(), ConvergentPropagator)
assert get_global_textmap().session_hosts == {"worker.example"}
with convergent.tracer_provider().get_tracer("test").start_as_current_span(
    "root-agent", attributes={"gen_ai.operation.name": "invoke_agent"}
):
    carrier = {}
    inject(carrier)
assert "traceparent" in carrier
assert "convergent=" in carrier["tracestate"]
"""
    subprocess.run(
        [sys.executable, "-c", script],
        env=_PROCESS_ENV,
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    )


def _wire_stamp(invocation: str, start: int, root: str, root_start: int) -> dict[str, Any]:
    return {
        "convergent.invocation.span_id": invocation,
        "convergent.invocation.start_time_unix_micros": start,
        "convergent.root_invocation.span_id": root,
        "convergent.root_invocation.start_time_unix_micros": root_start,
    }


# 36**10 is 3_656_158_440_062_976, "10000000000" in 11 base36 digits.
_WIRE_FORMAT = [
    pytest.param(
        _wire_stamp(
            "2222222222222222", 3_656_158_440_062_976, "2222222222222222", 3_656_158_440_062_976
        ),
        None,
        "0aaaaaaaa1111111111111111222222222222222210000000000",
        id="root",
    ),
    pytest.param(
        _wire_stamp(
            "2222222222222222", 3_656_158_440_062_976, "2222222222222222", 3_656_158_440_062_976
        ),
        "sess-1",
        "0aaaaaaaa1111111111111111222222222222222210000000000sess-1",
        id="root-with-session",
    ),
    pytest.param(
        _wire_stamp("2222222222222222", 3_656_158_440_063_011, "3333333333333333", 1),
        None,
        "1aaaaaaaa111111111111111122222222222222221000000000z333333333333333300000000001",
        id="nested",
    ),
    pytest.param(
        _wire_stamp("2222222222222222", 3_656_158_440_063_011, "3333333333333333", 1),
        "sess-1",
        "1aaaaaaaa111111111111111122222222222222221000000000z333333333333333300000000001sess-1",
        id="nested-with-session",
    ),
    pytest.param(None, "sess-1", "2aaaaaaaa1111111111111111sess-1", id="fresh-with-session"),
]


@pytest.mark.parametrize(("stamp", "session", "entry"), _WIRE_FORMAT)
def test_the_wire_format_is_fixed(
    stamp: dict[str, Any] | None, session: str | None, entry: str
) -> None:
    """Literal entries, checked both ways, so a change made to _encode and _decode together
    still fails: another SDK reads these bytes, not this module."""
    assert _propagation._encode(stamp, "aaaaaaaa", "1111111111111111", session) == entry
    assert _propagation._decode(entry) == (stamp, "1111111111111111", session, "aaaaaaaa")


@pytest.mark.parametrize(
    ("entry", "accepted"),
    [
        ("2aaaaaaaa2222222222222222sess-1", True),
        ("2bbbbbbbb2222222222222222sess-1", False),
        ("2aaaaaaaa3333333333333333sess-1", False),
    ],
    ids=["own-workspace", "foreign-workspace", "mismatched-sender"],
)
def test_a_fresh_entry_is_refused_from_another_workspace_or_sender(
    entry: str, accepted: bool
) -> None:
    """A fresh entry (kind 2) passes the same two checks as kinds 0 and 1: the workspace
    tag is this process's own, and the sender is the ``traceparent`` parent span."""
    carrier = {
        "traceparent": f"00-{'1' * 32}-{'2' * 16}-01",
        "tracestate": f"convergent={entry}",
    }
    provider, exporter = _provider()
    try:
        with provider.get_tracer("consumer").start_as_current_span(
            "remote-agent",
            context=convergent.carrier.extract(carrier),
            attributes={"gen_ai.operation.name": "invoke_agent"},
        ) as span:
            own = f"{span.get_span_context().span_id:016x}"
        attributes = dict(exporter.get_finished_spans()[-1].attributes or {})
    finally:
        provider.shutdown()

    if accepted:
        assert attributes["convergent.invocation.span_id"] == own
        assert attributes["convergent.root_invocation.span_id"] == own
        assert "convergent.parent_invocation.span_id" not in attributes
        assert attributes["convergent.session.id"] == "sess-1"
    else:
        _assert_unstamped(attributes, entry)


@pytest.mark.parametrize(
    ("own_tag", "accepted"),
    [("aaaaaaaa", True), ("bbbbbbbb", False)],
    ids=["own-workspace", "foreign-workspace"],
)
def test_a_fresh_entry_held_for_the_tag_gives_every_span_the_session_once_it_passes(
    monkeypatch: pytest.MonkeyPatch, own_tag: str, accepted: bool
) -> None:
    """A fresh entry (kind 2, as a Temporal run sends) read before the tag arrives is
    held like a stamped one. Once it passes, spans that are not agents get the caller's
    session too, and an agent below them starts its own trajectory. From another
    workspace, none of them carries either."""
    carrier = {
        "traceparent": f"00-{'1' * 32}-{'2' * 16}-01",
        "tracestate": "convergent=2aaaaaaaa2222222222222222sess-1",
    }
    provider, exporter = _provider()
    # A fetch still out with no tag: the entry is held.
    out = threading.Thread(target=lambda: None)
    out.start()
    out.join()
    monkeypatch.setattr(_tag_fetch._state, "tag", None)
    monkeypatch.setattr(_tag_fetch._state, "thread", out)
    try:
        tracer = provider.get_tracer("consumer")
        with tracer.start_as_current_span("activity", context=convergent.carrier.extract(carrier)):
            with tracer.start_as_current_span("tool-call"):
                pass
            with tracer.start_as_current_span(
                "agent", attributes={"gen_ai.operation.name": "invoke_agent"}
            ) as agent:
                own = f"{agent.get_span_context().span_id:016x}"
        assert exporter.get_finished_spans() == ()
        monkeypatch.setattr(_tag_fetch._state, "tag", own_tag)
        monkeypatch.setattr(_tag_fetch._state, "thread", None)
        _processors.release_answered()
    finally:
        provider.shutdown()

    spans = {span.name: dict(span.attributes or {}) for span in exporter.get_finished_spans()}
    assert set(spans) == {"activity", "tool-call", "agent"}
    if accepted:
        for name in spans:
            assert spans[name]["convergent.session.id"] == "sess-1", name
        assert spans["agent"]["convergent.root_invocation.span_id"] == own
        assert "convergent.parent_invocation.span_id" not in spans["agent"]
    else:
        for name, attributes in spans.items():
            _assert_unstamped(attributes, name)


def test_the_largest_entry_fits_one_128_character_member() -> None:
    """W3C lets a hop drop members over 128 characters first. The largest entry, a nested
    agent's with a session at the limit, still fits, and the session arrives whole."""
    provider, _ = _provider()
    session = "s" * _propagation.SESSION_LIMIT
    tracer = provider.get_tracer("producer")
    try:
        with convergent.session(session):
            with tracer.start_as_current_span(
                "root-agent", attributes={"gen_ai.operation.name": "invoke_agent"}
            ):
                with tracer.start_as_current_span(
                    "sub-agent", attributes={"gen_ai.operation.name": "invoke_agent"}
                ) as sub:
                    sub_id = f"{sub.get_span_context().span_id:016x}"
                    with tracer.start_as_current_span("POST", kind=SpanKind.CLIENT):
                        carrier: dict[str, str] = {}
                        convergent.carrier.inject(carrier, session=True)
    finally:
        provider.shutdown()

    assert len(_member(carrier)) <= 128
    assert _entry(carrier)["w"] == "aaaaaaaa"
    assert _entry(carrier)["i"] == sub_id
    assert _entry(carrier)["s"] == session


@pytest.mark.parametrize(
    "session",
    ["s" * (_propagation.SESSION_LIMIT + 1), "has,comma", "has=equals", "has space", "café"],
    ids=["too-long", "comma", "equals", "space", "non-ascii"],
)
def test_a_session_the_entry_cannot_carry_stays_local_and_warns_once(
    session: str, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The session is never truncated or rewritten: the stamp goes without it."""
    provider, _ = _provider()
    monkeypatch.setattr(_core, "_warned", set())
    carriers: list[dict[str, str]] = [{}, {}]
    try:
        with caplog.at_level("WARNING", logger="convergent.sdk"):
            with convergent.session(session):
                with provider.get_tracer("producer").start_as_current_span(
                    "root-agent", attributes={"gen_ai.operation.name": "invoke_agent"}
                ):
                    for carrier in carriers:
                        convergent.carrier.inject(carrier, session=True)
    finally:
        provider.shutdown()

    for carrier in carriers:
        assert "s" not in _entry(carrier)
    assert sum("session" in record.message for record in caplog.records) == 1


def test_a_header_of_32_members_gets_only_the_sdks_own_warning(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OpenTelemetry's ``TraceState.add`` logs on every call past 32 members, and a
    request with a session tries twice. A full header must log only the SDK's
    once-per-process warning, however many requests carry it."""
    producer, _ = _provider()
    monkeypatch.setattr(_core, "_warned", set())
    propagator = ConvergentPropagator(TraceContextTextMapPropagator())
    full = ",".join(f"v{i}=x" for i in range(32))
    carriers = [{"tracestate": full} for _ in range(3)]
    try:
        with caplog.at_level("WARNING"):
            with convergent.session("session-1"):
                with producer.get_tracer("producer").start_as_current_span(
                    "root-agent", attributes={"gen_ai.operation.name": "invoke_agent"}
                ):
                    for carrier in carriers:
                        with _propagation._sending_session():
                            propagator.inject(carrier)
    finally:
        producer.shutdown()

    assert [carrier["tracestate"] for carrier in carriers] == [full] * 3
    logged = [(record.name, record.getMessage()) for record in caplog.records]
    assert len(logged) == 1, logged
    assert logged[0][0] == "convergent.sdk"
    assert "W3C tracestate limits" in logged[0][1]


@pytest.mark.parametrize(
    ("tracestate", "expected"),
    [
        ("Vendor=1,other=2", "Vendor=1,other=2"),
        ("not a tracestate header", "not a tracestate header"),
        ("a=1 ,convergent=0aaaaaaaa, Vendor=2,b=3", "a=1 , Vendor=2,b=3"),
        ("convergent=0aaaaaaaa", None),
        # A header value may not start or end with a space; h11 refuses to send one.
        ("convergent=0aaaaaaaa, a=1", "a=1"),
        ("a=1 ,convergent=0aaaaaaaa", "a=1"),
        ("convergent=0aaaaaaaa, ", None),
    ],
    ids=[
        "upper-case-member",
        "unparseable",
        "entry-among-others",
        "entry-alone",
        "entry-first",
        "entry-last",
        "entry-and-blank",
    ],
)
def test_without_a_tag_inject_cuts_only_the_convergent_member(
    tracestate: str, expected: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no workspace tag (a workspace turned off), the SDK adds nothing and cuts
    only an existing ``convergent=`` member, which may hold a session id. Every other
    byte, other vendors' members, their order and spacing, stays as it came."""
    monkeypatch.setattr(_tag_fetch._state, "tag", None)
    producer, _ = _provider()
    carrier = {"tracestate": tracestate}
    try:
        with producer.get_tracer("producer").start_as_current_span(
            "root-agent", attributes={"gen_ai.operation.name": "invoke_agent"}
        ):
            ConvergentPropagator(TraceContextTextMapPropagator()).inject(carrier)
    finally:
        producer.shutdown()

    assert carrier.get("tracestate") == expected
    assert "traceparent" in carrier


def test_without_a_tag_inject_cuts_the_entry_a_plain_extract_left_on_the_parent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A plain W3C extract keeps a received entry on the remote parent's trace state,
    and the inner propagator would forward it, session and all, to every host."""
    monkeypatch.setattr(_tag_fetch._state, "tag", None)
    incoming = {
        "traceparent": "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01",
        "tracestate": "other=1,convergent=0aaaaaaaasession",
    }
    extracted = TraceContextTextMapPropagator().extract(incoming)
    producer, _ = _provider()
    carrier: dict[str, str] = {}
    try:
        with producer.get_tracer("producer").start_as_current_span("work", context=extracted):
            ConvergentPropagator(TraceContextTextMapPropagator()).inject(carrier)
    finally:
        producer.shutdown()

    assert carrier["tracestate"] == "other=1"


def test_a_session_dropped_to_fit_the_header_warns_once(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The entry fits the header only without the session: it goes, and the SDK says so."""
    producer, _ = _provider()
    monkeypatch.setattr(_core, "_warned", set())
    # 431 characters: room for the 63-character root entry, not for it plus a session.
    crowded = ",".join(f"v{i}=" + "x" * 104 for i in range(4))
    carriers = [{"tracestate": crowded}, {"tracestate": crowded}]
    try:
        with caplog.at_level("WARNING", logger="convergent.sdk"):
            with convergent.session("s" * _propagation.SESSION_LIMIT):
                with producer.get_tracer("producer").start_as_current_span(
                    "root-agent", attributes={"gen_ai.operation.name": "invoke_agent"}
                ):
                    for carrier in carriers:
                        convergent.carrier.inject(carrier, session=True)
    finally:
        producer.shutdown()

    for carrier in carriers:
        assert "s" not in _entry(carrier)
    warnings = [record.message for record in caplog.records if record.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "session" in warnings[0]
    assert "W3C limits" in warnings[0]


def test_replacing_the_propagator_after_init_warns_once() -> None:
    script = r"""
import logging
from opentelemetry.propagate import set_global_textmap
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
import convergent
from convergent import _transport

logging.basicConfig(level=logging.WARNING, format="%(message)s")
_transport.build_processor = lambda **kwargs: SimpleSpanProcessor(InMemorySpanExporter())
convergent.init(
    api_key="test-key",  # pragma: allowlist secret
    endpoint="https://example.test",
    release="test",
)
tracer = convergent.tracer_provider().get_tracer("test")
agent = {"gen_ai.operation.name": "invoke_agent"}
with tracer.start_as_current_span("agent", attributes=agent):
    pass
logging.warning("replacing now")
set_global_textmap(TraceContextTextMapPropagator())
for _ in range(3):
    with tracer.start_as_current_span("agent", attributes=agent):
        pass
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        env=_PROCESS_ENV,
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    )
    lines = result.stderr.splitlines()
    warnings = [index for index, line in enumerate(lines) if "replaced" in line]
    assert len(warnings) == 1, result.stderr
    assert warnings[0] > lines.index("replacing now"), result.stderr


def test_session_goes_only_to_listed_hosts() -> None:
    """The stamp goes everywhere; the session only to a host the caller listed or opted into."""
    provider, _ = _provider()
    propagator = ConvergentPropagator(
        TraceContextTextMapPropagator(), session_hosts=["Worker.Example"]
    )
    tracer = provider.get_tracer("producer")
    sent: dict[str, dict[str, Any]] = {}
    try:
        with convergent.session("user@example.com"):
            with tracer.start_as_current_span(
                "root-agent", attributes={"gen_ai.operation.name": "invoke_agent"}
            ):
                for name, attributes in (
                    ("listed", {"http.url": "https://worker.example:8443/jobs"}),
                    ("unlisted", {"server.address": "api.openai.com"}),
                ):
                    with tracer.start_as_current_span(
                        "POST", kind=SpanKind.CLIENT, attributes=attributes
                    ):
                        carrier: dict[str, str] = {}
                        propagator.inject(carrier)
                        sent[name] = _entry(carrier)
                manual: dict[str, str] = {}
                propagator.inject(manual)
                sent["manual"] = _entry(manual)
                opted: dict[str, str] = {}
                convergent.carrier.inject(opted, session=True)
                sent["opted"] = _entry(opted)
    finally:
        provider.shutdown()

    assert sent["listed"]["s"] == "user@example.com"
    assert sent["opted"]["s"] == "user@example.com"
    for name in ("unlisted", "manual"):
        assert "s" not in sent[name], name
        assert "i" in sent[name], name


def test_declared_agent_under_a_dropped_server_span_keeps_the_sender_and_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With ``agents=`` set, the agent filter drops a plain SERVER span, which then holds no
    stamp. The declared agent under it still files under the sender, a library child
    started from a fresh context still takes the received session, and a call made from
    the dropped span forwards the sender's stamp."""
    producer, _ = _provider()
    consumer, exporter = _provider(agents=["remote-agent"])
    carrier: dict[str, str] = {}
    try:
        with convergent.session("session-from-producer"):
            with producer.get_tracer("producer").start_as_current_span(
                "root-agent", attributes={"gen_ai.operation.name": "invoke_agent"}
            ) as root:
                sender_id = f"{root.get_span_context().span_id:016x}"
                convergent.carrier.inject(carrier, session=True)
        # A worker that never opened a session of its own.
        monkeypatch.setattr(_processors, "_ever_attached", False)
        monkeypatch.setattr(_processors, "_ever_received", False)

        tracer = consumer.get_tracer("consumer")
        extracted = convergent.carrier.extract(carrier)
        with tracer.start_as_current_span(
            "POST /jobs",
            context=extracted,
            kind=SpanKind.SERVER,
            attributes={"http.method": "POST"},
        ) as server:
            server_id = f"{server.get_span_context().span_id:016x}"
            forward: dict[str, str] = {}
            ConvergentPropagator(TraceContextTextMapPropagator()).inject(forward)
            with tracer.start_as_current_span(
                "remote-agent",
                attributes={
                    "gen_ai.operation.name": "invoke_agent",
                    "gen_ai.agent.name": "remote-agent",
                },
            ) as agent:
                with tracer.start_as_current_span(
                    "library-child", context=trace.set_span_in_context(agent)
                ):
                    pass
    finally:
        producer.shutdown()
        consumer.shutdown()

    spans = {span.name: dict(span.attributes or {}) for span in exporter.get_finished_spans()}
    assert "POST /jobs" not in spans
    assert spans["remote-agent"]["convergent.root_invocation.span_id"] == sender_id
    assert spans["remote-agent"]["convergent.parent_invocation.span_id"] == sender_id
    assert spans["library-child"]["convergent.session.id"] == "session-from-producer"
    assert _entry(forward)["i"] == sender_id
    assert _entry(forward)["c"] == server_id


def test_a_span_in_another_trace_does_not_inherit_an_attached_carrier() -> None:
    """The received stamp and session follow parentage, not whatever context is attached."""
    producer, _ = _provider()
    consumer, exporter = _provider()
    carrier: dict[str, str] = {}
    try:
        with convergent.session("session-from-producer"):
            with producer.get_tracer("producer").start_as_current_span(
                "root-agent", attributes={"gen_ai.operation.name": "invoke_agent"}
            ):
                convergent.carrier.inject(carrier, session=True)
        other = trace.NonRecordingSpan(
            trace.SpanContext(
                trace_id=0xABC, span_id=0xDEF, is_remote=True, trace_flags=trace.TraceFlags(0x1)
            )
        )
        own: dict[str, str] = {}
        token = otel_context.attach(convergent.carrier.extract(carrier))
        try:
            tracer = consumer.get_tracer("consumer")
            for name, parent in (("other-trace", other), ("new-root", trace.INVALID_SPAN)):
                with tracer.start_as_current_span(
                    name,
                    context=trace.set_span_in_context(parent),
                    attributes={"gen_ai.operation.name": "invoke_agent"},
                ) as span:
                    own[name] = f"{span.get_span_context().span_id:016x}"
        finally:
            otel_context.detach(token)
    finally:
        producer.shutdown()
        consumer.shutdown()

    spans = {span.name: dict(span.attributes or {}) for span in exporter.get_finished_spans()}
    assert set(spans) == set(own)
    # A remote parent that sent no entry leaves the span for ingest to link.
    _assert_unstamped(spans["other-trace"], "other-trace")
    assert spans["new-root"]["convergent.root_invocation.span_id"] == own["new-root"]
    assert "convergent.session.id" not in spans["new-root"]


class _UppercaseHeaders:
    """A dict carrier whose keys a broker uppercases."""

    def get(self, carrier: dict[str, str], key: str) -> list[str]:
        value = carrier.get(key.upper())
        return [value] if value is not None else []

    def keys(self, carrier: dict[str, str]) -> list[str]:
        return list(carrier)

    def set(self, carrier: dict[str, str], key: str, value: str) -> None:
        carrier[key.upper()] = value


class _HeaderList:
    """Kafka-style headers: a list of (key, bytes) pairs, not a mapping."""

    def get(self, carrier: list[tuple[str, bytes]], key: str) -> list[str]:
        return [value.decode() for name, value in carrier if name == key]

    def keys(self, carrier: list[tuple[str, bytes]]) -> list[str]:
        return [name for name, _ in carrier]

    def set(self, carrier: list[tuple[str, bytes]], key: str, value: str) -> None:
        carrier.append((key, value.encode()))


@pytest.mark.parametrize(
    ("codec", "new_carrier"),
    [(_UppercaseHeaders(), dict), (_HeaderList(), list)],
    ids=["uppercase-dict", "header-list"],
)
def test_custom_carriers_round_trip_through_their_own_getter_and_setter(
    codec: Any, new_carrier: Any
) -> None:
    """Inject and extract use only the caller's getter and setter, and never raise on a
    carrier that is not a mapping."""
    producer, _ = _provider()
    consumer, exporter = _provider()
    carrier = new_carrier()
    try:
        with producer.get_tracer("producer").start_as_current_span(
            "root-agent", attributes={"gen_ai.operation.name": "invoke_agent"}
        ):
            convergent.carrier.inject(carrier, setter=codec)
        assert codec.get(carrier, "traceparent")
        assert len(codec.get(carrier, "tracestate")) == 1
        extracted = convergent.carrier.extract(carrier, getter=codec)
        with consumer.get_tracer("consumer").start_as_current_span(
            "remote-child", context=extracted
        ):
            pass
        attributes = exporter.get_finished_spans()[0].attributes or {}
        assert "convergent.invocation.span_id" in attributes
    finally:
        producer.shutdown()
        consumer.shutdown()


def test_inject_never_passes_a_received_entry_on_unchecked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Re-injecting under an attached carrier rebuilds the entry through the session gate,
    and a span whose stamp failed sends no entry at all, not even the one it received."""
    is_invocation = _processors._is_invocation

    def fails_on_one_span(span: Any) -> bool:
        if span.name == "failed":
            raise RuntimeError("stamper failed")
        return is_invocation(span)

    # The stamper's own failure path, as test_sdk_otel_processor drives it.
    monkeypatch.setattr(_processors, "_is_invocation", fails_on_one_span)
    producer, _ = _provider()
    consumer, _ = _provider()
    carrier: dict[str, str] = {}
    try:
        with convergent.session("session-from-producer"):
            with producer.get_tracer("producer").start_as_current_span(
                "root-agent", attributes={"gen_ai.operation.name": "invoke_agent"}
            ) as root:
                sender_id = f"{root.get_span_context().span_id:016x}"
                convergent.carrier.inject(carrier, session=True)
        extracted = convergent.carrier.extract(carrier)

        token = otel_context.attach(extracted)
        try:
            default: dict[str, str] = {}
            convergent.carrier.inject(default)
            opted: dict[str, str] = {}
            convergent.carrier.inject(opted, session=True)
        finally:
            otel_context.detach(token)

        with consumer.get_tracer("consumer").start_as_current_span(
            "failed", context=extracted
        ) as failed:
            assert getattr(failed, _processors._TRAJECTORY_FAILED_FIELD, False)
            after_failure: dict[str, str] = {}
            convergent.carrier.inject(after_failure, session=True)
    finally:
        producer.shutdown()
        consumer.shutdown()

    assert "s" not in _entry(default)
    assert _entry(default)["i"] == sender_id
    assert _entry(default)["c"] == sender_id
    assert _entry(opted)["s"] == "session-from-producer"
    assert "traceparent" in after_failure
    assert "convergent=" not in after_failure.get("tracestate", "")


def _received_with_session(tracestate_before: str = "") -> tuple[dict[str, str], str]:
    """A carrier sent with a session, and the sender's span id."""
    producer, _ = _provider()
    carrier: dict[str, str] = {}
    try:
        with convergent.session("user@example.com"):
            with producer.get_tracer("producer").start_as_current_span(
                "root-agent", attributes={"gen_ai.operation.name": "invoke_agent"}
            ) as root:
                sender_id = f"{root.get_span_context().span_id:016x}"
                convergent.carrier.inject(carrier, session=True)
    finally:
        producer.shutdown()
    assert _entry(carrier)["s"] == "user@example.com"
    if tracestate_before:
        carrier["tracestate"] = f"{tracestate_before},{carrier['tracestate']}"
    return carrier, sender_id


def test_a_prefilled_tracestate_inject_cannot_parse_is_left_alone(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The SDK cannot add its entry to a header it cannot read, and does not delete
    the caller's header for it. The one warning names that cause, not the size limit."""
    producer, _ = _provider()
    monkeypatch.setattr(_core, "_warned", set())
    carriers = [{"tracestate": "not a tracestate header"} for _ in range(2)]
    try:
        with caplog.at_level("WARNING"):
            with producer.get_tracer("producer").start_as_current_span(
                "root-agent", attributes={"gen_ai.operation.name": "invoke_agent"}
            ):
                for carrier in carriers:
                    ConvergentPropagator(TraceContextTextMapPropagator()).inject(carrier)
    finally:
        producer.shutdown()

    assert [carrier["tracestate"] for carrier in carriers] == ["not a tracestate header"] * 2
    warnings = [record.message for record in caplog.records if record.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "could not parse" in warnings[0]
    assert "W3C tracestate limits" not in warnings[0]


def test_a_tracestate_sent_as_several_header_lines_is_read_whole() -> None:
    """HTTP lets a hop split tracestate over several header lines; the entry may sit
    on any of them."""
    carrier, sender_id = _received_with_session("vendor=1")
    lines: dict[str, Any] = {
        "traceparent": carrier["traceparent"],
        "tracestate": carrier["tracestate"].split(","),
    }
    assert not lines["tracestate"][0].startswith("convergent=")
    consumer, exporter = _provider()
    try:
        extracted = convergent.carrier.extract(lines)
        with consumer.get_tracer("consumer").start_as_current_span(
            "remote-agent", context=extracted, attributes={"gen_ai.operation.name": "invoke_agent"}
        ):
            pass
    finally:
        consumer.shutdown()

    attributes = dict(exporter.get_finished_spans()[-1].attributes or {})
    assert attributes["convergent.root_invocation.span_id"] == sender_id
    assert attributes["convergent.session.id"] == "user@example.com"


def test_an_accepted_session_never_rides_the_extracted_trace_state() -> None:
    """Child spans copy the remote trace state and any other propagator forwards it, so
    once accepted the session lives only in the SDK's own record, never in the header."""
    carrier, _ = _received_with_session("vendor=1")
    consumer, exporter = _provider()
    try:
        extracted = convergent.carrier.extract(carrier)
        remote = trace.get_current_span(extracted).get_span_context()
        tracer = consumer.get_tracer("consumer")
        with tracer.start_as_current_span("POST /jobs", context=extracted, kind=SpanKind.SERVER):
            with tracer.start_as_current_span("library-child") as child:
                child_state = child.get_span_context().trace_state
                plain: dict[str, str] = {}
                TraceContextTextMapPropagator().inject(plain)
    finally:
        consumer.shutdown()

    for state in (remote.trace_state, child_state):
        assert "convergent" not in state
        assert state.get("vendor") == "1"
    assert plain["tracestate"] == "vendor=1"
    spans = {span.name: dict(span.attributes or {}) for span in exporter.get_finished_spans()}
    assert spans["library-child"]["convergent.session.id"] == "user@example.com"


_OLD_ENTRY = "convergent=0aaaaaaaasession"


class _HeaderLines(MutableMapping[str, str]):
    """A carrier holding a key once per header line, as aiohttp's ``MultiDict`` does."""

    def __init__(self, pairs: list[tuple[str, str]]) -> None:
        self.pairs = pairs

    def __getitem__(self, key: str) -> str:
        for name, value in self.pairs:
            if name == key:
                return value
        raise KeyError(key)

    def __setitem__(self, key: str, value: str) -> None:
        self.pairs = [pair for pair in self.pairs if pair[0] != key] + [(key, value)]

    def __delitem__(self, key: str) -> None:
        if key not in self:
            raise KeyError(key)
        self.pairs = [pair for pair in self.pairs if pair[0] != key]

    def __iter__(self) -> Iterator[str]:
        return (name for name, _ in self.pairs)

    def __len__(self) -> int:
        return len(self.pairs)

    def items(self) -> Any:
        return list(self.pairs)

    def add(self, key: str, value: str) -> None:
        self.pairs.append((key, value))


@pytest.mark.parametrize(
    ("carrier", "expected"),
    [
        ({"Tracestate": f"vendor=1,{_OLD_ENTRY}"}, [("Tracestate", "vendor=1")]),
        ({"TraceState": _OLD_ENTRY}, []),
        ({"tracestate": ["vendor=1", _OLD_ENTRY]}, [("tracestate", ["vendor=1"])]),
        (
            _HeaderLines([("tracestate", "vendor=1"), ("x", "y"), ("tracestate", _OLD_ENTRY)]),
            [("x", "y"), ("tracestate", "vendor=1")],
        ),
    ],
    ids=["upper-case-key", "upper-case-key-entry-alone", "header-lines", "multidict"],
)
@pytest.mark.parametrize("tag", [None, "aaaaaaaa"])
def test_a_reused_carrier_loses_its_old_entry_in_any_shape(
    carrier: Any,
    expected: list[tuple[str, object]],
    tag: str | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A leftover entry, which may hold a session, is cut however the carrier holds
    ``tracestate``; every other value keeps its key and shape."""
    monkeypatch.setattr(_tag_fetch._state, "tag", tag)

    ConvergentPropagator(TraceContextTextMapPropagator()).inject(carrier)

    assert list(carrier.items()) == expected


@pytest.mark.parametrize(
    ("carrier", "parent", "expected"),
    [
        ({"tracestate": _OLD_ENTRY}, _OLD_ENTRY, {}),
        ({"tracestate": "old=1"}, _OLD_ENTRY, {}),
        (
            {"Tracestate": f"vendor=1,{_OLD_ENTRY}"},
            "other=1",
            {"Tracestate": "vendor=1", "tracestate": "other=1"},
        ),
    ],
    ids=["parent-holds-only-the-entry", "earlier-header", "upper-case-key"],
)
@pytest.mark.parametrize("tag", [None, "aaaaaaaa"])
def test_a_reused_carrier_loses_its_old_entry_when_a_header_is_written(
    carrier: dict[str, str],
    parent: str,
    expected: dict[str, str],
    tag: str | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With nothing to add, the parent's own ``tracestate`` goes out too, and the
    carrier's old entry, which may hold a session, is cut all the same."""
    monkeypatch.setattr(_tag_fetch._state, "tag", tag)
    traceparent = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
    extracted = TraceContextTextMapPropagator().extract(
        {"traceparent": traceparent, "tracestate": parent}
    )

    ConvergentPropagator(TraceContextTextMapPropagator()).inject(carrier, extracted)

    assert carrier == {**expected, "traceparent": traceparent}


@pytest.mark.parametrize("tag", [None, "aaaaaaaa"], ids=["no-tag", "tag"])
def test_extract_parses_no_header_it_has_no_entry_to_read_from(
    tag: str | None, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no tag, or no ``convergent=`` member, there is nothing to accept, so the
    SDK leaves the header to the inner propagator: an invalid ``tracestate`` gets
    only the warnings OpenTelemetry's own extract logs, none of the SDK's making."""
    monkeypatch.setattr(_tag_fetch._state, "tag", tag)
    carrier = {
        "traceparent": "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01",
        "tracestate": "not a valid member,Other=1",
    }

    def logged(propagator: Any) -> list[str]:
        caplog.clear()
        with caplog.at_level("WARNING"):
            propagator.extract(dict(carrier))
        return [record.getMessage() for record in caplog.records]

    plain = logged(TraceContextTextMapPropagator())
    assert logged(ConvergentPropagator(TraceContextTextMapPropagator())) == plain


def test_extract_fails_closed_when_the_entry_cannot_be_removed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If removing the received entry fails, the context returned never still holds it."""
    carrier, _ = _received_with_session("vendor=1")

    def fail(extracted: object) -> object:
        raise RuntimeError("strip failed")

    monkeypatch.setattr(_propagation, "_without_remote_entry", fail)
    extracted = ConvergentPropagator(TraceContextTextMapPropagator()).extract(carrier)

    assert "convergent" not in trace.get_current_span(extracted).get_span_context().trace_state


class _UnreadableCarrier(dict[str, str]):
    def items(self) -> Any:
        raise RuntimeError("carrier cannot be read")


class _UnpoppableCarrier(dict[str, str]):
    def pop(self, *args: object) -> Any:
        raise RuntimeError("carrier cannot drop a key")


@pytest.mark.parametrize("carrier_type", [_UnreadableCarrier, _UnpoppableCarrier])
def test_inject_never_raises_when_the_carrier_breaks(carrier_type: type[dict[str, str]]) -> None:
    """A carrier that fails while the old entry is cleared still gets this call's
    traceparent and a fresh entry, and the earlier call's session does not go out."""
    carrier, _ = _received_with_session()
    reused = carrier_type(tracestate=f"vendor=1,{carrier['tracestate']}")
    producer, _ = _provider()
    try:
        with producer.get_tracer("producer").start_as_current_span(
            "root-agent", attributes={"gen_ai.operation.name": "invoke_agent"}
        ) as root:
            ConvergentPropagator(TraceContextTextMapPropagator()).inject(reused)
    finally:
        producer.shutdown()

    sender_id = f"{root.get_span_context().span_id:016x}"
    assert reused["traceparent"].split("-")[2] == sender_id
    assert _entry(reused) == {"i": sender_id, "c": sender_id, "w": "aaaaaaaa"}
    assert "user@example.com" not in reused["tracestate"]


def test_an_unknown_otel_propagators_name_does_not_break_importing_the_sdk() -> None:
    """opentelemetry.propagate raises on an OTEL_PROPAGATORS name it cannot load
    when it is imported, so the SDK imports it only when init() or a caller asks."""
    imported = subprocess.run(
        [sys.executable, "-c", "from convergent import init, propagator, carrier"],
        env={**_PROCESS_ENV, "OTEL_PROPAGATORS": "no-such-propagator"},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert imported.returncode == 0, imported.stderr
