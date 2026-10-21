import { context, diag } from "@opentelemetry/api";
import { suppressTracing } from "@opentelemetry/core";
import {
  activeConfiguration,
  configuredStatus,
  disabledStatus,
  type Credentials,
  type Status,
  type StatusReason,
} from "./configuration.js";
import { lastSetupFailure } from "./failures.js";

const WIDTH = 80;
const LABEL_WIDTH = 14;
const MAX_FIELD_CHARS = 500;
const MAX_RESPONSE_BYTES = 1024 * 1024;
const TIMEOUT_MS = 5000;

const MODES: Record<Status["mode"], string> = {
  owned: "a tracer provider we created",
  attached: "attached to a tracer provider you own",
};

// The reason text shared by the SDKs, in TypeScript terms: the setup is
// ConvergentSpanProcessor, warnings go to OpenTelemetry's diagnostic logger,
// and CONVERGENT_DEBUG adds a console logger only when the application set none.
const REASONS: Record<StatusReason, string> = {
  missing_config:
    "init() or ConvergentSpanProcessor has not run in this process; a call that raised configured nothing. Set CONVERGENT_API_KEY. For init() you can also set CONVERGENT_SPANS_DIR to write spans to a file instead.",
  invalid_config:
    "init() or ConvergentSpanProcessor was given a configuration that cannot work, and strict mode is off, so tracing is disabled and nothing is sent. OpenTelemetry's diagnostic logger carries the exact problem as a warning. Set CONVERGENT_STRICT=1 to stop the process at startup instead.",
  setup_failed:
    "init() or ConvergentSpanProcessor ran but tracing could not start, so nothing is being recorded. OpenTelemetry's diagnostic logger already carries a warning naming what failed. Set CONVERGENT_DEBUG=1 if your application sets no diagnostic logger.",
  existing_provider:
    "OpenTelemetry refused registration. A provider may already be registered, or another @opentelemetry/api version is loaded. Run npm ls @opentelemetry/api.",
  already_configured:
    "init() or ConvergentSpanProcessor ran more than once with different settings. The first configuration is the one running, and the later call's settings were not applied. Tracing itself is working.",
};

/** One problem the server can see. `code` is open-ended. `message` is the server's own text. */
export interface Note {
  code: string;
  message: string;
}

/** The result of the server request. The names are shared by the SDKs where both have one. */
export type RoundTrip =
  | "ok"
  | "no_credentials"
  | `http_${number}`
  | "not_a_check_response"
  | "bad_json"
  | "response_too_large"
  | "TimeoutError"
  | `E${string}`
  | `UND_ERR_${string}`
  | "connection_failed"
  | "unexpected";

export interface ReportFields {
  status: Status;
  roundTrip?: RoundTrip;
  roundTripMs?: number;
  endpoint?: string;
  organizationId?: string;
  agents?: string[];
  agentsTruncated?: boolean;
  notes?: Note[];
}

/**
 * What `check()` found. Print it with `String(report)`.
 *
 * `status` is the local half, as `init()` reports it. The rest is what the
 * server said about the configured key and release. `roundTrip` is `"ok"`, or
 * why the request did not succeed: `no_credentials`, `http_<status>` (a
 * redirect is refused and reported by its status), `not_a_check_response`,
 * `bad_json`, `response_too_large`, `TimeoutError`, a Node error code such as
 * `ECONNREFUSED`, `connection_failed` when there is no code, or
 * `unexpected`.
 */
export class Report {
  readonly status: Status;
  readonly roundTrip: RoundTrip;
  readonly roundTripMs?: number;
  readonly endpoint?: string;
  readonly organizationId?: string;
  readonly agents: readonly string[];
  readonly agentsTruncated: boolean;
  readonly notes: readonly Note[];

  constructor(fields: ReportFields) {
    this.status = fields.status;
    this.roundTrip = fields.roundTrip ?? "no_credentials";
    if (fields.roundTripMs !== undefined) this.roundTripMs = fields.roundTripMs;
    if (fields.endpoint !== undefined) this.endpoint = fields.endpoint;
    if (fields.organizationId !== undefined) this.organizationId = fields.organizationId;
    this.agents = fields.agents ?? [];
    this.agentsTruncated = fields.agentsTruncated ?? false;
    this.notes = fields.notes ?? [];
  }

  /**
   * True when tracing is on, no local part is known to be broken, and the
   * server accepted the key. A correct file-only setup is false, because there
   * is no key to check. Gate on `status.enabled` for that one.
   */
  get ok(): boolean {
    return (
      this.status.enabled &&
      (this.status.reason === null || this.status.reason === "already_configured") &&
      this.roundTrip === "ok"
    );
  }

