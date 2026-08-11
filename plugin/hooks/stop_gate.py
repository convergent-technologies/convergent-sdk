#!/usr/bin/env python3
"""Stop hook: hold the session open until the recording passes its checks.

Reads the Stop event from stdin. When the loop is armed, it runs the checker
over the spans file the state names and blocks the stop while checks fail or
the verifier's verdict is not attested. The verdict comes from the file the
SubagentStop hook writes when the verifier finishes, never from the ledger, so
the agent being gated cannot write its own release. Every release path is
bounded and loud: five passes, a no-progress hash, and fail-open when the
checker cannot run.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gate_lib

VERIFIER_AGENT = "convergent-instrument:verifier"


def _disarm(project_dir: Path, state: dict) -> None:
    state["armed"] = False
    gate_lib.save_state(project_dir, state)


def _note_in_ledger(project_dir: Path, line: str) -> None:
    ledger = gate_lib.ledger_path(project_dir)
    try:
        ledger.parent.mkdir(parents=True, exist_ok=True)
        with ledger.open("a", encoding="utf-8") as handle:
            handle.write(f"\n{line}\n")
    except OSError:
        pass


def _release_unverified(project_dir: Path, state: dict, why: str, findings: str) -> dict:
    _disarm(project_dir, state)
    open_findings = findings or "(none captured)"
    attested = gate_lib.verdict_path(project_dir).exists()
    unattested = (
        ""
        if attested
        else (
            " No verifier attestation was recorded, so any verification of this recording "
            "was self-run rather than judged by an independent verifier."
        )
    )
    _note_in_ledger(
        project_dir,
        f"verification: released unverified ({why}); "
        + ("verifier attested, findings still open" if attested else "no verifier attestation"),
    )
    message = (
        f"convergent-instrument: {why} — NOT verified.{unattested} Open findings:\n"
        f"{open_findings}\n\nSay in your final report that this recording was released "
        "without a clean verifier attestation, and what is still open."
    )
    return {"systemMessage": message}


#: Repeats of one outcome before the gate gives up on the loop. Two of them mean
#: two blocked passes went by with the same findings over the same spans file,
#: which is a loop that is not moving rather than a stop that came too early.
NO_PROGRESS_REPEATS = 2


def _run_the_agent_instruction(spans: Path | None) -> str:
    return (
        f"Do one thing next: run the instrumented app with CONVERGENT_SPANS_DIR set so it "
        f"writes {spans}. Nothing has recorded spans there yet, so there is nothing to "
        "verify. Where the run fails, fix the run first: no verifier can judge an "
        "absent recording."
    )


def _instruction(project_dir: Path, state: dict) -> str:
    behaviors = gate_lib.resolve_content_dir() / "behaviors"
    ledger = gate_lib.ledger_path(project_dir)
    resume = gate_lib.last_verifier_agent(project_dir, state)
    if resume:
        lead = (
            f"Do one thing next: resume the {VERIFIER_AGENT} subagent this pass already ran, "
            f"by sending a message to agent id {resume} rather than dispatching a new one, so "
            "it keeps the criteria and this recording's history instead of reading them again. "
            "Where resuming is refused or unavailable, dispatch a fresh one instead. Give it "
        )
    else:
        lead = (
            f"Do one thing next: dispatch the {VERIFIER_AGENT} subagent, in the foreground "
            "so you have its report before you finish, with "
        )
    return (
        lead + "these findings, the ledger at "
        f"{ledger}, and the behavior criteria under {behaviors}; on its verdicts, fix "
        "every finding it classifies fix, put the ones it classifies ask to the user, "
        "and append its findings block to the ledger. The verdict that releases this "
        "gate is written by the plugin when the verifier finishes, from the "
        f"{gate_lib.FINDINGS_GLOB} file the verifier leaves in "
        f"{gate_lib.STATE_DIR}/; a verdict line you write yourself does not release it."
    )


def decide(hook_input: dict, run=gate_lib.run_checker) -> dict | None:
    """One Stop event in, one hook response out. None means allow, silently."""
    project_dir = Path(hook_input.get("cwd") or os.getcwd())
    state = gate_lib.load_state(project_dir)
    if state is None:
        return None
    gate_lib.arming_id(project_dir, state)
    passes = int(state.get("passes") or 0)
    if hook_input.get("stop_hook_active") and passes == 0:
        # Re-entered without this gate ever blocking: another hook is looping.
        return None
    if passes >= gate_lib.MAX_PASSES:
        return _release_unverified(
            project_dir,
            state,
            f"loop exhausted after {passes} passes",
            str(state.get("last_findings") or ""),
        )

    try:
        code, output = run(project_dir, state)
    except gate_lib.CheckerUnavailable as error:
        _disarm(project_dir, state)
        return {
            "systemMessage": (
                f"convergent-instrument: verification could not run ({error}); "
                "allowing the stop unverified."
            )
        }

    attestation, missing = gate_lib.read_attestation(project_dir, state)

    # A waiver the user confirmed closes the finding it names now, rather than on
    # the pass after the one that reads the ledger. Waiting for that pass is a
    # full verifier run spent to learn what the user already said.
    waived: list[tuple[dict, dict]] = []
    open_after_waivers = None
    if attestation is not None:
        open_after_waivers = int(attestation["open_count"])
        if open_after_waivers:
            findings = gate_lib.findings_for(project_dir, attestation)
            if findings is not None:
                waived = gate_lib.waived_clauses(findings, gate_lib.load_waivers(project_dir))
                open_after_waivers = max(0, open_after_waivers - len(waived))

    if code == 0 and attestation is not None and open_after_waivers == 0:
        _disarm(project_dir, state)
        if waived:
            named = "; ".join(
                f"{clause.get('criteria', '?')} ({waiver.get('reason') or 'no reason recorded'})"
                for clause, waiver in waived
            )
            _note_in_ledger(
                project_dir, f"verification: released with {len(waived)} waived finding(s): {named}"
            )
            return {
                "systemMessage": (
                    f"convergent-instrument: verified with {len(waived)} finding(s) waived by you "
                    f"— {named}. The checker is clean and every finding the verifier left open is "
                    "one you waived. Say in your final report which findings were waived and why."
                )
            }
        return {
            "systemMessage": (
                "convergent-instrument: verified — the checker is clean and the verifier "
                "attested zero open findings over this recording."
            )
        }

    verdict_summary = (
        missing if attestation is None else f"verifier attested open {open_after_waivers}"
    )
    findings = gate_lib.findings_tail(output)
    key = gate_lib.outcome_hash(
        output, verdict_summary, gate_lib.spans_fingerprint(project_dir, state)
    )
    stalled = int(state.get("stalled") or 0) + 1 if key == state.get("last_hash") else 0
    if stalled >= NO_PROGRESS_REPEATS:
        return _release_unverified(project_dir, state, "no progress across passes", findings)

    state["passes"] = passes + 1
    state["stalled"] = stalled
    state["last_hash"] = key
    state["last_findings"] = findings
    gate_lib.save_state(project_dir, state)

    spans = gate_lib.spans_target(project_dir, state)
    if spans is not None and not spans.exists():
        summary = f"nothing has been recorded at {spans} yet"
        instruction = _run_the_agent_instruction(spans)
    else:
        instruction = _instruction(project_dir, state)
        if code != 0:
            summary = (
                f"the recording did not pass its checks "
                f"(pass {passes + 1} of {gate_lib.MAX_PASSES})"
            )
        elif attestation is None:
            summary = f"the checks pass, but {missing}"
            never_ran = not gate_lib.verdict_path(project_dir).exists()
            if never_ran and gate_lib.ledger_claims_a_verdict(project_dir):
                summary += (
                    ". The ledger already carries a `verifier:` line, and that line is a "
                    "record of the pass, not what releases this gate: no verifier subagent "
                    "has finished over this recording"
                )
        else:
            summary = (
                f"the checks pass, but the verifier attested {open_after_waivers} open findings"
            )
            if waived:
                summary += f", with {len(waived)} more you already waived"
            summary += (
                ". A finding only the user can settle is one to put to them; where they say "
                "to leave it, `/convergent-instrument:waive` closes it without another "
                "verifier pass"
            )
    reason = f"convergent-instrument: {summary}.\n\n{findings}\n\n{instruction}"
    return {"decision": "block", "reason": reason}


def main() -> int:
    try:
        hook_input = json.load(sys.stdin)
    except ValueError:
        return 0
    try:
        response = decide(hook_input)
    except Exception as error:  # noqa: BLE001 - a broken gate must never trap the user
        response = {
            "systemMessage": (
                f"convergent-instrument: the stop gate failed ({error}); "
                "allowing the stop unverified."
            )
        }
    if response is not None:
        print(json.dumps(response))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
