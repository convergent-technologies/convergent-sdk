import {
  closeSync,
  constants,
  fchmodSync,
  fstatSync,
  fsync,
  mkdirSync,
  openSync,
  writeSync,
} from "node:fs";
import { dirname } from "node:path";
import { ExportResultCode, type ExportResult } from "@opentelemetry/core";
import { JsonTraceSerializer } from "@opentelemetry/otlp-transformer";
import type { ReadableSpan, SpanExporter } from "@opentelemetry/sdk-trace-base";
import { File, type Destination } from "./destinations.js";
import { reportOnce } from "./diagnostics.js";

const decoder = new TextDecoder();

// OpenTelemetry's JSON serializer writes every field, including defaults. The
// protobuf JSON mapping, which every Convergent SDK's files use, omits a field holding
// its default value and writes 64-bit integers as strings. Normalizing to that
// mapping keeps one file format across the SDKs.
// The scalar choices of an AnyValue keep their default, because a set choice is
// always written: `{ "intValue": "0" }` is not an empty value.
const anyValueScalars = new Set([
  "stringValue",
  "boolValue",
  "intValue",
  "doubleValue",
  "bytesValue",
]);

function protobufJson(value: unknown, key?: string): unknown {
  if (key !== undefined && anyValueScalars.has(key)) {
    return key === "intValue" ? String(value) : value;
  }
  if (Array.isArray(value)) {
    const items = value.map((item) => protobufJson(item)).filter((item) => item !== undefined);
    return items.length ? items : undefined;
  }
  if (value === null || value === undefined || value === "" || value === 0 || value === false) {
    return undefined;
  }
  if (typeof value !== "object") return value;
  const entries = Object.entries(value)
    .map(([name, item]) => [name, protobufJson(item, name)] as const)
    .filter(([, item]) => item !== undefined);
  return entries.length ? Object.fromEntries(entries) : {};
}

/** One span as a single-span OTLP/JSON `ExportTraceServiceRequest`. */
export function spanToOtlpJson(span: ReadableSpan, indent?: number): string {
  const request = JSON.parse(decoder.decode(JsonTraceSerializer.serializeRequest([span])));
  return JSON.stringify(protobufJson(request), undefined, indent);
}

function serialize(spans: ReadableSpan[], indent?: number): string {
  return spans.map((span) => `${spanToOtlpJson(span, indent)}\n`).join("");
}

function failed(error: unknown): ExportResult {
  return {
    code: ExportResultCode.FAILED,
    error: error instanceof Error ? error : new Error("Local span export failed."),
  };
}

/**
 * Appends spans to one file.
 *
 * Each batch is one synchronous append, so batches from this exporter never
 * interleave. Other processes appending to the same local file are kept apart
 * by `O_APPEND`. That does not hold on network filesystems.
 */
class FileSpanExporter implements SpanExporter {
  private fd: number | undefined;

  constructor(target: string, mode: number) {
    mkdirSync(dirname(target), { recursive: true });
    // O_NOFOLLOW refuses a symbolic link planted at the file itself. Parent
    // directories are followed. O_NONBLOCK makes a FIFO without a reader fail
    // instead of blocking setup. Windows has neither flag, and there access
    // depends on the file's ACLs rather than `mode`.
    const fd = openSync(
      target,
      constants.O_WRONLY |
        constants.O_CREAT |
        constants.O_APPEND |
        (constants.O_NOFOLLOW ?? 0) |
        (constants.O_NONBLOCK ?? 0),
      mode,
    );
    try {
      // Refuse anything but a regular file, and a file another user created
      // first, which that user could read.
      const stats = fstatSync(fd);
      if (!stats.isFile() || (process.getuid && stats.uid !== process.getuid())) {
        throw new Error("The spans file is not a regular file this user owns.");
      }
    } catch (error) {
      closeSync(fd);
      throw error;
    }
    this.fd = fd;
    try {
      // The open mode applies only to a new file, after the umask.
      fchmodSync(this.fd, mode);
    } catch {
      reportOnce(
        `spans_file_mode:${target}`,
        `Convergent could not set mode 0o${mode.toString(8)} on ${target}; it keeps the permissions the filesystem gave it.`,
      );
    }
  }

  export(spans: ReadableSpan[], callback: (result: ExportResult) => void): void {
    if (this.fd === undefined) {
      callback(failed(new Error("The spans file is closed.")));
      return;
    }
    try {
      const data = Buffer.from(serialize(spans));
      for (let offset = 0; offset < data.length;) {
        offset += writeSync(this.fd, data, offset);
      }
      callback({ code: ExportResultCode.SUCCESS });
    } catch (error) {
      callback(failed(error));
    }
  }

  forceFlush(): Promise<void> {
    const fd = this.fd;
    if (fd === undefined) return Promise.resolve();
    return new Promise((resolve, reject) => {
      fsync(fd, (error) => (error ? reject(error) : resolve()));
    });
  }

  async shutdown(): Promise<void> {
    if (this.fd === undefined) return;
    try {
      await this.forceFlush();
    } catch {
      // Closing the descriptor matters more than a final fsync error.
    } finally {
      if (this.fd !== undefined) closeSync(this.fd);
      this.fd = undefined;
    }
  }
}

/** Writes spans to stdout or stderr, looked up on every write so a replaced stream is honored. */
class ConsoleSpanExporter implements SpanExporter {
  private closed = false;

  constructor(
    private readonly stream: "stdout" | "stderr",
    private readonly indent: number | undefined,
  ) {}

  export(spans: ReadableSpan[], callback: (result: ExportResult) => void): void {
    if (this.closed) {
      callback(failed(new Error("The console destination is shut down.")));
      return;
    }
    try {
      // One write per batch, so another writer cannot land inside a span.
      process[this.stream].write(serialize(spans, this.indent), (error) => {
        callback(error ? failed(error) : { code: ExportResultCode.SUCCESS });
      });
    } catch (error) {
      callback(failed(error));
    }
  }

  forceFlush(): Promise<void> {
    return Promise.resolve();
  }

  shutdown(): Promise<void> {
    this.closed = true;
    return Promise.resolve();
  }
}

/** The exporter for one local destination. Opening a file can throw. */
export function createLocalExporter(destination: Destination): SpanExporter {
  if (destination instanceof File) {
    return new FileSpanExporter(destination.target, destination.mode);
  }
  return new ConsoleSpanExporter(destination.stream, destination.pretty ? 2 : undefined);
}