  toString(): string {
    const { status } = this;
    const lines = [`convergent: ${status.enabled ? "enabled" : "disabled"}`];
    if (status.enabled) {
      lines.push(
        row("release", status.release ?? "not set"),
        row("mode", MODES[status.mode]),
        row("sending to", status.destinations.join(", ") || "nothing"),
      );
      const filters = this.filtersLine();
      if (filters) lines.push(row("filters", filters));
    } else {
      lines.push(row("reason", status.reason ?? "unknown"));
    }
    const explanation = status.reason && REASONS[status.reason];
    if (explanation) lines.push(row("", explanation));

    lines.push("", row("round trip", this.roundTripLine()));
    if (this.roundTrip !== "ok") return lines.join("\n");
    lines.push(
      row("key", this.organizationId ?? "unknown"),
      row("agents", this.agentsLine()),
      "",
      ...this.notesBlock(),
    );
    return lines.join("\n");
  }

  // The running filter rules, reject first because reject wins.
  // Empty when no rule is configured, so the common report stays short.
  private filtersLine(): string {
    const directions = [
      ["reject", this.status.rejectSpanAttributes],
      ["require", this.status.requireSpanAttributes],
    ] as const;
    return directions
      .flatMap(([direction, rules]) =>
        rules
          ? [
              `${direction} ${Object.keys(rules)
                .sort()
                .map((name) => `${name}=${(rules[name] ?? []).map(String).join("|")}`)
                .join(", ")}`,
            ]
          : [],
      )
      .join("; ");
  }

  private roundTripLine(): string {
    if (this.roundTrip === "ok") return `ok (${this.roundTripMs}ms)`;
    if (this.roundTrip === "no_credentials") {
      return "not attempted, there is no api key to check with";
    }
    return `failed (${this.roundTrip})   endpoint ${this.endpoint}`;
  }

  private agentsLine(): string {
    if (!this.agents.length) return "none linked to this release yet";
    const listed = this.agents.join(", ");
    return this.agentsTruncated ? `${listed}, and more the server did not list` : listed;
  }

  private notesBlock(): string[] {
    if (!this.notes.length) return ["  no notes"];
    const heading = `  ${this.notes.length} note${this.notes.length > 1 ? "s" : ""}`;
    return [heading, ...this.notes.map((note) => wrap(note.message, "    - ", " ".repeat(6)))];
  }
}

class CheckFailure extends Error {
  constructor(readonly reason: RoundTrip) {
    super(reason);
  }
}

// Node's fetch throws a
// TypeError whose cause carries the system or undici error code, such as
// ECONNREFUSED, so that code is the reason here.
function networkFailure(error: unknown): CheckFailure {
  if (error instanceof CheckFailure) return error;
  if (error instanceof Error && error.name === "TimeoutError")
    return new CheckFailure("TimeoutError");
  const code: unknown =
    error instanceof Error && error.cause instanceof Error
      ? (error.cause as { code?: unknown }).code
      : undefined;
  if (typeof code === "string" && /^(?:E[A-Z0-9_]+|UND_ERR_[A-Z0-9_]+)$/u.test(code)) {
    return new CheckFailure(code as RoundTrip);
  }
  return new CheckFailure("connection_failed");
}

/** The status check() reports, and the credentials it checks, from one read of the state. */
function target(): { status: Status; credentials: Credentials | undefined } {
  const configuration = activeConfiguration();
  if (configuration) {
    return {
      status: configuredStatus(configuration),
      credentials: configuration.processor.credentials,
    };
  }
  const failure = lastSetupFailure();
  if (!failure) {
    return {
      status: disabledStatus("missing_config"),
      credentials: undefined,
    };
  }
  const { reason, resolved } = failure;
  return {
    status: disabledStatus(reason, resolved?.release),
    credentials: resolved?.credentials,
  };
}

async function readLimited(response: Response): Promise<string> {
  const chunks: Uint8Array[] = [];
  let size = 0;
  if (response.body) {
    for await (const chunk of response.body) {
      size += chunk.byteLength;
      if (size > MAX_RESPONSE_BYTES) {
        await response.body.cancel().catch(() => undefined);
        throw new CheckFailure("response_too_large");
      }
      chunks.push(chunk);
    }
  }
  try {
    return new TextDecoder("utf-8", { fatal: true }).decode(Buffer.concat(chunks));
  } catch {
    throw new CheckFailure("bad_json");
  }
}

