"""The installed SDK version, read by ``__version__``, the exporter and ``_user_agent``."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version


def sdk_version() -> str:
    # convergent-sdk is the published package; the monorepo ships the same
    # modules inside its convergent distribution, so that name comes second.
    for distribution in ("convergent-sdk", "convergent"):
        try:
            return version(distribution)
        except PackageNotFoundError:
            continue
    # Run from source, with neither distribution installed.
    return "0.0.0"
