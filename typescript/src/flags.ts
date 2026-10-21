import { diag, DiagConsoleLogger, DiagLogLevel } from "@opentelemetry/api";

const variables = { strict: "CONVERGENT_STRICT", debug: "CONVERGENT_DEBUG" } as const;
export type Flag = keyof typeof variables;

let debugLogger = false;

/** The type of a rejected value, never the value, which can be a secret. */
export function typeName(value: unknown): string {
  if (value === null) return "null";
  if (typeof value !== "object") return typeof value;
  return Object.getPrototypeOf(value)?.constructor?.name ?? "object";
}

/** False when unset, undefined when set to a value it does not take. */
function variable(flag: Flag): boolean | undefined {
  const value = (process.env[variables[flag]] ?? "").trim().toLowerCase();
  if (["1", "true", "yes", "on"].includes(value)) return true;
  if (["", "0", "false", "no", "off"].includes(value)) return false;
  return undefined;
}

/** Throws for the first flag option that is not a boolean, then the first unreadable variable. */
export function checkFlags(options: Partial<Record<Flag, unknown>>, flags: readonly Flag[]): void {
  for (const flag of flags) {
    const value = options[flag];
    if (value !== undefined && typeof value !== "boolean") {
      throw new TypeError(`${flag} takes a boolean, got ${typeName(value)}`);
    }
  }
  for (const flag of flags) {
    // The value is not echoed, because a variable set from the wrong expansion can hold a secret.
    if (variable(flag) === undefined) {
      throw new Error(
        `${variables[flag]} is set to a value it does not take. It takes 1, true, yes, or on, or 0, false, no, or off.`,
      );
    }
  }
}

/** Strict throws on an invalid configuration instead of disabling tracing. */
export function strictEnabled(strict: unknown): boolean {
  return strict === true || variable("strict") === true;
}

function diagLoggerSet(): boolean {
  try {
    // @opentelemetry/api keeps its globals under this symbol. Its getter is not public.
    const api = (globalThis as Record<symbol, { diag?: unknown } | undefined>)[
      Symbol.for("opentelemetry.js.api.1")
    ];
    return api?.diag !== undefined;
  } catch {
    return true;
  }
}

/**
 * Makes Convergent's messages visible when no diagnostic logger is set. It never replaces the
 * application's logger. INFO keeps out OpenTelemetry's DEBUG output, which prints exported spans.
 */
export function enableDebug(debug: unknown): void {
  if (debugLogger || (debug !== true && variable("debug") !== true)) return;
  debugLogger = true;
  if (diagLoggerSet()) return;
  try {
    diag.setLogger(new DiagConsoleLogger(), { logLevel: DiagLogLevel.INFO });
  } catch {
    // A diagnostic logger must not interrupt application work.
  }
}