async function getJson(url: string, apiKey: string): Promise<Record<string, unknown>> {
  let response: Response;
  try {
    // Suppressed so the application's own HTTP instrumentation does not record
    // this request or its authorization header.
    response = await context.with(suppressTracing(context.active()), () =>
      fetch(url, {
        headers: { Authorization: `Bearer ${apiKey}`, Accept: "application/json" },
        // A redirect would carry the key to another address.
        redirect: "manual",
        signal: AbortSignal.timeout(TIMEOUT_MS),
      }),
    );
  } catch (error) {
    throw networkFailure(error);
  }
  if (response.status < 200 || response.status > 299) {
    await response.body?.cancel().catch(() => undefined);
    throw new CheckFailure(`http_${response.status}`);
  }
  let text: string;
  try {
    text = await readLimited(response);
  } catch (error) {
    throw networkFailure(error);
  }
  let body: unknown;
  try {
    body = JSON.parse(text);
  } catch {
    throw new CheckFailure("bad_json");
  }
  if (typeof body !== "object" || body === null || Array.isArray(body)) {
    throw new CheckFailure("bad_json");
  }
  return body as Record<string, unknown>;
}

/**
 * Reports this process's configuration, then asks the server once whether it
 * accepts the configured key. Always resolves. A failure is reported in the
 * result. A successful check does not show that any trace was ingested.
 */
export async function check(): Promise<Report> {
  const { status, credentials } = target();
  if (!credentials) return new Report({ status });

  const { endpoint } = credentials;
  const query = status.release ? `?${new URLSearchParams({ release: status.release })}` : "";
  const started = performance.now();
  let body: Record<string, unknown>;
  try {
    body = await getJson(`${endpoint}/v1/check${query}`, credentials.apiKey);
  } catch (error) {
    if (error instanceof CheckFailure) {
      return new Report({ status, roundTrip: error.reason, endpoint });
    }
    try {
      diag.warn(
        `Convergent could not complete its check request: ${error instanceof Error ? error.name : typeof error}`,
      );
    } catch {
      // A caller's diagnostic logger must not reject check().
    }
    return new Report({ status, roundTrip: "unexpected", endpoint });
  }

  const organizationId = stringField(body.key, "organization_id");
  if (!organizationId) {
    return new Report({ status, roundTrip: "not_a_check_response", endpoint });
  }
  return new Report({
    status,
    roundTrip: "ok",
    roundTripMs: Math.trunc(performance.now() - started),
    endpoint,
    organizationId,
    agents: rows(body, "agents")
      .map((item) => stringField(item, "name") || stringField(item, "agent_id"))
      .filter((name): name is string => Boolean(name)),
    agentsTruncated: body.agents_truncated === true,
    notes: rows(body, "notes").flatMap((item) => {
      const code = stringField(item, "code");
      const message = stringField(item, "message");
      return code && message ? [{ code, message }] : [];
    }),
  });
}

function rows(body: Record<string, unknown>, key: string): unknown[] {
  const value = body[key];
  return Array.isArray(value) ? value : [];
}

function stringField(source: unknown, key: string): string | undefined {
  if (typeof source !== "object" || source === null || Array.isArray(source)) return;
  const value = (source as Record<string, unknown>)[key];
  return typeof value === "string" ? printable(value) : undefined;
}

// Any character that is not printable: controls, format characters such
// as bidi overrides, surrogates, private use, unassigned, and separators other
// than the ASCII space. Server text, including customer agent names, must not
// rewrite the lines around it when the report is pasted somewhere.
const UNPRINTABLE = /[\p{Cc}\p{Cf}\p{Cs}\p{Co}\p{Cn}\p{Zl}\p{Zp}]|(?! )\p{Zs}/gu;

function printable(value: string): string {
  const chars = Array.from(value);
  const text = chars.slice(0, MAX_FIELD_CHARS).join("").replace(UNPRINTABLE, " ");
  return chars.length > MAX_FIELD_CHARS ? `${text}...` : text;
}

function row(label: string, value: string): string {
  return wrap(value, `  ${label.padEnd(12)}`, " ".repeat(LABEL_WIDTH));
}

// Wrap at spaces without breaking words, so an id, a path, or a
// hyphenated agent name stays searchable. Spaces inside a line are kept and
// spaces at a line break are dropped.
function wrap(text: string, first: string, rest: string): string {
  const chunks = text.replace(/\s/gu, " ").split(/( +)/u).filter(Boolean);
  const lines: string[] = [];
  let line = "";
  const indent = (): string => (lines.length ? rest : first);
  for (const chunk of chunks) {
    const blank = chunk.trim() === "";
    if (!line && blank) continue;
    if (line && !blank && indent().length + line.length + chunk.length > WIDTH) {
      lines.push(indent() + line.trimEnd());
      line = chunk;
    } else {
      line += chunk;
    }
  }
  if (line.trimEnd()) lines.push(indent() + line.trimEnd());
  return lines.join("\n");
}
