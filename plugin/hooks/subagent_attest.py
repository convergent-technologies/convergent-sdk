#!/usr/bin/env python3
"""SubagentStop hook: attest that the verifier ran, over which recording.

Claude Code delivers this event when a subagent finishes, and its input names
the agent that stopped. When that agent is this plugin's verifier and the loop
is armed, the hook reads the findings file the verifier wrote and records the
verdict in `.convergent-instrument/verdict.json`, bound to the current arming
and to a digest of the recording as it stands at this moment.

Only this hook writes that file. The agent that dispatched the verifier writes
the ledger, and the ledger no longer releases the stop gate.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gate_lib

VERIFIER_AGENT = "convergent-instrument:verifier"


def attest(hook_input: dict) -> str | None:
    """Write the attestation for a finished verifier, or say why there is none.

    Returns a sentence for the transcript when the verifier stopped, or None
    when this event is not ours to act on.
    """
    if hook_input.get("agent_type") != VERIFIER_AGENT:
        return None
    project_dir = Path(hook_input.get("cwd") or os.getcwd())
    state = gate_lib.load_state(project_dir)
    if state is None:
        return None

    found = gate_lib.latest_findings(project_dir)
    if found is None:
        return (
            "convergent-instrument: the verifier stopped without leaving a readable "
            f"findings file in {gate_lib.STATE_DIR}/, so its verdict is not attested "
            "and the stop gate still holds."
        )
    findings_file, findings = found

    target = gate_lib.verdict_path(project_dir)
    if target.exists() and findings_file.stat().st_mtime <= target.stat().st_mtime:
        return (
            f"convergent-instrument: {findings_file.name} predates the last attestation, "
            "so this verifier pass left no new findings to attest."
        )

    spans = gate_lib.spans_digest(project_dir, state)
    if spans is None:
        return (
            "convergent-instrument: the recording named by state.json is missing, so the "
            "verifier's verdict cannot be bound to what it judged."
        )

    count = gate_lib.open_count(findings)
    cycle = gate_lib.arming_id(project_dir, state)
    verdict = {
        "arming_id": cycle,
        "verdict": "clean" if count == 0 else "open",
        "open_count": count,
        "spans_digest": spans,
        "findings_digest": gate_lib.digest(findings_file.read_bytes()),
        "findings_file": findings_file.name,
        "agent_type": VERIFIER_AGENT,
        "agent_id": hook_input.get("agent_id"),
        "written_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": "subagent-stop",
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(verdict, indent=2) + "\n", encoding="utf-8")
    state["verifier_ran_at"] = verdict["written_at"]
    state["verifier_cycle"] = cycle
    gate_lib.save_state(project_dir, state)
    return (
        f"convergent-instrument: attested the verifier's verdict from {findings_file.name} "
        f"as {verdict['verdict']} with {count} open findings."
    )


def main() -> int:
    try:
        hook_input = json.load(sys.stdin)
    except ValueError:
        return 0
    try:
        note = attest(hook_input)
    except Exception as error:  # noqa: BLE001 - a broken hook must never trap the subagent
        note = f"convergent-instrument: the verdict attestation failed ({error})."
    if note is not None:
        print(json.dumps({"systemMessage": note}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
