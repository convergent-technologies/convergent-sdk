"""``convergent.check()``: the report a person reads when nothing arrived.

The printed text is the product here, so it is asserted verbatim. A report whose
fields are all correct but whose printout buries the one actionable line is a report
nobody acts on.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import convergent
from convergent import _core, _registry, _transport
from convergent._check import Report
from convergent._core import Status

_KEY = "k-check"  # pragma: allowlist secret
_ENDPOINT = "https://dp.example.test"
_RELEASE = "a3f21c9"

_HEALTHY: dict[str, object] = {
    "key": {"organization_id": "org_7c2b1a9e4d"},
    "deployment": {"resolved": True, "deployment_id": "dep_abc"},
    "agents": [
        {"agent_id": "agt_1", "name": "support-agent"},
        {"agent_id": "agt_2", "name": "billing-agent"},
    ],
    "agents_truncated": False,
    "notes": [],
}


@pytest.fixture(autouse=True)
def reset_sdk(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name in (
        "CONVERGENT_API_KEY",
        "CONVERGENT_ENDPOINT",
        "CONVERGENT_SPANS_DIR",
        "CONVERGENT_RELEASE",
        "CONVERGENT_DEBUG",
        "CONVERGENT_TRACES_EXPORTER",
        "CONVERGENT_STRICT",
        "CONVERGENT_REQUIRE_SPAN_ATTRIBUTES",
        "CONVERGENT_REJECT_SPAN_ATTRIBUTES",
    ):
        monkeypatch.delenv(name, raising=False)
    _reset_otel()
    _core._reset_for_tests()
    yield
    _core._reset_for_tests()
    _reset_otel()


def _reset_otel() -> None:
    trace._TRACER_PROVIDER_SET_ONCE._done = False  # type: ignore[attr-defined]
    trace._TRACER_PROVIDER = None  # type: ignore[attr-defined]


@pytest.fixture
def enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        _transport, "build_processor", lambda **_: SimpleSpanProcessor(InMemorySpanExporter())
    )
    monkeypatch.setattr(_registry, "post_json", lambda *_, **__: {"deployment_id": "dep_abc"})
    convergent.init(api_key=_KEY, endpoint=_ENDPOINT, release=_RELEASE)


def _refuse_to_start(**_: object) -> None:
    raise RuntimeError("no exporter for you")


def _record_gets(monkeypatch: pytest.MonkeyPatch, result: object) -> list[dict]:
    calls: list[dict] = []

    def _get(url: str, **kwargs: object) -> object:
        calls.append({"url": url, **kwargs})
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(_registry, "get_json", _get)
    return calls


def test_check_reports_a_healthy_round_trip(monkeypatch: pytest.MonkeyPatch, enabled: None) -> None:
    calls = _record_gets(monkeypatch, _HEALTHY)

    report = convergent.check()

    assert calls[0]["url"] == f"{_ENDPOINT}/v1/check?release={_RELEASE}", (
        "the server reports on the release init() registered, or on no deployment at all"
    )
    assert calls[0]["headers"] == {"authorization": f"Bearer {_KEY}"}
    assert report.round_trip == "ok"
    assert report.round_trip_ms is not None
    assert report.organization_id == "org_7c2b1a9e4d"
    assert report.agents == ["support-agent", "billing-agent"]
    assert report.agents_truncated is False
    assert report.notes == []
    assert report.status.deployment == "dep_abc"
    assert report.status.release == _RELEASE


def test_credentials_header_key_is_lowercase_for_grpc_metadata(enabled: None) -> None:
    """gRPC rejects uppercase metadata keys; see the Credentials docstring."""
    credentials = _core.credentials()

    assert credentials is not None
    assert list(credentials.headers) == ["authorization"]


def test_check_reports_a_rejected_key_instead_of_raising(
    monkeypatch: pytest.MonkeyPatch, enabled: None
) -> None:
    _record_gets(monkeypatch, _registry.RegistrationError("http_401"))

    report = convergent.check()

    assert report.round_trip == "http_401"
    assert report.round_trip_ms is None
    assert report.organization_id is None
    printed = str(report)
    assert "failed (http_401)" in printed
    assert _ENDPOINT in printed, "which endpoint refused the key is half the diagnosis"
    assert _KEY not in printed, "a report gets pasted into a ticket"


def test_check_survives_a_request_that_raises(
    monkeypatch: pytest.MonkeyPatch, enabled: None, caplog: pytest.LogCaptureFixture
) -> None:
    _record_gets(monkeypatch, ValueError("something get_json never promised"))

    with caplog.at_level(logging.WARNING, logger="convergent.sdk"):
        report = convergent.check()

    assert report.round_trip == "unexpected"
    assert any("check request: ValueError" in record.message for record in caplog.records)
    assert all(record.exc_info is None for record in caplog.records), (
        "no traceback: a handler that captures locals would carry the bearer header "
        "out of get_json's frame"
    )


def test_check_still_asks_the_server_when_tracing_could_not_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The key and the endpoint are right and nothing is arriving, which is exactly
    when the server's view is worth the most. The report must not say the key and
    endpoint are unset, and it must not skip the request."""
    monkeypatch.setattr(_transport, "build_processor", _refuse_to_start)
    monkeypatch.setattr(_registry, "post_json", lambda *_, **__: {"deployment_id": "dep_abc"})
    calls = _record_gets(monkeypatch, _HEALTHY)

    status = convergent.init(api_key=_KEY, endpoint=_ENDPOINT, release=_RELEASE)
    report = convergent.check()

    assert (status.enabled, status.reason) == (False, "setup_failed")
    assert (report.status.enabled, report.status.reason) == (False, "setup_failed")
    assert calls[0]["url"] == f"{_ENDPOINT}/v1/check?release={_RELEASE}", (
        "the release still goes out, so the server reports on the right deployment"
    )
    assert report.round_trip == "ok"
    assert report.organization_id == "org_7c2b1a9e4d"
    assert not report, "nothing is being recorded, whatever the server can see"
    printed = str(report)
    assert "  reason      setup_failed" in printed
    assert "init() or install() ran but tracing could not start" in printed


