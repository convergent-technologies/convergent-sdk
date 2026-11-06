"""Convergent's Python SDK.

The names
bind eagerly with relative imports, so the package also works mounted under a
second top-level name (``tests/test_repackaging.py`` holds that guarantee).
This file is maintained by hand; a check keeps ``__all__`` complete.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

from . import _version
from ._artifacts import ArtifactOrigin
from ._check import Note, Report, check
from ._core import FlushResult, Status, flush, init, tracer_provider
from ._destinations import Console, Destination, File
from ._propagation import public_propagator as propagator
from ._semantic import (
    ContextAttributes,
    SpanHandle,
    TraceRef,
    agent,
    current_span,
    current_trace,
    observe,
    session,
    span,
    tool,
)

__version__ = _version.sdk_version()

__all__ = [
    "__version__",
    "init",
    "check",
    "observe",
    "session",
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
    "ContextAttributes",
    "ArtifactOrigin",
    "Report",
    "Note",
    "propagator",
]

#: Public submodules, reachable as ``convergent.<name>`` after ``import
#: convergent`` alone. Kept out of ``__all__``, which lists the calls and types.
__submodules__ = ["otel", "carrier", "temporal"]


def __getattr__(name: str) -> Any:
    if name in __submodules__:
        value = import_module(f".{name}", __name__)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
