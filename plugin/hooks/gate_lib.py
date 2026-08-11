#!/usr/bin/env python3
"""State machine shared by the convergent-instrument hooks.

The instrument skill arms the loop by writing `.convergent-instrument/state.json`
at the project root. The Stop hook runs the checker over the spans file that
state names and blocks the stop while the checks fail, the PostToolUse hook
surfaces the same failures between stops, the SubagentStop hook attests the
verifier's verdict, and the cancel command disarms. Standard library only, like
the checker it runs.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
from pathlib import Path

STATE_DIR = ".convergent-instrument"
STATE_FILE = "state.json"
LEDGER_FILE = "plan.md"
VERDICT_FILE = "verdict.json"
WAIVERS_FILE = "waivers.json"
FINDINGS_GLOB = "findings-*.json"

#: Blocks per armed cycle before the gate gives up and releases the stop.
MAX_PASSES = 5

CHECKER_TIMEOUT = 60.0

#: Seconds between PostToolUse checker runs.
POSTTOOL_DEBOUNCE = 30.0

#: The verdicts a findings file may give a single criteria clause.
CLAUSE_VERDICTS = frozenset({"true", "false", "not-applicable"})

#: The ledger line the dispatching agent writes. It is a record for the reader,
#: not a release: the gate reads it only to name it in a block reason.
LEDGER_VERDICT_LINE = re.compile(r"^verifier:\s*(clean|open\s+\d+)\s*$", re.IGNORECASE)

PLUGIN_ROOT = Path(__file__).resolve().parent.parent


class CheckerUnavailable(Exception):
    """The checker could not run at all, which is not a finding."""


def state_path(project_dir: Path) -> Path:
    return project_dir / STATE_DIR / STATE_FILE


def ledger_path(project_dir: Path) -> Path:
    return project_dir / STATE_DIR / LEDGER_FILE


def verdict_path(project_dir: Path) -> Path:
    return project_dir / STATE_DIR / VERDICT_FILE


def load_state(project_dir: Path) -> dict | None:
    """The armed state, or None when the loop is not armed."""
    try:
        state = json.loads(state_path(project_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(state, dict) or state.get("armed") is not True:
        return None
    return state


def save_state(project_dir: Path, state: dict) -> None:
    path = state_path(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def plugin_root() -> Path:
    return Path(os.environ.get("CLAUDE_PLUGIN_ROOT", PLUGIN_ROOT))


def arming_id(project_dir: Path, state: dict) -> str:
    """The id of the current arming, minted and persisted on first sight.

    The skill arms the loop by hand, so the id cannot come from the file it
    writes. The first hook to look mints one and saves it, and every later
    reader of that state gets the same value until the loop is armed again.
    """
    existing = state.get("arming_id")
    if isinstance(existing, str) and existing:
        return existing
    minted = secrets.token_hex(8)
    state["arming_id"] = minted
    save_state(project_dir, state)
    return minted


def resolve_content_dir() -> Path:
    """The freshest copy of the instructions the agent reads.

    The session-start hook keeps a fetched copy under the plugin data
    directory; the build-time snapshot inside the plugin is the fallback.
    Instructions only. Nothing under this directory is ever executed, because
    whoever can move the published ref decides what it holds.
    """
    data_dir = os.environ.get("CLAUDE_PLUGIN_DATA")
    if data_dir:
        cached = Path(data_dir) / "content"
        if (cached / "manifest.json").is_file():
            return cached
    return plugin_root() / "content-snapshot"


def resolve_executable_dir() -> Path:
    """The copy of the content this plugin runs code from, which is its own.

    Always the snapshot shipped inside the versioned plugin, never the fetched
    cache. The checker's behavior therefore changes only when the plugin is
    released, and the content manifest's `requires-plugin` floor is what keeps
    newer instructions from assuming a newer checker.
    """
    return plugin_root() / "content-snapshot"


def _expect_args(state: dict) -> list[str]:
    args: list[str] = []
    for key, flag in (("expect_agents", "--expect-agents"), ("expect_tools", "--expect-tools")):
        value = state.get(key)
        if isinstance(value, int):
            value = str(value)
        if isinstance(value, str) and value.strip():
            args += [flag, value.strip()]
    return args


def spans_target(project_dir: Path, state: dict) -> Path | None:
    """Where the run under judgment writes its spans, or None when unnamed."""
    spans = state.get("spans")
    if not isinstance(spans, str) or not spans.strip():
        return None
    path = Path(spans.strip())
    return path if path.is_absolute() else project_dir / path


def spans_fingerprint(project_dir: Path, state: dict) -> str:
    """What the spans on disk are right now, as size and modification time.

    This is what separates a pass that ran the agent again and failed again
    from a pass that did nothing between two stops.
    """
    target = spans_target(project_dir, state)
    if target is None:
        return "unnamed"
    files = sorted(target.rglob("spans*.jsonl")) if target.is_dir() else [target]
    marks = []
    for path in files:
        try:
            info = path.stat()
        except OSError:
            continue
        marks.append(f"{path}:{info.st_size}:{info.st_mtime_ns}")
    return "|".join(marks) or "absent"


def digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def spans_digest(project_dir: Path, state: dict) -> str | None:
    """A digest of the recording as it stands right now, or None when absent.

    This is what a verdict is bound to. `spans_fingerprint` answers whether the
    recording changed between two stops; this answers whether it is the same
    bytes the verifier judged, which a size and a timestamp cannot.
    """
    path = spans_target(project_dir, state)
    if path is None:
        return None
    if path.is_file():
        try:
            return digest(path.read_bytes())
        except OSError:
            return None
    if not path.is_dir():
        return None
    running = hashlib.sha256()
    try:
        for child in sorted(p for p in path.rglob("*") if p.is_file()):
            running.update(child.relative_to(path).as_posix().encode("utf-8"))
            running.update(b"\0")
            running.update(child.read_bytes())
            running.update(b"\0")
    except OSError:
        return None
    return "sha256:" + running.hexdigest()


def run_checker(
    project_dir: Path, state: dict, timeout: float = CHECKER_TIMEOUT
) -> tuple[int, str]:
    """Run the bundled checker over the spans the state names.

    Returns the exit code and combined output. Raises CheckerUnavailable when
    the checker itself cannot run, which callers treat as fail-open.
    """
    spans_path = spans_target(project_dir, state)
    if spans_path is None:
        raise CheckerUnavailable("state.json names no spans path")
    script = resolve_executable_dir() / "scripts" / "show_spans.py"
    if not script.is_file():
        raise CheckerUnavailable(f"no checker at {script}")
    cmd = [sys.executable, str(script), str(spans_path), *_expect_args(state)]
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=project_dir)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise CheckerUnavailable(str(error)) from error
    return done.returncode, (done.stdout + done.stderr).strip()


def ledger_claims_a_verdict(project_dir: Path) -> bool:
    """Whether the ledger carries a `verifier:` verdict line.

    The gate does not decide on this. It reads the ledger only so it can say,
    when it blocks, that the line it found there is not what releases it.
    """
    try:
        lines = ledger_path(project_dir).read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    return any(LEDGER_VERDICT_LINE.match(line.strip()) for line in lines)


def latest_findings(project_dir: Path) -> tuple[Path, dict] | None:
    """The newest well-formed findings file the verifier left, if any.

    A findings file is well formed when it lists clause verdicts drawn from
    the criteria vocabulary and an open count. Anything else is not a verdict
    this gate will attest, so it reads as no findings at all.
    """
    candidates = sorted(
        (project_dir / STATE_DIR).glob(FINDINGS_GLOB),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for path in candidates:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        clauses = data.get("clauses")
        if not isinstance(clauses, list) or not clauses:
            continue
        if not all(isinstance(c, dict) and c.get("verdict") in CLAUSE_VERDICTS for c in clauses):
            continue
        if not isinstance(data.get("open_count"), int) or data["open_count"] < 0:
            continue
        return path, data
    return None


def open_count(findings: dict) -> int:
    """How many findings are open, counted from the clauses and the report.

    The verifier reports its own count and the clauses carry the evidence for
    one. Where they disagree the larger wins, so a miscount cannot release a
    recording that still has an open finding.
    """
    counted = sum(
        1
        for c in findings["clauses"]
        if c.get("verdict") == "false" and c.get("classification") != "acknowledged"
    )
    return max(counted, findings["open_count"])


def waivers_path(project_dir: Path) -> Path:
    return project_dir / STATE_DIR / WAIVERS_FILE


def load_waivers(project_dir: Path) -> list[dict]:
    """The waivers the user confirmed, each naming the criteria it covers.

    Written by the cancel-adjacent waive command, which the user runs. Nothing
    here is unforgeable: an agent with a shell can write this file, exactly as it
    can write a verdict. What keeps it honest is that every waiver a release
    applied is named in the release message, so a waiver nobody granted is
    visible in the transcript rather than silent.
    """
    try:
        data = json.loads(waivers_path(project_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    entries = data.get("waived") if isinstance(data, dict) else data
    if not isinstance(entries, list):
        return []
    return [
        entry
        for entry in entries
        if isinstance(entry, dict) and isinstance(entry.get("criteria"), str) and entry["criteria"]
    ]


def findings_for(project_dir: Path, verdict: dict) -> dict | None:
    """The findings file an attestation was written from."""
    name = verdict.get("findings_file")
    if not isinstance(name, str) or not name or "/" in name:
        return None
    try:
        data = json.loads((project_dir / STATE_DIR / name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def waived_clauses(findings: dict, waivers: list[dict]) -> list[tuple[dict, dict]]:
    """Open clauses a waiver covers, paired with the waiver that covers each.

    A waiver names a criteria file, and optionally a substring that has to appear
    in the clause or its evidence. The substring is what keeps a waiver of one
    finding from covering the next finding the same criteria raises.
    """
    clauses = findings.get("clauses")
    if not isinstance(clauses, list):
        return []
    out: list[tuple[dict, dict]] = []
    for clause in clauses:
        if not isinstance(clause, dict):
            continue
        if clause.get("verdict") != "false" or clause.get("classification") == "acknowledged":
            continue
        haystack = (
            f"{clause.get('criteria', '')} {clause.get('clause', '')} {clause.get('evidence', '')}"
        )
        for waiver in waivers:
            if waiver["criteria"] != clause.get("criteria"):
                continue
            needle = waiver.get("clause")
            if isinstance(needle, str) and needle and needle not in haystack:
                continue
            out.append((clause, waiver))
            break
    return out


def last_verifier_agent(project_dir: Path, state: dict) -> str | None:
    """The verifier id this arming already ran, so a later pass resumes it.

    A resumed verifier keeps the criteria files, the integration pages, and this
    recording's history in its context, which is most of what a pass spends. The
    id is recorded by the SubagentStop hook and is scoped to the arming, so a new
    arming gets a verifier that comes to the evidence cold.
    """
    try:
        verdict = json.loads(verdict_path(project_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(verdict, dict) or verdict.get("arming_id") != state.get("arming_id"):
        return None
    agent_id = verdict.get("agent_id")
    return agent_id if isinstance(agent_id, str) and agent_id else None


def read_attestation(project_dir: Path, state: dict) -> tuple[dict | None, str]:
    """The verifier attestation for this arming and this recording.

    Returns the attestation and an empty reason when one applies, or None and
    a sentence naming what is missing. An attestation applies only when the
    SubagentStop hook wrote it during the current arming and the recording it
    judged still hashes to what is on disk now.
    """
    try:
        verdict = json.loads(verdict_path(project_dir).read_text(encoding="utf-8"))
    except OSError:
        return None, "no verifier attestation has been recorded for this recording"
    except ValueError:
        return None, "the verifier attestation is not readable JSON"
    if not isinstance(verdict, dict):
        return None, "the verifier attestation is not readable JSON"
    if verdict.get("arming_id") != state.get("arming_id"):
        return None, "the verifier attestation belongs to an earlier arming of the loop"
    current = spans_digest(project_dir, state)
    if current is None or verdict.get("spans_digest") != current:
        return None, "the verifier attestation judged a different recording than the one on disk"
    if verdict.get("verdict") not in {"clean", "open"}:
        return None, "the verifier attestation carries no verdict"
    return verdict, ""


def outcome_hash(checker_output: str, verdict_summary: str, spans_state: str = "") -> str:
    payload = "\n---\n".join((checker_output, verdict_summary, spans_state))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def findings_tail(checker_output: str, limit: int = 40) -> str:
    """The end of the checker output, where the failures are printed."""
    lines = checker_output.splitlines()
    return "\n".join(lines[-limit:])
