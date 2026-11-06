import { context, createContextKey } from "@opentelemetry/api";
import { reportOnce } from "./diagnostics.js";
import { CONVERGENT_SESSION_ID } from "./telemetry-keys.js";

export const sessionContextKey = createContextKey(CONVERGENT_SESSION_ID);

export function session<T>(id: string, callback: () => T): T {
  const value = typeof id === "string" ? id.trim() : "";
  if (!value || Array.from(value).length > 128) {
    reportOnce(
      "invalid_session_id",
      "Convergent ignored an invalid session id: use a nonblank string of 1-128 characters.",
    );
    return callback();
  }

  let called = false;
  try {
    return context.with(context.active().setValue(sessionContextKey, value), () => {
      called = true;
      return callback();
    });
  } catch (error) {
    if (called) throw error;
    reportOnce(
      "session_context",
      "Convergent could not activate a session; application work will continue.",
    );
    return callback();
  }
}
