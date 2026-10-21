import { basename, join, resolve } from "node:path";
import { reportOnce } from "./diagnostics.js";

export const SPANS_FILENAME = "spans.jsonl";

export interface FileOptions {
  filename?: string;
  mode?: number;
}

export interface ConsoleOptions {
  stream?: "stdout" | "stderr";
  pretty?: boolean;
}

function typeName(value: unknown): string {
  if (value === null) return "null";
  if (typeof value !== "object") return typeof value;
  return value.constructor?.name ?? "object";
}

/**
 * Writes every exported span to `<path>/<filename>` as OTLP/JSON, one span per line.
 *
 * Construction only validates. The file is opened when `init()` or
 * `ConvergentSpanProcessor` starts tracing. `mode` defaults to owner read and
 * write because the file holds every recorded prompt and output.
 */
export class File {
  readonly path: string;
  readonly filename: string;
  readonly mode: number;

  constructor(path: string, { filename = SPANS_FILENAME, mode = 0o600 }: FileOptions = {}) {
    if (typeof path !== "string" || !path) {
      throw new TypeError(`File(path) takes a nonempty string, got ${typeName(path)}`);
    }
    if (typeof filename !== "string") {
      throw new TypeError(`File({ filename }) takes a string, got ${typeName(filename)}`);
    }
    if (!Number.isInteger(mode)) {
      throw new TypeError(`File({ mode }) takes integer permission bits, got ${typeName(mode)}`);
    }
    if (mode < 0 || mode > 0o777) {
      throw new RangeError(
        `File({ mode }) takes permission bits in 0..0o777, got 0o${mode.toString(8)}`,
      );
    }
    // A name with a directory part would write spans outside `path`.
    if (
      basename(filename) !== filename ||
      filename.includes("/") ||
      filename.includes("\\") ||
      filename === "" ||
      filename === "." ||
      filename === ".."
    ) {
      throw new RangeError(
        `File({ filename }) takes a bare file name, not a path. The default is "${SPANS_FILENAME}".`,
      );
    }
    this.path = path;
    this.filename = filename;
    this.mode = mode;
    Object.freeze(this);
  }

  /** The file this destination writes. */
  get target(): string {
    return join(this.path, this.filename);
  }
}

/**
 * Writes every exported span to stdout or stderr as OTLP/JSON.
 *
 * The compact default writes one span per line, the same format as a `File`.
 * `pretty` indents each span for reading.
 */
export class Console {
  readonly stream: "stdout" | "stderr";
  readonly pretty: boolean;

  constructor({ stream = "stdout", pretty = false }: ConsoleOptions = {}) {
    if (typeof pretty !== "boolean") {
      throw new TypeError(`Console({ pretty }) takes a boolean, got ${typeName(pretty)}`);
    }
    if (stream !== "stdout" && stream !== "stderr") {
      throw new RangeError('Console({ stream }) takes "stdout" or "stderr".');
    }
    this.stream = stream;
    this.pretty = pretty;
    Object.freeze(this);
  }
}

export type Destination = File | Console;

/** The name `Status.destinations` reports, which is also the key duplicates collapse on. */
export function destinationName(destination: Destination): string {
  return destination instanceof File
    ? `file:${destination.target}`
    : `console:${destination.stream}`;
}

/**
 * Explicit destinations plus the environment shorthands, with file paths made
 * absolute before duplicates are removed.
 *
 * `CONVERGENT_SPANS_DIR` adds a `File` and `CONVERGENT_TRACES_EXPORTER=console`
 * adds a `Console` when none is configured. Both add to the explicit list.
 * Throws when an entry is not a destination or the exporter variable has
 * another value, which the caller reports as `invalid_config`.
 */
export function resolveDestinations(given: unknown): Destination[] {
  if (given !== undefined && !Array.isArray(given)) {
    throw new TypeError("destinations takes an array of File and Console values.");
  }
  const exporter = process.env.CONVERGENT_TRACES_EXPORTER?.trim() ?? "";
  if (exporter && exporter.toLowerCase() !== "console") {
    throw new RangeError('CONVERGENT_TRACES_EXPORTER takes only "console".');
  }
  const candidates: Destination[] = [];
  const spansDir = process.env.CONVERGENT_SPANS_DIR?.trim();
  if (spansDir) candidates.push(new File(resolve(spansDir)));
  for (const destination of (given ?? []) as unknown[]) {
    if (destination instanceof File) {
      candidates.push(
        new File(resolve(destination.path), {
          filename: destination.filename,
          mode: destination.mode,
        }),
      );
    } else if (destination instanceof Console) {
      candidates.push(destination);
    } else {
      throw new TypeError("destinations takes File and Console values.");
    }
  }
  if (exporter && !candidates.some((destination) => destination instanceof Console)) {
    candidates.push(new Console());
  }

  const unique = new Map<string, Destination>();
  for (const destination of candidates) {
    const name = destinationName(destination);
    if (!unique.has(name)) unique.set(name, destination);
  }
  const resolved = [...unique.values()];
  const files = resolved.flatMap((destination) =>
    destination instanceof File ? [destination.target] : [],
  );
  if (files.length > 1) {
    reportOnce(
      "several_spans_files",
      `Convergent is writing every span to more than one file (${files.join(", ")}). A File destination adds to CONVERGENT_SPANS_DIR rather than replacing it.`,
    );
  }
  return resolved;
}
