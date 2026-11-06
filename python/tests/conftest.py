"""Shared fixtures for the unit tests.

This file is owned by this repo; it is maintained by hand.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest


@pytest.fixture(autouse=True)
def _isolate_otel_service_name() -> Iterator[None]:
    """Undo any OTEL_SERVICE_NAME a test leaves on the real environment.

    ``init()`` can set it with ``os.environ.setdefault``, which monkeypatch
    cannot undo for a variable that did not exist before, so a test that
    reaches that path would otherwise leak the name into every later test.
    """
    original = os.environ.pop("OTEL_SERVICE_NAME", None)
    yield
    os.environ.pop("OTEL_SERVICE_NAME", None)
    if original is not None:
        os.environ["OTEL_SERVICE_NAME"] = original


@pytest.fixture(autouse=True)
def _no_workspace_tag_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the workspace-tag request off the network in-process.

    ``init()`` and ``otel.install()`` send ``GET /v1/check`` on a thread of their
    own, which would race tests that count check requests. test_sdk_tag_fetch.py
    runs the real fetch.
    """
    monkeypatch.setattr("convergent._tag_fetch.fetch_workspace_tag", lambda *_: None)
