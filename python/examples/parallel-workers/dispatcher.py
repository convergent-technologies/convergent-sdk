"""Puts six invoices on the queue, all under one workflow span.

Each message carries the trace context beside its body, the way an SQS message
attribute carries metadata alongside the message. That field is what makes every
worker's run part of this one trace.
"""

from __future__ import annotations

from multiprocessing.queues import JoinableQueue
from typing import Any

from opentelemetry.propagate import inject

import convergent

INVOICE_IDS = (
    "inv_88213",
    "inv_88214",
    "inv_88215",
    "inv_88216",
    "inv_88217",
    "inv_88218",
)


def main(queue: JoinableQueue[dict[str, Any]], spans_dir: str) -> None:
    convergent.init(destinations=[convergent.File(spans_dir, filename="spans-dispatcher.jsonl")])

    # This span ends as soon as the last message is queued, while the workers are
    # still running. That is normal for a queue, and OpenTelemetry allows a child
    # span to start and end after its parent has finished.
    with convergent.span(name="dispatch-invoices", operation="workflow"):
        for invoice_id in INVOICE_IDS:
            carrier: dict[str, str] = {}
            inject(carrier)
            queue.put({"body": {"invoice_id": invoice_id}, "message_attributes": carrier})

    convergent.flush()
