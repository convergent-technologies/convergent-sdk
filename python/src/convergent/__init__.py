"""Convergent's Python SDK.

The names
bind eagerly with relative imports, so the package also works mounted under a
second top-level name (``tests/test_repackaging.py`` holds that guarantee).
This file is maintained by hand; a check keeps ``__all__`` complete.
"""

from __future__ import annotations

from importlib import import_module
from importlib.metadata import PackageNotFoundError, version
from typing import Any

from ._check import Note, Report, check
from ._core import FlushResult, Status, flush, init, tracer_provider
from ._destinations import Console, Destination, File
from ._semantic import (
    SpanHandle,
    TraceRef,
    agent,
    current_span,
    current_trace,
    observe,
    span,
    tool,
)

try:
    __version__ = version("convergent-sdk")
except PackageNotFoundError:
    # A checkout that runs the SDK from source has no convergent-sdk
    # distribution to ask.
    __version__ = "0.0.0"

__all__ = [
    "__version__",
    "init",
    "check",
    "observe",
    "agent",
    "tool",
    "span",
    "flush",
    "FlushResult",
    "tracer_provider",
    "File",
    "Console",
    "Destination",
    "Status",
    "current_trace",
    "current_span",
    "TraceRef",
    "SpanHandle",
    "Report",
    "Note",
]

#: Public submodules, reachable as ``convergent.<name>`` after ``import
#: convergent`` alone. Kept out of ``__all__``, which lists the calls and types.
__submodules__ = ["otel"]


def __getattr__(name: str) -> Any:
    if name in __submodules__:
        value = import_module(f".{name}", __name__)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
