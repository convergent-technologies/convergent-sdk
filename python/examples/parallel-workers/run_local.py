#!/usr/bin/env python3
"""Run the whole example: a stub model, three workers, and one dispatcher.

    uv run python/examples/parallel-workers/run_local.py
    uv run python/examples/parallel-workers/verify.py

Workers are started with the spawn method, so each child begins with an empty
process and has to call init() for itself, which is what an ECS task does too.
"""

from __future__ import annotations

import multiprocessing
import os
import signal
import subprocess
import threading
from pathlib import Path

import dispatcher
import stub_model
import worker

WORKERS = 3
DRAIN_SECONDS = 30
SPANS_DIR = Path(__file__).resolve().parent / "spans"


def release() -> str:
    """The version every trace is tied to."""
    found = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    return found.stdout.strip() or "0.0.0"


def prepare(spans_dir: Path) -> None:
    """Clear the spans from the last run so a count means one run."""
    spans_dir.mkdir(parents=True, exist_ok=True)
    for stale in spans_dir.glob("spans*.jsonl"):
        stale.unlink()


def main() -> int:
    spans_dir = Path(os.environ.get("PARALLEL_WORKERS_SPANS_DIR") or SPANS_DIR)
    prepare(spans_dir)

    server = stub_model.serve()
    threading.Thread(target=server.serve_forever, daemon=True).start()

    # Set before spawning, because a spawned child inherits the environment and
    # nothing else.
    os.environ.pop("CONVERGENT_API_KEY", None)
    os.environ.pop("CONVERGENT_ENDPOINT", None)
    os.environ["STUB_MODEL_URL"] = stub_model.base_url(server)
    os.environ.setdefault("CONVERGENT_RELEASE", release())
    os.environ.setdefault("OTEL_SERVICE_NAME", "invoice-workers")

    spawn = multiprocessing.get_context("spawn")
    queue = spawn.JoinableQueue()
    workers = [
        spawn.Process(
            target=worker.main,
            args=(queue, index, str(spans_dir)),
            name=f"invoice-worker-{index}",
        )
        for index in range(1, WORKERS + 1)
    ]
    for process in workers:
        process.start()

    producer = spawn.Process(target=dispatcher.main, args=(queue, str(spans_dir)))
    producer.start()
    producer.join()

    drained = threading.Thread(target=queue.join, daemon=True)
    drained.start()
    drained.join(DRAIN_SECONDS)

    for process in workers:
        if process.pid is not None:
            os.kill(process.pid, signal.SIGTERM)
    for process in workers:
        process.join(DRAIN_SECONDS)

    server.shutdown()
    server.server_close()

    if drained.is_alive():
        print(f"the queue did not drain within {DRAIN_SECONDS}s", flush=True)
        return 1
    stuck = [process.name for process in workers if process.is_alive()]
    if stuck:
        for process in workers:
            process.kill()
        print("workers did not exit on SIGTERM: " + ", ".join(stuck), flush=True)
        return 1
    failed = [
        f"{process.name} exited {process.exitcode}" for process in workers if process.exitcode
    ]
    if producer.exitcode:
        failed.append(f"dispatcher exited {producer.exitcode}")
    if failed:
        print("; ".join(failed), flush=True)
        return 1

    print(f"spans written to {spans_dir}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
