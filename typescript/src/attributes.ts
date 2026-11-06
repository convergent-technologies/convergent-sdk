import type { Attributes, AttributeValue } from "@opentelemetry/api";
import { reportOnce } from "./diagnostics.js";
import {
  GEN_AI_AGENT_NAME,
  GEN_AI_AGENT_VERSION,
  GEN_AI_INPUT_MESSAGES,
  GEN_AI_OPERATION_NAME,
  GEN_AI_OUTPUT_MESSAGES,
  GEN_AI_TOOL_CALL_ARGUMENTS,
  GEN_AI_TOOL_CALL_RESULT,
  GEN_AI_TOOL_NAME,
  OPERATIONS,
} from "./telemetry-keys.js";

// Kept a Map: indexing OPERATIONS directly would find a built-in property such as
// "constructor" on the object's prototype.
export const operations: ReadonlyMap<string, string> = new Map(Object.entries(OPERATIONS));

export const reservedAttributeKeys: ReadonlySet<string> = new Set([
  GEN_AI_OPERATION_NAME,
  GEN_AI_AGENT_NAME,
  GEN_AI_AGENT_VERSION,
  GEN_AI_TOOL_NAME,
  GEN_AI_INPUT_MESSAGES,
  GEN_AI_OUTPUT_MESSAGES,
  GEN_AI_TOOL_CALL_ARGUMENTS,
  GEN_AI_TOOL_CALL_RESULT,
]);

export function acceptedAttribute(key: string, value: unknown): value is AttributeValue {
  if (typeof key !== "string" || key.startsWith("convergent.") || reservedAttributeKeys.has(key)) {
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

// JSON's own values, which both SDKs encode alike: null, booleans, finite numbers,
// strings, arrays, and plain objects of these. An undefined property is left out, as
// JSON.stringify leaves it out. A class instance, a Date, a Map, NaN, a cycle, or a
// hole is not, since JSON.stringify would change it, and Python has no form for it.
function portable(value: unknown, path: Set<object>): boolean {
  if (value === null || typeof value === "string" || typeof value === "boolean") return true;
  if (typeof value === "number") return Number.isFinite(value);
  if (typeof value !== "object" || path.has(value)) return false;
  path.add(value);
  try {
    if (Array.isArray(value)) {
      for (const item of value) if (!portable(item, path)) return false;
      return true;
    }
    const prototype = Object.getPrototypeOf(value);
    if (prototype !== Object.prototype && prototype !== null) return false;
    return Object.values(value).every((item) => item === undefined || portable(item, path));
  } finally {
    path.delete(value);
  }
}

// Compact JSON, or undefined, logged once, for a value that is not portable JSON.
export function json(value: unknown): string | undefined {
  try {
    if (portable(value, new Set())) return JSON.stringify(value);
  } catch {
    // A getter or proxy that throws is the caller's code; nothing here may raise.
  }
  reportOnce(
    "content_not_json",
    "Convergent dropped span content that is not portable JSON. Pass plain objects, arrays, strings, finite numbers, booleans, and null.",
  );
  return undefined;
}

export function text(value: unknown): string | undefined {
  return typeof value === "string" ? value : json(value);
}

export function messages(role: "user" | "assistant", value: unknown): string | undefined {
  if (
    Array.isArray(value) &&
    value.length > 0 &&
    value.every((item) => item !== null && typeof item === "object" && "role" in item)
  ) {
    return json(value);
  }
  const content = text(value);
  return content === undefined ? undefined : json([{ role, parts: [{ type: "text", content }] }]);
}