def test_a_200_from_something_other_than_the_check_endpoint_is_a_failed_round_trip(
    monkeypatch: pytest.MonkeyPatch, enabled: None
) -> None:
    """The healthy state is the one that prints "no notes", so a body nobody
    recognized must not reach it. The server makes the organization id required, which
    is what tells the two apart."""
    _record_gets(monkeypatch, {"status": "healthy", "service": "somebody else"})

    report = convergent.check()

    assert report.round_trip == "not_a_check_response"
    assert report.organization_id is None
    assert not report
    printed = str(report)
    assert "failed (not_a_check_response)" in printed
    assert _ENDPOINT in printed, "which address answered is half the diagnosis"
    assert "no notes" not in printed


def test_server_text_cannot_move_the_cursor_or_run_long(
    monkeypatch: pytest.MonkeyPatch, enabled: None
) -> None:
    """Agent names come from the customer's own trace data, and the report gets pasted
    into a support thread."""
    _record_gets(
        monkeypatch,
        {
            **_HEALTHY,
            "agents": [{"agent_id": "agt_1", "name": "billing\x1b[2K\rround trip  ok (1ms)"}],
            "notes": [{"code": "wordy", "message": "a\x08\x08b" + "z" * 600}],
        },
    )

    report = convergent.check()

    assert report.agents == ["billing [2K round trip  ok (1ms)"]
    assert report.notes[0].message == "a  b" + "z" * 496 + "..."
    printed = str(report)
    assert "\x1b" not in printed
    assert "\x08" not in printed


def test_a_report_is_true_only_when_tracing_is_on_and_the_server_confirmed_it() -> None:
    """The server's route description sends customers here from CI, so the answer is a
    bool rather than a string every gate would have to spell correctly."""
    assert Report(status=Status(enabled=True), round_trip="ok")
    assert not Report(status=Status(enabled=True), round_trip="http_401")
    assert not Report(status=Status(enabled=False, reason="setup_failed"), round_trip="ok")
    # A provider that is enabled but cannot be found is still broken; a CI gate must
    # not go green while span() and observe() are recording nothing.
    assert not Report(status=Status(enabled=True, reason="no_provider"), round_trip="ok")
    # A losing second init() means the first configuration is running and working, so
    # a CI gate on bool(check()) must not fail a healthy process for it.
    assert Report(status=Status(enabled=True, reason="already_configured"), round_trip="ok")


