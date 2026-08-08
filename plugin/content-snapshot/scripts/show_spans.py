#!/usr/bin/env python3
"""Show what a spans.jsonl file recorded.

Usage:
    python show_spans.py <dir-or-file> [--show-content]
        [--expect-agents a,b | --expect-agents 2]
        [--expect-tools search,fetch | --expect-tools 1]

Reads newline delimited OTLP/JSON, the format written by a file destination, prints
a summary of what was recorded, and draws each trace as a tree. Compare what it
shows with what you meant to record, or name the agents and tools from your
coverage plan, or say how many of each there should be, and let it do that
comparison. Standard library only. Exits nonzero when there is no file to read,
when a name you expected has no span, when a count you expected falls short, and
when the file itself shows a problem: one call recorded twice by two tracers, either
nested or side by side, or usage and message data under an operation name nothing
reads.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

AGENT_RUN = "invoke_agent"
MODEL_CALL = "chat"
TOOL_CALL = "execute_tool"

#: Every span the SDK itself starts comes from a tracer with this name. Any other
#: scope belongs to an instrumentation package, which decides where its own spans
#: open and close.
SDK_SCOPE = "convergent.sdk"

#: The operation names the workspace reads usage and message data from.
KNOWN_OPERATIONS = frozenset(
    {AGENT_RUN, MODEL_CALL, TOOL_CALL, "text_completion", "generate_content"}
)

#: The keys that mark a span as carrying model-call data.
DATA_KEYS = (
    "gen_ai.usage.input_tokens",
    "gen_ai.usage.output_tokens",
    "gen_ai.input.messages",
    "gen_ai.output.messages",
)


@dataclass
class Span:
    trace_id: str
    span_id: str
    parent_id: str
    name: str
    operation: str
    attributes: dict[str, object]
    resource: dict[str, object]
    scope: str
    start_ns: int = 0
    end_ns: int = 0

    @property
    def duration_ms(self) -> float:
        return (self.end_ns - self.start_ns) / 1_000_000 if self.end_ns > self.start_ns else 0.0

    @property
    def agent_name(self) -> str | None:
        value = self.attributes.get("gen_ai.agent.name")
        return value if isinstance(value, str) else None

    @property
    def release(self) -> str | None:
        for source, key in (
            (self.attributes, "gen_ai.agent.version"),
            (self.resource, "service.version"),
        ):
            value = source.get(key)
            if isinstance(value, str) and value:
                return value
        return None


@dataclass
class Report:
    spans: list[Span] = field(default_factory=list)
    #: Things worth knowing about the file itself, e.g. a cut-off last line.
    notes: list[str] = field(default_factory=list)


def _attr_value(value: dict[str, object]) -> object:
    """Unwrap one OTLP AnyValue."""
    if not isinstance(value, dict):
        return value
    for key in ("stringValue", "boolValue", "doubleValue"):
        if key in value:
            return value[key]
    if "intValue" in value:
        raw = value["intValue"]
        return int(raw) if isinstance(raw, (str, int)) else raw
    if "arrayValue" in value:
        inner = value["arrayValue"]
        values = inner.get("values", []) if isinstance(inner, dict) else []
        return [_attr_value(v) for v in values]
    if "kvlistValue" in value:
        inner = value["kvlistValue"]
        return _attributes(inner.get("values", []) if isinstance(inner, dict) else [])
    return value.get("bytesValue")


def _attributes(items: object) -> dict[str, object]:
    if not isinstance(items, list):
        return {}
    out: dict[str, object] = {}
    for item in items:
        if isinstance(item, dict) and isinstance(item.get("key"), str):
            out[item["key"]] = _attr_value(item.get("value", {}))
    return out


def read_spans(path: Path, report: Report) -> None:
    lines = path.read_text(errors="replace").splitlines()
    if not lines:
        report.notes.append(f"{path} is empty, so no span was written")
        return
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            if number == len(lines):
                report.notes.append(
                    f"{path} line {number} is cut off part way through. The last batch "
                    "may be missing: the process can have exited before flush() "
                    "finished, or the disk filled."
                )
            else:
                report.notes.append(f"{path} line {number} is not valid JSON")
            continue
        for resource_span in payload.get("resourceSpans", []) or []:
            resource = _attributes((resource_span.get("resource") or {}).get("attributes"))
            for scope_span in resource_span.get("scopeSpans", []) or []:
                scope = ((scope_span.get("scope") or {}).get("name")) or "unknown"
                for raw in scope_span.get("spans", []) or []:
                    attributes = _attributes(raw.get("attributes"))
                    operation = attributes.get("gen_ai.operation.name")
                    report.spans.append(
                        Span(
                            trace_id=raw.get("traceId", ""),
                            span_id=raw.get("spanId", ""),
                            parent_id=raw.get("parentSpanId", "") or "",
                            name=raw.get("name", ""),
                            operation=operation if isinstance(operation, str) else "",
                            attributes=attributes,
                            resource=resource,
                            scope=scope,
                            start_ns=int(raw.get("startTimeUnixNano") or 0),
                            end_ns=int(raw.get("endTimeUnixNano") or 0),
                        )
                    )


def summarize(spans: list[Span]) -> list[str]:
    """What was recorded, as counts. Nothing here says what should have been."""
    by_operation = Counter(span.operation or "(unset)" for span in spans)
    models = [s for s in spans if s.operation == MODEL_CALL]
    tools = [s for s in spans if s.operation == TOOL_CALL]
    named = sorted({s.agent_name for s in spans if s.operation == AGENT_RUN and s.agent_name})
    releases = sorted({s.release for s in spans if s.release})
    with_tokens = sum(1 for s in models if "gen_ai.usage.input_tokens" in s.attributes)
    scopes = Counter(span.scope for span in spans)

    return [
        f"{len(spans)} spans across {len({s.trace_id for s in spans})} traces",
        "operations: " + ", ".join(f"{op}={n}" for op, n in sorted(by_operation.items())),
        "scopes: " + ", ".join(f"{s}={n}" for s, n in sorted(scopes.items())),
        "agents: " + (", ".join(named) if named else "none"),
        "release: " + (", ".join(releases) if releases else "none"),
        f"model calls: {len(models)}, {with_tokens} with token usage",
        f"tool calls: {len(tools)}",
    ]


def token_notes(spans: list[Span]) -> list[str]:
    """Why model calls carry no token counts, split by who wrote the span.

    The fix differs by author and the wrong one costs hours. A span the SDK started
    sits where the app put it, so it can be moved to where the response is. A span
    an instrumentation package started sits where that package put it, and the
    counts are absent because the package did not read them.
    """
    models = [s for s in spans if s.operation == MODEL_CALL]
    absent = [s for s in models if "gen_ai.usage.input_tokens" not in s.attributes]
    if not absent:
        return []

    notes = []
    ours = [s for s in absent if s.scope == SDK_SCOPE]
    if ours:
        notes.append(
            f"{len(ours)} of {len(models)} model calls have no token usage, and the "
            f"{SDK_SCOPE} tracer wrote them, so they are spans in your own code. The counts "
            "are on the provider's response, so the span has to be open where the response "
            "is, inside the function making the request rather than around the call to it."
        )
    others = Counter(s.scope for s in absent if s.scope != SDK_SCOPE)
    for scope, count in sorted(others.items()):
        notes.append(
            f"{count} of {len(models)} model calls have no token usage, and {scope} wrote "
            f"them, so {scope} recorded the call without usage on it. Where your own spans "
            "go does not change that. Read what that library records, and what it needs set "
            "before it does, on the Integrations page at references/integrations.md. A "
            "library that reports no usage on a path, such as a streaming response, leaves a "
            "gap that closes no other way: say so and leave it."
        )
    return notes


#: How much of the longer span two copies of one call have to share before this
#: counts them as the same interval.
INTERVAL_OVERLAP = 0.8


def _same_interval(one: Span, other: Span) -> bool:
    """Whether two spans cover enough of the same stretch of time to be one call."""
    longest = max(one.duration_ms, other.duration_ms)
    if not longest:
        return True
    overlap_ns = min(one.end_ns, other.end_ns) - max(one.start_ns, other.start_ns)
    return overlap_ns / 1_000_000 >= INTERVAL_OVERLAP * longest


def _same_subject(one: Span, other: Span) -> bool:
    """Whether two spans of one operation name the same model, or the same tool."""
    key = "gen_ai.request.model" if one.operation == MODEL_CALL else "gen_ai.tool.name"
    first, second = one.attributes.get(key), other.attributes.get(key)
    return not (first and second and first != second)


def duplicate_calls(spans: list[Span]) -> list[str]:
    """Pairs of spans that record one call twice.

    Two shapes count. Nested: a model or tool span whose direct parent is a span of
    the same operation from a different tracer, over the same interval, which is what
    two wrappers on one call look like. Side by side: two spans of the same operation
    under one parent, or both roots, from different tracers, over the same interval
    and naming the same model or tool, which is what a library emitting each span
    twice looks like. Both copies carry the counts, so anything computed from them is
    doubled, and nothing errors.

    Different tracers is the part that keeps parallel work out of this. An app that
    fires several model calls at once produces side by side spans over one interval
    too, and every one of them comes from the tracer the app installed, so a shared
    scope is read as concurrency rather than as double recording.

    Two limits are worth knowing. The pair has to share 80 percent of the longer
    span, so a wrapper that adds more than a quarter of overhead around the call it
    duplicates, such as a retry loop, reads as two separate calls and is missed. And
    a copy that lands in a different trace, or under a different parent, is not one
    of the shapes above.
    """
    by_id = {span.span_id: span for span in spans}
    calls = [s for s in spans if s.operation in (MODEL_CALL, TOOL_CALL)]
    out = []

    for span in calls:
        parent = by_id.get(span.parent_id)
        if parent is None or parent.operation != span.operation or parent.scope == span.scope:
            continue
        if not _same_interval(span, parent) or not _same_subject(span, parent):
            continue
        out.append(
            f"{parent.name!r} ({parent.scope}) and {span.name!r} ({span.scope}) record the "
            f"same {span.operation} call twice: nested, one interval, two tracers. Every "
            "count from this call is doubled. Remove one wrapper, keeping the "
            "instrumentation package's where there is one."
        )

    siblings: dict[tuple[str, str, str], list[Span]] = defaultdict(list)
    for span in calls:
        siblings[(span.trace_id, span.parent_id, span.operation)].append(span)
    for group in siblings.values():
        for index, one in enumerate(group):
            for other in group[index + 1 :]:
                if one.scope == other.scope:
                    continue
                if not _same_interval(one, other) or not _same_subject(one, other):
                    continue
                out.append(
                    f"{one.name!r} ({one.scope}) and {other.name!r} ({other.scope}) record the "
                    f"same {one.operation} call twice: side by side, one interval, two tracers. "
                    "Every count from this call is doubled. Two packages are wrapping the same "
                    "call, or one package is emitting each span twice. Leave one of them "
                    "recording it."
                )
    return out


def unlisted_operations(spans: list[Span]) -> list[str]:
    """Spans whose usage or message data sits under an operation name nothing reads."""
    listed = ", ".join(sorted(KNOWN_OPERATIONS))
    out = []
    for span in spans:
        if span.operation in KNOWN_OPERATIONS:
            continue
        carried = [key for key in DATA_KEYS if key in span.attributes]
        if not carried:
            continue
        operation = span.operation or "(unset)"
        out.append(
            f"{span.name!r} carries {', '.join(carried)} under operation {operation!r}. "
            f"The workspace reads that data from {listed} spans, so here it becomes no "
            "token count, no cost, no first-class field. A hand-written model span gets "
            'the right name from operation="model_call".'
        )
    return out


#: One coverage expectation: the fewest spans there should be, or the names there
#: should be. A count catches a run that took a shortcut, e.g. a model that answered
#: in one turn and called no tool at all.
Expectation = int | list[str]


def _shortfall(expected: Expectation, found: list[Span], label: str, key: str) -> list[str]:
    if isinstance(expected, int):
        if len(found) >= expected:
            return []
        plural = "" if expected == 1 else "s"
        return [f"expected at least {expected} {label} span{plural}, found {len(found)}"]
    recorded = {span.attributes.get(key) for span in found}
    return [f"no {label} span carries {key}={name!r}" for name in expected if name not in recorded]


def missing_expected(spans: list[Span], agents: Expectation, tools: Expectation) -> list[str]:
    """What the coverage plan asked for and the run did not record."""
    return _shortfall(
        agents,
        [s for s in spans if s.operation == AGENT_RUN],
        "agent run",
        "gen_ai.agent.name",
    ) + _shortfall(
        tools,
        [s for s in spans if s.operation == TOOL_CALL],
        "tool call",
        "gen_ai.tool.name",
    )


#: Past this many lines the tree stops being something you read at a glance, and a
#: long agent loop can produce hundreds of spans.
TREE_LINE_CAP = 60

#: What each operation is worth showing, in the order a reader wants it.
SUMMARY_KEYS = {
    AGENT_RUN: ("gen_ai.agent.name", "gen_ai.agent.version"),
    MODEL_CALL: ("gen_ai.request.model", "gen_ai.usage.input_tokens", "gen_ai.usage.output_tokens"),
    # The call id reads MISSING when nothing called set_tool_call_id, which is what
    # makes one tool call show as two rows in the workspace.
    TOOL_CALL: ("gen_ai.tool.name", "gen_ai.tool.call.id"),
}

#: The content keys and the label each is shown under. A tool call's content has
#: GenAI keys of its own, so without the last two rows a run instrumented exactly
#: as the skill teaches prints no content on any tool span.
CONTENT_KEYS = {
    "gen_ai.input.messages": "input",
    "gen_ai.output.messages": "output",
    "gen_ai.tool.call.arguments": "arguments",
    "gen_ai.tool.call.result": "result",
}


def _summary(span: Span) -> str:
    """The attributes worth seeing next to a span name, or what is missing.

    An operation with no entry above gets no attribute line. Falling back to the
    model keys reported `model=MISSING` on spans that never carry a model, e.g. a
    `workflow`, which reads as a defect in the run rather than in this output.
    """
    keys = SUMMARY_KEYS.get(span.operation, ())
    parts = []
    for key in keys:
        value = span.attributes.get(key)
        short = key.rsplit(".", 1)[-1]
        parts.append(f"{short}={value}" if value is not None else f"{short}=MISSING")
    if span.duration_ms >= 1:
        parts.append(f"{span.duration_ms:.0f}ms")
    return "  ".join(parts)


def _clip(value: object, limit: int = 160) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=repr)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + f"... [{len(text)} chars]"


def render_tree(spans: list[Span], show_content: bool) -> list[str]:
    """The recorded run as a tree, which is how a wrong parent becomes obvious."""
    known = {s.span_id for s in spans}
    children: dict[str, list[Span]] = defaultdict(list)
    roots: list[Span] = []
    for span in sorted(spans, key=lambda s: s.start_ns):
        if span.parent_id and span.parent_id in known:
            children[span.parent_id].append(span)
        else:
            roots.append(span)

    lines: list[str] = []
    truncated = False

    def walk(span: Span, prefix: str, last: bool, top: bool) -> None:
        nonlocal truncated
        if len(lines) >= TREE_LINE_CAP:
            truncated = True
            return
        joint = "" if top else ("`-- " if last else "|-- ")
        lines.append(f"{prefix}{joint}{span.name}    {_summary(span)}".rstrip())
        if show_content:
            for key, label in CONTENT_KEYS.items():
                if key in span.attributes:
                    pad = prefix + ("    " if last or top else "|   ")
                    lines.append(f"{pad}    {label}: {_clip(span.attributes[key])}")
        kids = children.get(span.span_id, [])
        for index, child in enumerate(kids):
            step = "" if top else ("    " if last else "|   ")
            walk(child, prefix + step, index == len(kids) - 1, top=False)

    for root in roots:
        walk(root, "", last=True, top=True)
    if truncated:
        lines.append(f"... more spans, output capped at {TREE_LINE_CAP} lines")
    return lines


def resolve(target: Path) -> list[Path]:
    if target.is_dir():
        return sorted(target.rglob("spans*.jsonl"))
    return [target]


def _expectation(value: str) -> Expectation:
    """A count when the value is all digits, otherwise a list of names.

    `isdecimal` is the set `int` accepts, so it never parses something as a count and
    then fails to convert it, and a tool named `2` is not a name anyone uses.
    """
    if value.strip().isdecimal():
        return int(value)
    return [name.strip() for name in value.split(",") if name.strip()]


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", type=Path, help="a spans.jsonl file, or a directory holding one")
    parser.add_argument(
        "--show-content",
        action="store_true",
        help="print the prompts and completions the run recorded, which is what would leave "
        "this machine with the file",
    )
    parser.add_argument(
        "--expect-agents",
        default=[],
        type=_expectation,
        help="from the coverage plan: comma separated agent names, or a number for the fewest "
        "agent run spans there should be; exit nonzero on a name no span carries, or a count "
        "the run fell short of",
    )
    parser.add_argument(
        "--expect-tools",
        default=[],
        type=_expectation,
        help="from the coverage plan: comma separated tool names, or a number for the fewest "
        "tool call spans there should be; exit nonzero on a name no span carries, or a count "
        "the run fell short of",
    )
    args = parser.parse_args(argv)

    paths = resolve(args.target)
    report = Report()
    if not paths:
        print(
            f"No spans file under {args.target}. init() wrote nothing, or it wrote somewhere else."
        )
        return 1
    missing = [p for p in paths if not p.is_file()]
    if missing:
        print(f"No such file: {missing[0]}")
        return 1

    for path in paths:
        read_spans(path, report)

    print("read: " + ", ".join(str(p) for p in paths))
    for line in summarize(report.spans) if report.spans else []:
        print("  " + line)

    if report.spans:
        print("\nwhat the run recorded:")
        for line in render_tree(report.spans, args.show_content):
            print("  " + line)
        if not args.show_content:
            print("\n  pass --show-content to see the prompts and completions in the file")

    gaps = token_notes(report.spans)
    if gaps:
        print("\nabout the token counts:")
        for line in gaps:
            print("  - " + line)
    if report.notes:
        print("\nabout the file:")
        for line in report.notes:
            print("  - " + line)

    failed = False
    problems = duplicate_calls(report.spans) + unlisted_operations(report.spans)
    if problems:
        print("\nrecorded, and wrong:")
        for line in problems:
            print("  - " + line)
        failed = True

    absent = missing_expected(report.spans, args.expect_agents, args.expect_tools)
    if absent:
        print("\nexpected, and not recorded:")
        for line in absent:
            print("  - " + line)
        failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
