import { diag } from "@opentelemetry/api";

const reported = new Set<string>();

export function reportOnce(reason: string, message: string): void {
  if (reported.has(reason)) return;
  reported.add(reason);
  try {
    diag.warn(message);
  } catch {
    // A caller's diagnostic logger must not interrupt application work.
  }
}
