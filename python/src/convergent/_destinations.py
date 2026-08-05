"""Where spans go, besides Convergent.

``api_key`` already implies the Convergent destination. ``endpoint`` can override
its managed address. The same address registers the deployment and receives
spans. Anything in ``init(destinations=...)`` is *added* on top, and every span
goes to all of them.

These are inert descriptions, not exporters. ``_core`` turns each one into a span
processor when ``init()`` runs, so constructing one opens no descriptor and makes
no network call. What it does check is its own arguments: a value that cannot
work raises here, at startup, on the caller's own thread.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from ._console_export import StreamName
from ._file_export import SPANS_FILENAME


def _type_name(value: object) -> str:
    """The type of a rejected value, for the message that rejects it.

    The type and never ``repr(value)``: an api key, a prompt, or a whole
    conversation would otherwise reach a log line and a traceback. The module is
    named too, because two packages can hold one class name --
    ``opentelemetry.trace.TracerProvider`` and
    ``opentelemetry.sdk.trace.TracerProvider`` are the pair that matters here.

    Here rather than in ``_config`` because ``_config`` imports this module, so
    the other placement is an import cycle.
    """
    kind = type(value)
    if kind.__module__ == "builtins":
        return kind.__qualname__
    return f"{kind.__module__}.{kind.__qualname__}"


@dataclass(frozen=True)
class File:
    """Write every span to ``<path>/<filename>`` as OTLP/JSON, one span per line.

    A ``File`` on its own is a complete configuration: no credentials, and
    nothing sent over the network at all. That is what makes a sandbox with no
    route to the receiver traceable -- the file is how the trace gets out, and
    someone outside collects it.

    ``filename`` exists so several processes can share one directory.

    ``mode`` is the file's permission bits, owner read and write by default. The
    file holds whatever the spans carry, which for an auto-instrumented agent is
    every prompt and completion, so widen it only when you know who else can read
    the directory.

    ``filename`` must be a bare name, because it is joined with ``path`` and both
    escapes are otherwise silent: ``"../spans.jsonl"`` climbs out of the
    directory, and an absolute name discards ``path`` entirely -- ``Path.__truediv__``
    drops its left side when the right is absolute. That would put prompts
    somewhere other than where this docstring promises.

    Raises:
        TypeError: ``path`` is not a string or ``os.PathLike``, ``filename`` is not
            a string, or ``mode`` is not an int.
        ValueError: ``filename`` is a path rather than a bare name.
    """

    path: str | os.PathLike[str]
    filename: str = field(default=SPANS_FILENAME, kw_only=True)
    mode: int = field(default=0o600, kw_only=True)

    def __post_init__(self) -> None:
        if not isinstance(self.path, str | os.PathLike):
            raise TypeError(
                f"File(path=) takes a string or os.PathLike, got {_type_name(self.path)}"
            )
        if not isinstance(self.filename, str):
            raise TypeError(f"File(filename=) takes a string, got {_type_name(self.filename)}")
        if not isinstance(self.mode, int) or isinstance(self.mode, bool):
            raise TypeError(
                f"File(mode=) takes an int of permission bits, got {_type_name(self.mode)}"
            )
        if not 0 <= self.mode <= 0o777:
            raise ValueError(f"File(mode=) takes permission bits in 0..0o777, got {self.mode:#o}")
        bare = os.path.basename(self.filename)
        if bare != self.filename or bare in ("", ".", ".."):
            raise ValueError(
                f"File(filename={self.filename!r}) takes a bare file name and not a path; "
                "it is joined with path=, so a name that climbs out of that directory or "
                f"replaces it would write spans elsewhere. The default is {SPANS_FILENAME!r}."
            )


@dataclass(frozen=True)
class Console:
    """Write every span to stdout or stderr as OTLP/JSON.

    Two uses. While developing, it shows exactly what is being sent without
    standing up a collector. In Lambda, Cloud Run, or Modal it is a *transport*:
    those platforms collect stdout off the container, so it is the one channel
    that still works when the process cannot open a socket and nobody will fetch
    a file.

    ``pretty`` indents for reading. The compact default is the same shape a spans
    file has, so captured output can be read back by the same reader.

    ``stream`` names one of the two streams, checked here because the ``Literal``
    annotation holds no runtime weight and an unknown name would otherwise fail at
    export time, after ``init()`` reported a healthy setup.

    Raises:
        TypeError: ``pretty`` is not a bool.
        ValueError: ``stream`` is neither ``"stdout"`` nor ``"stderr"``.
    """

    stream: StreamName = field(default="stdout", kw_only=True)
    pretty: bool = field(default=False, kw_only=True)

    def __post_init__(self) -> None:
        if not isinstance(self.pretty, bool):
            raise TypeError(f"Console(pretty=) takes a bool, got {_type_name(self.pretty)}")
        if self.stream not in ("stdout", "stderr"):
            raise ValueError(
                f"Console(stream={self.stream!r}) takes 'stdout' or 'stderr'; an unknown "
                "stream would lose every span written to it."
            )


#: Anything accepted in ``init(destinations=...)``. Lives here rather than in
#: ``_core`` so a caller can actually import it to annotate their own list.
Destination = File | Console

__all__ = ["Console", "Destination", "File"]
