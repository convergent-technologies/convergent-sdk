import type { Attributes, AttributeValue } from "@opentelemetry/api";
import { reportOnce } from "./diagnostics.js";

export const operations = new Map([
  ["agent_run", "invoke_agent"],
  ["model_call", "chat"],
  ["tool_call", "execute_tool"],
  ["retrieval", "retrieval"],
  ["embeddings", "embeddings"],
  ["workflow", "invoke_workflow"],
  ["agent_create", "create_agent"],
  ["text_completion", "text_completion"],
  ["generate_content", "generate_content"],
]);

const reserved = new Set([
  "gen_ai.operation.name",
  "gen_ai.agent.name",
  "gen_ai.agent.version",
  "gen_ai.tool.name",
  "gen_ai.input.messages",
  "gen_ai.output.messages",
  "gen_ai.tool.call.arguments",
  "gen_ai.tool.call.result",
]);

export function acceptedAttribute(key: string, value: unknown): value is AttributeValue {
  if (typeof key !== "string" || key.startsWith("convergent.") || reserved.has(key)) {
    reportOnce("reserved_attribute", "Convergent ignored an attribute key reserved by the SDK.");
    return false;
  }
  const isScalar = (item: unknown): item is string | number | boolean =>
    typeof item === "string" || typeof item === "boolean" || typeof item === "number";
  if (isScalar(value)) return true;
  if (
    Array.isArray(value) &&
    value.every((item) => isScalar(item) && typeof item === typeof value[0])
  ) {
    return true;
  }
  reportOnce(
    "invalid_attribute",
    "Convergent ignored an attribute value: use a string, number, boolean, or an array of one type.",
  );
  return false;
}

export function customAttributes(attributes: Attributes | undefined): Attributes {
  return Object.fromEntries(
    Object.entries(attributes ?? {}).filter(([key, value]) => acceptedAttribute(key, value)),
  );
}

export function json(value: unknown): string {
  try {
    const encoded = JSON.stringify(value);
    if (encoded !== undefined) return encoded;
  } catch {
    reportOnce(
      "content_json",
      "Convergent could not encode content as JSON; using its text value.",
    );
  }
  try {
    return String(value);
  } catch {
    reportOnce("content_text", "Convergent could not convert content to text.");
    return "<unrepresentable>";
  }
}

export function text(value: unknown): string {
  return typeof value === "string" ? value : json(value);
}

export function messages(role: "user" | "assistant", value: unknown): string {
  if (
    Array.isArray(value) &&
    value.length > 0 &&
    value.every((item) => item !== null && typeof item === "object" && "role" in item)
  ) {
    return json(value);
  }
  return json([{ role, parts: [{ type: "text", content: text(value) }] }]);
}
