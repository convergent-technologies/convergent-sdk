import { Agent } from "@mastra/core/agent";
import { Mastra } from "@mastra/core/mastra";
import { MastraLanguageModelV2Mock } from "@mastra/core/test-utils/llm-mock";
import { createTool } from "@mastra/core/tools";
import { z } from "zod";

export async function runOrderSupport(observability) {
  const model = new MastraLanguageModelV2Mock({
    provider: "scripted",
    modelId: "scripted-model",
    doGenerate: async ({ prompt }) => {
      const hasOrder = prompt.some((message) => message.role === "tool");
      return {
        content: hasOrder
          ? [{ type: "text", text: "Order 1042 has shipped and will arrive on 2026-10-09." }]
          : [
              {
                type: "tool-call",
                toolCallId: "order-lookup",
                toolName: "lookup_order",
                input: JSON.stringify({ orderId: "1042" }),
              },
            ],
        finishReason: hasOrder ? "stop" : "tool-calls",
        usage: { inputTokens: 24, outputTokens: 17, totalTokens: 41 },
        warnings: [],
      };
    },
  });

  const lookupOrder = createTool({
    id: "lookup_order",
    description: "Look up an order's delivery status.",
    inputSchema: z.object({ orderId: z.string() }),
    outputSchema: z.object({
      orderId: z.string(),
      status: z.string(),
      deliveryDate: z.string(),
    }),
    execute: async ({ orderId }) => ({
      orderId,
      status: "shipped",
      deliveryDate: "2026-10-09",
    }),
  });

  const orderSupport = new Agent({
    id: "order_support",
    name: "order_support",
    instructions: "Look up the order and report its delivery status.",
    model,
    tools: { lookup_order: lookupOrder },
  });
  const mastra = new Mastra({ agents: { orderSupport }, observability });
  const result = await mastra.getAgent("orderSupport").generate("Where is order 1042?");
  console.log(JSON.stringify({ traceId: result.traceId, answer: result.text }, null, 2));
}