def test_a_losing_second_init_prints_what_happened() -> None:
    """already_configured used to flip the bool with nothing wrong in the printed
    report. The explanation line is what makes the state diagnosable."""
    report = Report(
        status=Status(enabled=True, destinations=["convergent"], reason="already_configured"),
        round_trip="ok",
        round_trip_ms=1,
        organization_id="org_31KcM2p4Wq",
    )
    printed = str(report)
    assert "ran more than once" in printed
    assert "Tracing itself is working" in printed


def test_a_report_with_an_enabled_but_unhealthy_sdk_shows_the_reason() -> None:
    """An enabled SDK can still have a part that is not working, such as a provider
    that cannot be found. The printed report must name that part."""
    report = Report(
        status=Status(
            enabled=True,
            deployment="dep_abc",
            release=_RELEASE,
            destinations=["convergent"],
            reason="no_provider",
        ),
        round_trip="no_credentials",
    )
    printed = str(report)
    assert "convergent.otel.install" in printed


def test_check_makes_no_request_when_tracing_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """No ``init()`` has claimed this process, which is the state a caller who put
    ``check()`` at the top of their startup script is in."""
    calls = _record_gets(monkeypatch, _HEALTHY)

    report = convergent.check()

    assert calls == [], "there are no credentials to make the call with"
    assert report.status.enabled is False
    assert report.round_trip == "no_credentials"


