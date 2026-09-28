"""Record two concurrent conversations with two turns each."""

import asyncio

import convergent


@convergent.tool()
def lookup_order(order_id: str) -> str:
    call = convergent.current_span()
    call.set_input({"order_id": order_id})
    status = "shipped"
    call.set_output(status)
    return status


@convergent.agent(name="support-agent")
async def answer(question: str) -> str:
    run = convergent.current_span()
    run.set_input(question)
    tracer = convergent.tracer_provider().get_tracer("session-example")
    with tracer.start_as_current_span("prepare request"):
        await asyncio.sleep(0)
    status = lookup_order("42")
    reply = f"Order 42 is {status}."
    run.set_output(reply)
    return reply


async def conversation(session_id: str) -> None:
    with convergent.session(session_id):
        for question in ("Where is my order?", "Has it shipped?"):
            print(session_id, await answer(question))


async def main() -> None:
    await asyncio.gather(conversation("chat-1"), conversation("chat-2"))
    tracer = convergent.tracer_provider().get_tracer("session-example")
    with tracer.start_as_current_span("after sessions"):
        pass


if __name__ == "__main__":
    convergent.init(
        release="session-example",
        destinations=[convergent.File("./traces")],
        strict=True,
    )
    asyncio.run(main())
    print(convergent.flush())
