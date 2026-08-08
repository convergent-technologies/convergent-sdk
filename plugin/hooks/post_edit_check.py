#!/usr/bin/env python3
"""PostToolUse hook on Edit and Write: surface checker failures between stops.

When the loop is armed and a spans file exists, run the checker and report
failures on stderr with exit code 2, so they reach the model right after the
edit instead of at the next stop. Runs at most once per debounce window, and
any problem running the check is silence, never a block.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gate_lib


def check(hook_input: dict, run=gate_lib.run_checker, now=time.time) -> str | None:
    """The concise failure text to feed back, or None to stay silent."""
    project_dir = Path(hook_input.get("cwd") or os.getcwd())
    state = gate_lib.load_state(project_dir)
    if state is None:
        return None
    gate_lib.arming_id(project_dir, state)
    spans_path = gate_lib.spans_target(project_dir, state)
    if spans_path is None or not spans_path.exists():
        # Before the first run there is nothing to check, and spending the
        # debounce window here would skip the first check that could say something.
        return None
    moment = now()
    if moment - float(state.get("last_posttool_check") or 0.0) < gate_lib.POSTTOOL_DEBOUNCE:
        return None
    state["last_posttool_check"] = moment
    gate_lib.save_state(project_dir, state)
    try:
        code, output = run(project_dir, state, timeout=20.0)
    except gate_lib.CheckerUnavailable:
        return None
    if code == 0:
        return None
    return (
        "convergent-instrument: the recorded spans still fail their checks "
        "(the recording may predate this edit; rerun the agent to refresh it):\n"
        + gate_lib.findings_tail(output, limit=15)
    )


def main() -> int:
    try:
        hook_input = json.load(sys.stdin)
    except ValueError:
        return 0
    try:
        failure = check(hook_input)
    except Exception:  # noqa: BLE001 - an early-signal hook must never block on its own bugs
        return 0
    if failure is None:
        return 0
    print(failure, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