def test_check_makes_no_request_for_a_file_only_process(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A sandbox writing spans to a file is enabled and still has nobody to ask."""
    calls = _record_gets(monkeypatch, _HEALTHY)

    convergent.init(destinations=[convergent.File(tmp_path)], release=_RELEASE)
    report = convergent.check()

    assert calls == []
    assert report.status.enabled is True
    assert report.round_trip == "no_credentials"


def test_check_prints_a_note_code_it_does_not_recognize(
    monkeypatch: pytest.MonkeyPatch, enabled: None
) -> None:
    """The wire code is an open string on purpose: a server newer than the SDK must
    still be able to tell a customer something."""
    _record_gets(
        monkeypatch,
        {
            **_HEALTHY,
            "notes": [
                {
                    "code": "invented_after_this_sdk_shipped",
                    "message": (
                        'Spans from "support-agent" carry no gen_ai.request.model, so '
                        "cost and model comparison are unavailable for it."
                    ),
                }
            ],
        },
    )

    report = convergent.check()

    assert "\n".join(
        [
            "  1 note",
            '    - Spans from "support-agent" carry no gen_ai.request.model, so cost and',
            "      model comparison are unavailable for it.",
        ]
    ) in str(report)
    assert [note.code for note in report.notes] == ["invented_after_this_sdk_shipped"]


def test_a_truncated_agent_list_says_there_are_more(
    monkeypatch: pytest.MonkeyPatch, enabled: None
) -> None:
    _record_gets(monkeypatch, {**_HEALTHY, "agents_truncated": True})

    report = convergent.check()

    assert report.agents_truncated is True
    assert "and more the server did not list" in str(report)


def test_the_printed_report_reads_like_the_readme() -> None:
    report = Report(
        status=Status(
            enabled=True,
            deployment="dep_4f11938ad78945df",
            release=_RELEASE,
            destinations=["convergent", "file:/data/spans.jsonl"],
            mode="attached",
        ),
        round_trip="ok",
        round_trip_ms=142,
        endpoint=_ENDPOINT,
        organization_id="org_7c2b1a9e4d",
        agents=["support-agent", "billing-agent"],
    )

    assert str(report) == "\n".join(
        [
            "convergent: enabled",
            "  release     a3f21c9",
            "  mode        attached to a tracer provider you own",
            "  sending to  convergent, file:/data/spans.jsonl",
            "",
            "  round trip  ok (142ms)",
            "  key         org_7c2b1a9e4d",
            "  agents      support-agent, billing-agent",
            "",
            "  no notes",
        ]
    )


def test_the_printed_report_of_a_disabled_sdk_says_what_to_set() -> None:
    assert str(convergent.check()) == "\n".join(
        [
            "convergent: disabled",
            "  reason      missing_config",
            "              init() or install() has not run in this process; a call that",
            "              raised configured nothing. Set CONVERGENT_API_KEY. For init() you",
            "              can also set CONVERGENT_SPANS_DIR to write spans to a file",
            "              instead.",
            "",
            "  round trip  not attempted, there is no api key to check with",
        ]
    )


def test_a_report_never_invents_a_release() -> None:
    report = Report(status=Status(enabled=True, destinations=["convergent"]))

    assert "  release     not set" in str(report)


def test_a_note_missing_half_its_fields_is_not_a_note(
    monkeypatch: pytest.MonkeyPatch, enabled: None
) -> None:
    _record_gets(monkeypatch, {**_HEALTHY, "notes": [{"code": "no_deployment"}, "junk"]})

    report = convergent.check()

    assert report.notes == []
    assert report.round_trip == "ok", "one bad row does not lose the rest of the report"


def test_an_unnamed_linked_agent_is_listed_by_id(
    monkeypatch: pytest.MonkeyPatch, enabled: None
) -> None:
    _record_gets(monkeypatch, {**_HEALTHY, "agents": [{"agent_id": "agt_9", "name": None}]})

    report = convergent.check()

    assert report.agents == ["agt_9"]


def test_status_echoes_the_running_filters(tmp_path: Path) -> None:
    """A bare scalar echoes as a one-value list: the echo is what runs, not what was typed."""
    status = convergent.init(
        release=_RELEASE,
        destinations=[convergent.File(str(tmp_path))],
        require_span_attributes={"customer.id": "acme"},
        reject_span_attributes={"tier": ["test", "internal"]},
    )

    assert status.require_span_attributes == {"customer.id": ["acme"]}
    assert status.reject_span_attributes == {"tier": ["internal", "test"]}


def test_status_echoes_filters_set_by_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CONVERGENT_REQUIRE_SPAN_ATTRIBUTES", '{"customer.id": ["acme"]}')

    status = convergent.init(release=_RELEASE, destinations=[convergent.File(str(tmp_path))])

    assert status.require_span_attributes == {"customer.id": ["acme"]}
    assert status.reject_span_attributes is None


def test_a_filter_argument_beats_its_environment_variable_in_the_echo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CONVERGENT_REQUIRE_SPAN_ATTRIBUTES", '{"customer.id": ["globex"]}')

    status = convergent.init(
        release=_RELEASE,
        destinations=[convergent.File(str(tmp_path))],
        require_span_attributes={"customer.id": ["acme"]},
    )

    assert status.require_span_attributes == {"customer.id": ["acme"]}


def test_install_status_echoes_the_running_filters(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        _transport, "build_processor", lambda **_: SimpleSpanProcessor(InMemorySpanExporter())
    )
    monkeypatch.setattr(_registry, "post_json", lambda *_, **__: {"deployment_id": "dep_abc"})
    convergent.otel.install(
        TracerProvider(),
        api_key=_KEY,
        endpoint=_ENDPOINT,
        release=_RELEASE,
        reject_span_attributes={"customer.id": ["initech"]},
    )
    _record_gets(monkeypatch, _HEALTHY)

    report = convergent.check()

    assert report.status.reject_span_attributes == {"customer.id": ["initech"]}
    assert report.status.require_span_attributes is None


def test_the_printed_report_names_the_running_filters() -> None:
    report = Report(
        Status(
            enabled=True,
            release=_RELEASE,
            destinations=["convergent"],
            require_span_attributes={"customer.id": ["acme"]},
            reject_span_attributes={"tier": ["internal", "test"]},
        )
    )

    line = next(line for line in str(report).splitlines() if line.lstrip().startswith("filters"))

    assert "reject tier=internal|test" in line, "reject prints first because reject wins"
    assert "require customer.id=acme" in line


def test_a_report_with_no_filters_has_no_filters_row() -> None:
    report = Report(Status(enabled=True, release=_RELEASE, destinations=["convergent"]))

    assert "filters" not in str(report)
