#!/usr/bin/env python3
"""Show one agent subtree from a Convergent OTLP/JSONL recording."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

AGENT_RUN = "invoke_agent"
MODEL_CALL = "chat"
TOOL_CALL = "execute_tool"
LITELLM_SCOPE = "litellm"
TREE_LINE_CAP = 60

OPERATION_ROLES = {
    AGENT_RUN: AGENT_RUN,
    MODEL_CALL: MODEL_CALL,
    TOOL_CALL: TOOL_CALL,
    "text_completion": MODEL_CALL,
    "generate_content": MODEL_CALL,
    "invoke_workflow": "step",
    "retrieval": "step",
    "embeddings": "step",
    "create_agent": "step",
}

LITELLM_OPERATIONS = {
    "completion": MODEL_CALL,
    "acompletion": MODEL_CALL,
    "completion_with_retries": MODEL_CALL,
    "responses": MODEL_CALL,
    "aresponses": MODEL_CALL,
    "atext_completion": "text_completion",
    "embedding": "embeddings",
    "aembedding": "embeddings",
}

SUMMARY_KEYS = {
    AGENT_RUN: (
        "gen_ai.agent.name",
        "gen_ai.agent.version",
        "gen_ai.conversation.id",
    ),
    MODEL_CALL: (
        "gen_ai.request.model",
        "gen_ai.usage.input_tokens",
        "gen_ai.usage.output_tokens",
    ),
    TOOL_CALL: ("gen_ai.tool.name", "gen_ai.tool.call.id"),
}

MARK_PREFIX = "convergent.attributes."

CONTENT_KEYS = {
    "gen_ai.input.messages": "input",
    "gen_ai.output.messages": "output",
    "gen_ai.tool.call.arguments": "arguments",
    "gen_ai.tool.call.result": "result",
}


class RecordingError(Exception):
    """The requested recording cannot be rendered."""


def role_of(operation: str, scope: str) -> str:
    role = OPERATION_ROLES.get(operation, "")
    if role:
        return role
    if scope == LITELLM_SCOPE or scope.startswith(f"{LITELLM_SCOPE}."):
        return OPERATION_ROLES.get(LITELLM_OPERATIONS.get(operation, ""), "")
    return ""


@dataclass(frozen=True)
class Span:
    trace_id: str
    span_id: str
    parent_id: str
    name: str
    operation: str
    scope: str
    attributes: dict[str, object]
    resource: dict[str, object]
    start_ns: int
    end_ns: int
    status: str

    @property
    def role(self) -> str:
        return role_of(self.operation, self.scope)

    @property
    def agent_name(self) -> str | None:
        value = self.attributes.get("gen_ai.agent.name")
        return value if isinstance(value, str) and value else None

    @property
    def duration_ms(self) -> float | None:
        if not self.start_ns or not self.end_ns or self.end_ns < self.start_ns:
            return None
        return (self.end_ns - self.start_ns) / 1_000_000


def _value(raw: object) -> object:
    if not isinstance(raw, dict):
        return raw
    for key in ("stringValue", "boolValue", "doubleValue"):
        if key in raw:
            return raw[key]
    if "intValue" in raw:
        value = raw["intValue"]
        return int(value) if isinstance(value, (str, int)) else value
    if "arrayValue" in raw:
        value = raw["arrayValue"]
        items = value.get("values", []) if isinstance(value, dict) else []
        return [_value(item) for item in items]
    if "kvlistValue" in raw:
        value = raw["kvlistValue"]
        items = value.get("values", []) if isinstance(value, dict) else []
        return _attributes(items)
    return raw.get("bytesValue")


def _attributes(items: object) -> dict[str, object]:
    if not isinstance(items, list):
        return {}
    return {
        item["key"]: _value(item.get("value", {}))
        for item in items
        if isinstance(item, dict) and isinstance(item.get("key"), str)
    }


def _paths(target: Path) -> list[Path]:
    if target.is_dir():
        paths = sorted(target.rglob("spans*.jsonl"))
        if not paths:
            raise RecordingError(f"no spans*.jsonl file exists under {target}")
        return paths
    if not target.is_file():
        raise RecordingError(f"no such file: {target}")
    return [target]


def read_recording(target: Path) -> tuple[list[Path], list[Span]]:
    paths = _paths(target)
    spans: list[Span] = []
    for path in paths:
        with path.open(encoding="utf-8", errors="replace") as recording:
            for number, line in enumerate(recording, start=1):
                if not line.strip():
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError as error:
                    raise RecordingError(
                        f"{path} line {number} is not valid JSON: {error.msg}"
                    ) from error
                if not isinstance(payload, dict):
                    raise RecordingError(f"{path} line {number} is not an OTLP object")
                for resource_span in payload.get("resourceSpans", []) or []:
                    resource = _attributes((resource_span.get("resource") or {}).get("attributes"))
                    for scope_span in resource_span.get("scopeSpans", []) or []:
                        scope = ((scope_span.get("scope") or {}).get("name")) or "unknown"
                        for raw in scope_span.get("spans", []) or []:
                            attributes = _attributes(raw.get("attributes"))
                            operation = attributes.get("gen_ai.operation.name")
                            status = (raw.get("status") or {}).get("code") or "STATUS_CODE_UNSET"
                            spans.append(
                                Span(
                                    trace_id=str(raw.get("traceId") or ""),
                                    span_id=str(raw.get("spanId") or ""),
                                    parent_id=str(raw.get("parentSpanId") or ""),
                                    name=str(raw.get("name") or "(unnamed)"),
                                    operation=operation if isinstance(operation, str) else "",
                                    scope=str(scope),
                                    attributes=attributes,
                                    resource=resource,
                                    start_ns=int(raw.get("startTimeUnixNano") or 0),
                                    end_ns=int(raw.get("endTimeUnixNano") or 0),
                                    status=str(status),
                                )
                            )
    if not spans:
        raise RecordingError("the recording contains no spans")
    return paths, spans


def select(spans: list[Span], agent: str | None) -> tuple[list[Span], list[Span]]:
    agent_spans = [span for span in spans if span.role == AGENT_RUN and span.agent_name]
    names = sorted({span.agent_name for span in agent_spans if span.agent_name})
    if agent is None:
        if len(names) != 1:
            candidates = ", ".join(_clip(name) for name in names) if names else "none"
            raise RecordingError(f"pass --agent with one of: {candidates}")
        agent = names[0]
    roots = [span for span in agent_spans if span.agent_name == agent]
    if not roots:
        candidates = ", ".join(_clip(name) for name in names) if names else "none"
        raise RecordingError(f"agent {agent!r} was not recorded; candidates: {candidates}")

    children: dict[tuple[str, str], list[Span]] = defaultdict(list)
    for span in spans:
        children[(span.trace_id, span.parent_id)].append(span)

    selected: dict[tuple[str, str], Span] = {}
    pending = list(reversed(roots))
    while pending:
        span = pending.pop()
        key = (span.trace_id, span.span_id)
        if key in selected:
            continue
        selected[key] = span
        pending.extend(reversed(children.get(key, [])))

    return roots, sorted(selected.values(), key=lambda span: (span.trace_id, span.start_ns))


def _clip(value: object, limit: int = 160) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=repr)
    text = " ".join(text.split())
    text = "".join(
        character if character.isprintable() else repr(character)[1:-1] for character in text
    )
    return text if len(text) <= limit else f"{text[:limit]}... [{len(text)} chars]"


def _summary(span: Span) -> str:
    parts: list[str] = []
    for key in SUMMARY_KEYS.get(span.role, ()):
        value = span.attributes.get(key)
        label = key.removeprefix("gen_ai.")
        parts.append(f"{label}={_clip(value)}" if value is not None else f"{label}=MISSING")
    for key in sorted(span.attributes):
        if key.startswith(MARK_PREFIX):
            parts.append(f"{key.removeprefix('convergent.')}={_clip(span.attributes[key])}")
    duration = span.duration_ms
    parts.append(f"duration={duration:.0f}ms" if duration is not None else "duration=MISSING")
    if span.status not in {"STATUS_CODE_UNSET", "STATUS_CODE_OK", "0", "1"}:
        parts.append(f"status={_clip(span.status)}")
    return "  ".join(parts)


def counts(spans: list[Span], total: int) -> list[str]:
    roles = Counter(span.role for span in spans)
    operations = Counter(span.operation or "(unset)" for span in spans)
    scopes = Counter(span.scope for span in spans)
    models = [span for span in spans if span.role == MODEL_CALL]
    with_usage = sum(
        1
        for span in models
        if "gen_ai.usage.input_tokens" in span.attributes
        or "gen_ai.usage.output_tokens" in span.attributes
    )
    releases = sorted(
        {
            value
            for span in spans
            for value in (
                span.attributes.get("gen_ai.agent.version"),
                span.resource.get("service.version"),
            )
            if isinstance(value, str) and value
        }
    )
    marks = Counter(
        f"{key.removeprefix(MARK_PREFIX)}={_clip(value)}"
        for span in spans
        for key, value in span.attributes.items()
        if key.startswith(MARK_PREFIX)
    )
    return [
        # The whole-file count sits beside the selection so a filter that
        # withheld another agent's spans is visible from this line alone.
        f"selected spans: {len(spans)} of {total} in the file",
        f"traces: {len({span.trace_id for span in spans})}",
        f"agent runs: {roles[AGENT_RUN]}",
        f"model calls: {roles[MODEL_CALL]}",
        f"model calls with token usage: {with_usage}",
        f"tool calls: {roles[TOOL_CALL]}",
        "releases: " + (", ".join(_clip(release) for release in releases) if releases else "none"),
        "marks: "
        + (
            ", ".join(f"{mark} ({count} spans)" for mark, count in sorted(marks.items()))
            if marks
            else "none"
        ),
        "operations: "
        + ", ".join(f"{_clip(key)}={value}" for key, value in sorted(operations.items())),
        "scopes: " + ", ".join(f"{_clip(key)}={value}" for key, value in sorted(scopes.items())),
    ]


def _tree_index(
    spans: list[Span],
) -> tuple[list[Span], dict[tuple[str, str], list[Span]]]:
    selected = {(span.trace_id, span.span_id) for span in spans}
    children: dict[tuple[str, str], list[Span]] = defaultdict(list)
    roots: list[Span] = []
    for span in spans:
        parent = (span.trace_id, span.parent_id)
        if span.parent_id and parent in selected:
            children[parent].append(span)
        else:
            roots.append(span)
    for group in children.values():
        group.sort(key=lambda span: span.start_ns)
    return roots, children


def _content_lines(span: Span, pad: str, show_content: bool) -> list[str]:
    content = [
        (CONTENT_KEYS[key], span.attributes[key]) for key in CONTENT_KEYS if key in span.attributes
    ]
    if not content:
        return []
    if not show_content:
        return [f"{pad}content fields: {len(content)} hidden"]
    return [f"{pad}{label}: {_clip(value)}" for label, value in content]


def render_tree(spans: list[Span], *, show_content: bool, full: bool) -> list[str]:
    roots, children = _tree_index(spans)

    lines: list[str] = []
    limit = None if full else TREE_LINE_CAP
    pending = [
        (root, "", True, True)
        for root in reversed(sorted(roots, key=lambda span: (span.trace_id, span.start_ns)))
    ]
    truncated = False
    while pending:
        if limit is not None and len(lines) >= limit:
            truncated = True
            break
        span, prefix, last, top = pending.pop()
        connector = "" if top else ("`-- " if last else "|-- ")
        lines.append(f"{prefix}{connector}{_clip(span.name)}    {_summary(span)}".rstrip())
        pad = prefix + ("    " if top or last else "|   ")
        lines.extend(_content_lines(span, pad, show_content))
        kids = children.get((span.trace_id, span.span_id), [])
        next_prefix = prefix if top else prefix + ("    " if last else "|   ")
        for index in range(len(kids) - 1, -1, -1):
            pending.append((kids[index], next_prefix, index == len(kids) - 1, False))

    if limit is not None and len(lines) > limit:
        lines = lines[:limit]
        truncated = True
    if truncated:
        lines.append(f"... tree capped at {TREE_LINE_CAP} lines; pass --full to show all")
    return lines


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", type=Path, help="a spans file or directory")
    parser.add_argument("--agent", help="the gen_ai.agent.name subtree to render")
    parser.add_argument("--show-content", action="store_true", help="show recorded content values")
    parser.add_argument("--full", action="store_true", help="show the full tree")
    args = parser.parse_args(argv)

    try:
        paths, spans = read_recording(args.target)
        roots, selected = select(spans, args.agent)
    except (AttributeError, OSError, RecordingError, TypeError, ValueError) as error:
        print(f"error: {error}")
        return 2

    names = sorted({root.agent_name for root in roots if root.agent_name})
    print("read: " + ", ".join(_clip(str(path)) for path in paths))
    print(
        f"selected agent: {', '.join(_clip(name) for name in names)} "
        f"({len(roots)} run{'s' if len(roots) != 1 else ''})"
    )
    print("counts:")
    for line in counts(selected, total=len(spans)):
        print(f"  {line}")
    print("tree:")
    for line in render_tree(selected, show_content=args.show_content, full=args.full):
        print(f"  {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
