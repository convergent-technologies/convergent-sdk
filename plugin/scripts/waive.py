#!/usr/bin/env python3
"""Record a waiver the user confirmed, so the gate stops holding for it.

The user runs this through `/convergent-instrument:waive`. Without it a waiver
only takes effect on the pass after the one that reads the ledger, which is a
whole verifier run spent learning what the user already said.

A waiver names the criteria file it covers and, where given, a substring that has
to appear in the finding. The substring is what keeps a waiver of one finding
from covering the next finding the same criteria raises. Standard library only,
like every script this plugin runs.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

STATE_DIR = ".convergent-instrument"
WAIVERS_FILE = "waivers.json"


def load(path: Path) -> list[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    entries = data.get("waived") if isinstance(data, dict) else data
    return [e for e in entries if isinstance(e, dict)] if isinstance(entries, list) else []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("criteria", help="the criteria file the finding came from")
    parser.add_argument("reason", nargs="*", help="why this finding is being left as it is")
    parser.add_argument(
        "--match",
        default="",
        help="a substring of the clause or evidence, so this covers one finding and not every "
        "finding that criteria will ever raise",
    )
    parser.add_argument("--list", action="store_true", help="print the waivers already recorded")
    args = parser.parse_args(argv)

    project_dir = Path.cwd()
    path = project_dir / STATE_DIR / WAIVERS_FILE
    waived = load(path)

    if args.list:
        if not waived:
            print("convergent-instrument: no waivers recorded in this project.")
            return 0
        for entry in waived:
            print(f"  {entry.get('criteria')}: {entry.get('reason') or '(no reason)'}")
        return 0

    reason = " ".join(args.reason).strip()
    if not reason:
        print(
            "convergent-instrument: a waiver needs a reason. It is the record of why this "
            "recording ships with the finding open, and the release message quotes it.",
            file=sys.stderr,
        )
        return 2

    waived.append(
        {
            "criteria": args.criteria,
            "clause": args.match,
            "reason": reason,
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "source": "user-command",
        }
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"waived": waived}, indent=2) + "\n", encoding="utf-8")
    scope = f" matching {args.match!r}" if args.match else ""
    print(
        f"convergent-instrument: waived {args.criteria}{scope}. The gate will release on this "
        "finding without another verifier pass, and the release message names the waiver."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
