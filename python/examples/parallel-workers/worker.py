"""One worker process. Pulls invoices off the queue until it is told to stop.

Spawned children start with none of the parent's tracing, so this calls init()
itself, and it writes to a spans file of its own so two workers never append to the
same file. The release reaches init() as CONVERGENT_RELEASE, which run_local.py sets
before it spawns anything.
"""

from __future__ import annotations

import json
import os
import signal
import threading
import urllib.request
from multiprocessing.queues import JoinableQueue
from queue import Empty
from typing import Any

from opentelemetry import context as otel_context
from opentelemetry.propagate import extract

import convergent

MODEL = "gpt-4o-mini"
POLL_SECONDS = 0.1

SYSTEM_PROMPT = (
    "You are a billing agent. Look up the invoice you are asked about and report "
    "its status in one sentence."
)

TOOL_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "lookup_invoice",
            "description": "Fetch one invoice by id.",
            "parameters": {
                "type": "object",
                "properties": {"invoice_id": {"type": "string"}},
                "required": ["invoice_id"],
            },
        },
    }
]

INVOICES = {
    "inv_88213": {"amount_cents": 4200, "status": "open"},
    "inv_88214": {"amount_cents": 15900, "status": "paid"},
    "inv_88215": {"amount_cents": 800, "status": "open"},
    "inv_88216": {"amount_cents": 32100, "status": "overdue"},
    "inv_88217": {"amount_cents": 6750, "status": "paid"},
    "inv_88218": {"amount_cents": 1100, "status": "open"},
}

_stop = threading.Event()


def _request_stop(signum: int, frame: object) -> None:
    _stop.set()


def main(queue: JoinableQueue[dict[str, Any]], index: int, spans_dir: str) -> None:
    signal.signal(signal.SIGTERM, _request_stop)
    convergent.init(
        destinations=[convergent.File(spans_dir, filename=f"spans-worker-{index}.jsonl")]
    )

    while not _stop.is_set():
        try:
            message = queue.get(timeout=POLL_SECONDS)
        except Empty:
            continue
        try:
            run_item(message)
        finally:
            queue.task_done()
        # Outside handle(), so the agent_run span has ended and is in the batch this
        # drains. Called inside it, the flush would miss the run it is meant to send.
        convergent.flush()


def run_item(message: dict[str, Any]) -> None:
    """Run one queue message as a child of the trace that produced it."""
    # observe() records against whatever OpenTelemetry context is active and takes
    # no parent argument, so the extracted context has to be attached around it.
    token = otel_context.attach(extract(message["message_attributes"]))
    try:
        print(handle(message["body"]), flush=True)
    finally:
        otel_context.detach(token)


@convergent.observe(name="invoice-worker", operation="agent_run")
def handle(body: dict[str, Any]) -> str:
    reply = call_model(body["invoice_id"])
    requested = reply["tool_calls"][0]
    invoice = lookup_invoice(requested["id"], **json.loads(requested["function"]["arguments"]))
    return f"{body['invoice_id']} is {invoice.get('status', 'unknown')}"


def call_model(invoice_id: str) -> dict[str, Any]:
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"What is the status of invoice {invoice_id}?"},
    ]
    # The token counts are on the response, so the span opens here rather than
    # around the call to this function.
    with convergent.span(name=MODEL, operation="model_call") as call:
        call.set_attribute("gen_ai.request.model", MODEL)
        call.set_input(messages)
        response = _post(
            f"{_base_url()}/chat/completions",
            {"model": MODEL, "messages": messages, "tools": TOOL_SCHEMA},
        )
        usage = response["usage"]
        call.set_attribute("gen_ai.usage.input_tokens", usage["prompt_tokens"])
        call.set_attribute("gen_ai.usage.output_tokens", usage["completion_tokens"])
        reply = response["choices"][0]["message"]
        call.set_output(reply)
        return reply


def lookup_invoice(call_id: str, invoice_id: str) -> dict[str, Any]:
    with convergent.span(name="lookup_invoice", operation="tool_call") as tool:
        # The id the model gave the call. It is what pairs the model asking for
        # the call with the call running, so the two show as one row.
        tool.set_tool_call_id(call_id)
        tool.set_input({"invoice_id": invoice_id})
        record = INVOICES.get(invoice_id, {"status": "not found"})
        tool.set_output(record)
        return record


def _base_url() -> str:
    return os.environ.get("STUB_MODEL_URL", "http://127.0.0.1:8899/v1")


def _post(url: str, payload: dict[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.load(response)
