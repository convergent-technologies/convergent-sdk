#!/usr/bin/env python3
"""Disarm the enforced verification loop for the current project.

Sets `"armed": false` in `.convergent-instrument/state.json` under the working
directory, which makes every hook pass through. The ledger keeps its record.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

STATE = Path(".convergent-instrument") / "state.json"


def main() -> int:
    if not STATE.is_file():
        print("nothing to disarm: no .convergent-instrument/state.json here")
        return 0
    try:
        state = json.loads(STATE.read_text(encoding="utf-8"))
    except ValueError:
        print("state.json is not valid JSON; leaving it alone", file=sys.stderr)
        return 1
    if state.get("armed") is not True:
        print("already disarmed")
        return 0
    state["armed"] = False
    STATE.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    print("disarmed: the stop gate will no longer hold this project's sessions open")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
