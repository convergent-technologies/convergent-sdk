#!/usr/bin/env python3
"""Assert the run recorded the trace this example claims to produce.

    uv run python/examples/parallel-workers/verify.py [spans-dir]

Uses the portable renderer's parser, then checks the exact shape: nineteen spans
in one trace, every agent run under the
dispatcher's workflow span, and every model call carrying its model and both token
counts. Which worker picked up which invoice is not fixed, so nothing here counts
spans per worker. Exits non-zero when anything is off.
"""

from __future__ import annotations

import importlib.util
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ITEMS = 6
EXPECTED_OPERATIONS = {
    "invoke_workflow": 1,
    "invoke_agent": ITEMS,
    "chat": ITEMS,
    "execute_tool": ITEMS,
}
MODEL_ATTRIBUTES = (
    "gen_ai.request.model",
    "gen_ai.usage.input_tokens",
    "gen_ai.usage.output_tokens",
)

HERE = Path(__file__).resolve().parent
_SHOW_SPANS_SUFFIX = Path("skills") / "convergent-verify" / "scripts" / "show_spans.py"
# The example ships in two repositories, which nest it at different depths, so the
# skill directory is found by walking up rather than by a fixed number of parents.
SHOW_SPANS = next(
    (parent / _SHOW_SPANS_SUFFIX for parent in HERE.parents if (parent / "skills").is_dir()),
    HERE / _SHOW_SPANS_SUFFIX,
)


def load_show_spans() -> Any:
    spec = importlib.util.spec_from_file_location("show_spans", SHOW_SPANS)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot load {SHOW_SPANS}")
    module = importlib.util.module_from_spec(spec)
    # @dataclass resolves annotations through sys.modules, so registering the module
    # has to happen before its body runs.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def read(show_spans: Any, spans_dir: Path) -> list[Any]:
    _, spans = show_spans.read_recording(spans_dir)
    return spans


def problems(spans: list[Any]) -> list[str]:
    found = Counter(span.operation for span in spans)
    expected_total = sum(EXPECTED_OPERATIONS.values())
    out = []

    if len(spans) != expected_total:
        out.append(f"expected {expected_total} spans and found {len(spans)}")
    for operation, count in EXPECTED_OPERATIONS.items():
        if found[operation] != count:
            out.append(f"expected {count} {operation} spans and found {found[operation]}")

    traces = {span.trace_id for span in spans}
    if len(traces) != 1:
        out.append(f"expected one trace and found {len(traces)}")

    workflows = [span for span in spans if span.operation == "invoke_workflow"]
    runs = [span for span in spans if span.operation == "invoke_agent"]
    run_ids = {span.span_id for span in runs}
    if len(workflows) == 1:
        orphans = [span.name for span in runs if span.parent_id != workflows[0].span_id]
        if orphans:
            out.append("agent runs not under the workflow span: " + ", ".join(orphans))
    inside = [span for span in spans if span.operation in {"chat", "execute_tool"}]
    loose = [span.name for span in inside if span.parent_id not in run_ids]
    if loose:
        out.append("model or tool spans not under an agent run: " + ", ".join(loose))

    for span in spans:
        if span.operation != "chat":
            continue
        missing = [key for key in MODEL_ATTRIBUTES if key not in span.attributes]
        if missing:
            out.append(f"{span.name} is missing " + ", ".join(missing))

    return out


def main(argv: list[str]) -> int:
    spans_dir = Path(argv[0]) if argv else HERE / "spans"
    show_spans = load_show_spans()
    spans = read(show_spans, spans_dir)
    print("recording:")
    for line in show_spans.counts(spans):
        print("  " + line)

    found = problems(spans)
    if found:
        print("\nwrong shape:")
        for line in found:
            print("  - " + line)
        return 1
    print(f"\nverify: {sum(EXPECTED_OPERATIONS.values())} spans in one trace, as expected")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
