#!/usr/bin/env python3
"""Run the skill's checker over a spans file.

This is a thin entry point, not a fork: the checker is the skill's own
`scripts/show_spans.py`, taken from the snapshot inside this plugin. The
fetched content cache is never run from, whatever it holds, so the checker
changes only when the plugin is released. Arguments pass through unchanged, so
the interface is the checker's own:

    python3 run_checker.py <spans-file-or-dir> [--show-content]
        [--expect-agents a,b | --expect-agents 2]
        [--expect-tools search,fetch | --expect-tools 1]
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))
from gate_lib import resolve_executable_dir


def main(argv: list[str]) -> int:
    script = resolve_executable_dir() / "scripts" / "show_spans.py"
    if not script.is_file():
        print(f"no checker at {script}", file=sys.stderr)
        return 1
    return subprocess.run([sys.executable, str(script), *argv]).returncode


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
