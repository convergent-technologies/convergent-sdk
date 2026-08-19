from __future__ import annotations

import subprocess
import sys

import convergent
import convergent as sdk

EXPECTED = [
    "__version__",
    "init",
    "check",
    "observe",
    # Decorator aliases for the two operations nearly every integration writes.
    # agent(name) keeps the name explicit; tool() may take it from __name__.
    "agent",
    "tool",
    "span",
    "flush",
    # What flush() returns and what span() yields. Exported so a caller can
    # annotate a variable without reaching into private modules.
    "FlushResult",
    "tracer_provider",
    "File",
    "Console",
    # The union of what init(destinations=...) accepts. Exported so a caller can
    # annotate their own list; it is a type, not an eighth callable.
    "Destination",
    "Status",
    "current_trace",
    # The active span as a guarded handle, for code inside a decorated function.
    "current_span",
    "TraceRef",
    "SpanHandle",
    # The union context_attributes= accepts on the decorators: the pairs, or a
    # callable resolved from the decorated function's own arguments per call.
    "ContextAttributes",
    "Report",
    "Note",
]
#: Public submodules. ``otel`` holds ConvergentSpanProcessor, which a caller adds
#: to a tracer provider they own. It stays out of ``__all__``, which lists calls
#: and types rather than modules.
EXPECTED_SUBMODULES = ["otel"]
#: ``install`` adds the processor to a provider and hands that provider over.
#: ``ConvergentSpanProcessor`` is the same thing for a caller placing it by hand.
EXPECTED_OTEL = ["ConvergentSpanProcessor", "install"]
REMOVED = (
    "AlreadyInitializedError",
    "NoSupportedFrameworkError",
    "NotInitializedError",
    "agent_capability",
    # Requests are marked with span(context_attributes=...), not a call of its own.
    "attributes",
    "get_current_span",
    "instrument",
    "span_processor",
)


def test_public_surface_contains_only_the_v01_functions() -> None:
    assert convergent.__all__ == EXPECTED
    assert sdk.__all__ == EXPECTED
    for name in EXPECTED:
        assert getattr(convergent, name) is getattr(sdk, name)
    for name in REMOVED:
        assert not hasattr(convergent, name)
        assert not hasattr(sdk, name)


def test_the_otel_submodule_is_reachable_both_ways() -> None:
    """``docs/reference/api.md`` writes ``convergent.otel.ConvergentSpanProcessor``
    after a bare ``import convergent``, and a caller may import the name directly
    instead. The first form needs its own interpreter, because importing the
    submodule anywhere in this one would set the attribute and hide a broken
    lookup."""
    from opentelemetry.sdk.trace import SpanProcessor

    from convergent.otel import ConvergentSpanProcessor

    assert convergent.__submodules__ == EXPECTED_SUBMODULES
    assert convergent.otel.__all__ == EXPECTED_OTEL
    assert issubclass(ConvergentSpanProcessor, SpanProcessor), "add_span_processor takes one"

    result = subprocess.run(
        [sys.executable, "-c", "import convergent; print(convergent.otel.__all__)"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == str(EXPECTED_OTEL)


def test_version_is_a_nonempty_string() -> None:
    """The installed distribution's version, or ``0.0.0`` in a source checkout
    that has no convergent-sdk distribution to ask."""
    assert isinstance(convergent.__version__, str)
    assert convergent.__version__
    assert convergent.__version__ is sdk.__version__


def test_importing_convergent_does_not_load_pydantic_ai() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import convergent; print('pydantic_ai' in sys.modules)",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "False"
