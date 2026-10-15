import { flush, init, span } from "convergent-sdk";

const status = init();
if (!status.enabled) {
  throw new Error(`Tracing could not start: ${status.reason}`);
}

async function lookupOrder(orderId) {
  return span({ name: "lookup_order", operation: "tool_call" }, (call) => {
    call.setInput({ orderId });
    const order = { orderId, status: "shipped", deliveryDate: "2026-10-09" };
    call.setOutput(order);
    return order;
  });
}

async function scriptedModel(order) {
  return {
    text: `Order ${order.orderId} has shipped and will arrive on ${order.deliveryDate}.`,
    usage: { inputTokens: 24, outputTokens: 17 },
  };
}

const answer = await span(
  { name: "order_support", operation: "agent_run", model: "scripted-model" },
  async (run) => {
    const question = "Where is order 1042?";
    run.setInput(question);
    const order = await lookupOrder("1042");
    const response = await span(
      { name: "scripted-model", operation: "model_call" },
      async (call) => {
        call.setInput({ question, order });
        const completion = await scriptedModel(order);
        call.setOutput(completion.text);
        call.setAttribute("gen_ai.request.model", "scripted-model");
        call.setAttribute("gen_ai.usage.input_tokens", completion.usage.inputTokens);
        call.setAttribute("gen_ai.usage.output_tokens", completion.usage.outputTokens);
        return completion.text;
      },
    );
    run.setOutput(response);
    return { traceId: run.traceId, answer: response };
  },
);

console.log(JSON.stringify(answer, null, 2));
const drained = await flush();
if (!drained.ok) {
  throw new Error("Telemetry did not finish flushing.");
}
