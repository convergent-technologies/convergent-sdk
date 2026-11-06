"""litellm's second copy of a model call's request, left out before export.

litellm's ``otel`` callback opens a ``raw_gen_ai_request`` child under each model
call span and writes every provider request field to it as
``llm.<provider>.<field>``. For a Responses API call ``llm.<provider>.input``
repeats the request messages the model call already holds in
``gen_ai.input.messages``, images included, so an export carries them twice.

The child's copy is left out when its model call is in the same export batch and
holds every message whole. Convergent's ingest applies the same rule to spans
from SDKs that still send the copy, and one case table in Convergent's tests runs
both rules, so the two sides agree on what a copy is.
Chat completions ``messages`` stay: the model call does not say which API it used.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping, Sequence
from typing import Any, Final

from opentelemetry.sdk.trace import ReadableSpan

from ._telemetry_keys import GEN_AI_INPUT_MESSAGES

logger = logging.getLogger("convergent.sdk")

_RAW_REQUEST_SPAN: Final = "raw_gen_ai_request"
_REQUEST_MESSAGES: Final = re.compile(r"llm\.[^.]+\.input")
_EMPTY_TEXT_PART: Final = {"type": "text", "content": ""}
_warned = False


def without_request_copies(spans: Sequence[ReadableSpan]) -> Sequence[ReadableSpan]:
    """``spans`` with each raw request child's copy of its parent's messages left out.

    Span attributes are frozen once a span ends, so a child that loses its copy
    is replaced by a clone without it. Every other span is passed through as is.
    A failure here sends the batch unchanged: a smaller export never costs one.
    It runs once per destination, so a batch with no raw request child returns
    before any span is indexed or any message decoded.
    """
    global _warned
    if not any(span.name == _RAW_REQUEST_SPAN for span in spans):
        return spans
    try:
        return _without_copies(spans)
    except Exception:
        if not _warned:
            _warned = True
            logger.warning("Convergent could not trim litellm's request copies", exc_info=True)
        return spans


def _without_copies(spans: Sequence[ReadableSpan]) -> list[ReadableSpan]:
    by_id = {(span.context.trace_id, span.context.span_id): span for span in spans if span.context}
    kept: list[ReadableSpan] = []
    for span in spans:
        parent = (
            by_id.get((span.parent.trace_id, span.parent.span_id))
            if span.parent is not None
            else None
        )
        copied = _copied_keys(span, parent) if parent is not None else ()
        kept.append(_without(span, copied) if copied else span)
    return kept


def _copied_keys(span: ReadableSpan, parent: ReadableSpan) -> tuple[str, ...]:
    scope = _scope_name(span)
    if span.name != _RAW_REQUEST_SPAN or scope is None or _scope_name(parent) != scope:
        return ()
    if scope != "litellm" and not scope.startswith("litellm."):
        return ()
    messages = _decoded((parent.attributes or {}).get(GEN_AI_INPUT_MESSAGES))
    if not messages or not all(_is_whole(message) for message in messages):
        return ()
    return tuple(key for key in span.attributes or {} if _REQUEST_MESSAGES.fullmatch(key))


def _scope_name(span: ReadableSpan) -> str | None:
    scope = span.instrumentation_scope
    return scope.name if scope is not None else None


def _decoded(value: object) -> list[Any] | None:
    if not isinstance(value, str):
        return None
    try:
        decoded = json.loads(value)
    except ValueError:
        return None
    return decoded if isinstance(decoded, list) else None


def _is_whole(message: object) -> bool:
    """Whether litellm recorded ``message`` on the model call without loss.

    litellm records a Responses API item with no role (a function call, its
    output, a reasoning item) as a user message with no parts or one empty text
    part, and a remote image as its URL where the raw request has the bytes.
    """
    if not isinstance(message, Mapping):
        return False
    recorded = message.get("parts") or []
    if not isinstance(recorded, list):
        return False
    parts = [part for part in recorded if part != _EMPTY_TEXT_PART]
    if not (parts or message.get("tool_calls")):
        return False
    for part in parts:
        image = part.get("image_url") if isinstance(part, Mapping) else None
        url = image.get("url") if isinstance(image, Mapping) else image
        if url is not None and not str(url).startswith("data:"):
            return False
    return True


def _without(span: ReadableSpan, keys: Sequence[str]) -> ReadableSpan:
    attributes = {key: value for key, value in (span.attributes or {}).items() if key not in keys}
    return ReadableSpan(
        name=span.name,
        context=span.context,
        parent=span.parent,
        resource=span.resource,
        attributes=attributes,
        events=span.events,
        links=span.links,
        kind=span.kind,
        status=span.status,
        start_time=span.start_time,
        end_time=span.end_time,
        instrumentation_scope=span.instrumentation_scope,
    )


__all__ = ["without_request_copies"]
