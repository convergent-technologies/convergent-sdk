#!/usr/bin/env python3
"""A scripted stand-in for a chat completions endpoint.

Speaks enough of the OpenAI wire format for the workers to talk to it, and answers
from a fixed script rather than a model, so the example runs with no API key and no
network. It listens on loopback and serves each request on its own thread, because
every worker calls it at the same time.

Run it on its own with:

    python stub_model.py
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

#: The token counts each invoice is answered with. They are a function of the
#: invoice rather than of arrival order, because several workers call this server at
#: once and a counter here would make the numbers depend on who arrived first.
SCRIPT: dict[str, tuple[int, int]] = {
    "inv_88213": (180, 24),
    "inv_88214": (192, 31),
    "inv_88215": (174, 19),
    "inv_88216": (205, 28),
    "inv_88217": (168, 22),
    "inv_88218": (199, 26),
}


def _invoice_id(request: dict[str, Any]) -> str | None:
    """The invoice the messages ask about, which is what keys the script."""
    messages = request.get("messages") or []
    text = " ".join(str(message.get("content") or "") for message in messages)
    return next((invoice_id for invoice_id in SCRIPT if invoice_id in text), None)


def _completion(invoice_id: str, model: str) -> dict[str, Any]:
    input_tokens, output_tokens = SCRIPT[invoice_id]
    return {
        "id": f"chatcmpl-{invoice_id}",
        "object": "chat.completion",
        "created": 1785367000,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": f"call_{invoice_id}",
                            "type": "function",
                            "function": {
                                "name": "lookup_invoice",
                                "arguments": json.dumps({"invoice_id": invoice_id}),
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {
            "prompt_tokens": input_tokens,
            "completion_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        },
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass

    def do_POST(self) -> None:
        if self.path.rstrip("/") != "/v1/chat/completions":
            self.send_error(404, "Not Found")
            return

        length = int(self.headers.get("Content-Length") or 0)
        request = json.loads(self.rfile.read(length) or b"{}")
        invoice_id = _invoice_id(request)
        if invoice_id is None:
            self.send_error(400, "No scripted answer for this request")
            return

        body = json.dumps(_completion(invoice_id, request.get("model", "gpt-4o-mini"))).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def serve(host: str = "127.0.0.1", port: int = 0) -> ThreadingHTTPServer:
    """A server bound and ready to answer, which the caller starts and stops.

    Port 0 lets the operating system pick a free port, so repeated runs of the
    example never collide with a server an earlier run left behind.
    """
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server


def base_url(server: ThreadingHTTPServer) -> str:
    host, port = server.server_address[0], server.server_address[1]
    return f"http://{host}:{port}/v1"


def main() -> None:
    server = serve(port=8899)
    print(f"stub_model: listening on {base_url(server)}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()


if __name__ == "__main__":
    main()
