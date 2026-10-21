import { agent, currentSpan, currentTrace, flush, init, session, tool } from "convergent-sdk";

const status = init();
if (!status.enabled) {
  throw new Error(`Tracing could not start: ${status.reason}`);
}

const lookupOrder = tool({ name: "lookup_order" }, (orderId, callId) => {
  const call = currentSpan();
  call.setToolCallId(callId);
  call.setInput({ orderId });
  const order = { orderId, status: "shipped", deliveryDate: "2026-10-09" };
  call.setOutput(order);
  return order;
});

const answer = agent({ name: "order_support", model: "scripted-model" }, async (question) => {
  const run = currentSpan();
  run.setInput(question);
  const order = await lookupOrder("1042", "call-1042");
  const response = `Order ${order.orderId} has shipped and will arrive on ${order.deliveryDate}.`;
  run.setOutput(response);
  return { ...currentTrace(), answer: response };
});

const sessionId = "order-conversation-1042";
const turns = await session(sessionId, async () => [
  await answer("Where is order 1042?"),
  await answer("When will it arrive?"),
]);

console.log(JSON.stringify({ sessionId, turns }, null, 2));
const drained = await flush();
if (!drained.ok) {
  throw new Error("Telemetry did not finish flushing.");
}
