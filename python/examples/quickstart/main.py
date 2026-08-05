"""The README quickstart, runnable with no keys and no network.

The traced function is the one the README and the Get started page show. What is
different here is the client above it: `scripted()` answers the request in the
OpenAI wire format, so the example makes a real client call without a provider
key. Against a real provider the client is `OpenAI()` and nothing else changes.

Run with:

    GIT_SHA=$(git rev-parse --short HEAD) uv run python/examples/quickstart/main.py
"""

import json
import os

import httpx
from openai import OpenAI

import convergent

#: The stub answers every request, so nothing here is ever sent to a provider.
PLACEHOLDER = "not-a-real-key"


def scripted(request: httpx.Request) -> httpx.Response:
    """One fixed completion, so no request leaves the process."""
    question = json.loads(request.content)["messages"][0]["content"]
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-quickstart",
            "object": "chat.completion",
            "created": 1785367000,
            "model": "gpt-5.5",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": f"Thanks for asking about {question}",
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 42, "completion_tokens": 11, "total_tokens": 53},
        },
    )


convergent.init(release=os.environ["GIT_SHA"])

client = OpenAI(
    api_key=PLACEHOLDER,
    http_client=httpx.Client(transport=httpx.MockTransport(scripted)),
)
MODEL = "gpt-5.5"


@convergent.agent(name="support-agent")
def answer(question: str) -> str:
    with convergent.span(name=MODEL, operation="model_call") as call:
        call.set_input({"question": question})
        completion = client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "user", "content": question}],
        )
        reply = completion.choices[0].message.content
        call.set_output({"answer": reply})
        call.set_attribute("gen_ai.request.model", MODEL)
        call.set_attribute("gen_ai.usage.input_tokens", completion.usage.prompt_tokens)
        call.set_attribute("gen_ai.usage.output_tokens", completion.usage.completion_tokens)
    return reply


print(answer("Where is my invoice?"))
convergent.flush()
