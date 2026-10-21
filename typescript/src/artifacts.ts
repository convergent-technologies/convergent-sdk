import { createHash } from "node:crypto";
import { closeSync, constants, fstatSync, openSync, readSync } from "node:fs";
import {
  request as httpRequest,
  type IncomingHttpHeaders,
  type OutgoingHttpHeaders,
} from "node:http";
import { request as httpsRequest } from "node:https";
import { basename } from "node:path";
import { promisify } from "node:util";
import { zstdCompress } from "node:zlib";
import { context, ROOT_CONTEXT, type Span } from "@opentelemetry/api";
import { suppressTracing } from "@opentelemetry/core";
import type { ReadableSpan } from "@opentelemetry/sdk-trace-base";
import { reportOnce } from "./diagnostics.js";
import { version } from "./version.js";

export type ArtifactOrigin = "user" | "agent" | "model" | "tool" | "external";

export interface ArtifactOptions {
  filename?: string;
  mediaType?: string;
  origin?: ArtifactOrigin;
}

type Role = "input" | "output";

// "refused" is a final answer about the bytes, so they are never sent again.
// "fail" drops it this time only, and a later span can send it again.
type Step = "stored" | "refused" | "retry" | "pause" | "stop" | "fail";

interface Capture {
  role: Role;
  digest: string;
  sizeBytes: number;
  mediaType: string;
  filename: string | undefined;
  origin: ArtifactOrigin | undefined;
  traceId: string;
  body: Buffer;
}

interface Answer {
  status: number;
  headers: IncomingHttpHeaders;
  body: string;
}

const listKeys: Record<Role, string> = {
  input: "convergent.artifacts.input",
  output: "convergent.artifacts.output",
};
const droppedKey = "convergent.artifacts.dropped";
// The route says uploads are off for the organization with this error reason, and
// only with it, so a 403 or 404 from a proxy or load balancer cannot turn uploads
// off. Uploads and file lists pause for pauseMs, then the next file asks again.
const uploadsOffReason = "artifact_uploads_disabled";
// Carries the reason on answers that have no body, such as a HEAD.
const reasonHeader = "x-convergent-reason";
const pauseMs = 10 * 60_000;
// A bad key ends uploads and file lists for the rest of the process.
const stopStatus = new Set([401]);
const refusedStatus = new Set([413, 422]);
const errorBodyBytes = 4096;
const userAgent = `convergent-sdk-typescript/${version}`;
const retryableStatus = new Set([429, 500, 502, 503, 504]);
const MiB = 1024 * 1024;
// Files are packed into one attribute per role, so a span's count is capped.
const filesPerSpan = 100;
const fileBytes = 16 * MiB;
const heldBytesLimit = 128 * MiB;
const queuedBytesLimit = 64 * MiB;
const answeredUploads = 4096;
const chunkBytes = 64 * 1024;
const attempts = 6;
// The ingest edge blocks an over-limit address with 429 and Retry-After: 300.
const retryAfterCapS = 300;
const requestTimeoutMs = 30_000;
const origins = new Set<unknown>(["user", "agent", "model", "tool", "external"]);
// Ingestion's limits on one listed media_type and filename.
const mediaTypeChars = 255;
const filenameChars = 1024;
const compress = promisify(zstdCompress);
// Leading bytes of image and archive formats zstd cannot shrink: PNG, JPEG,
// GIF, WebP, gzip, zip (DOCX, XLSX, EPUB), zstd, xz, bzip2, and 7z.
const compressedSignature =
  // oxlint-disable-next-line no-control-regex
  /^(?:\x89PNG|\xff\xd8\xff|GIF8|RIFF[\s\S]{4}WEBP|\x1f\x8b|PK\x03\x04|\x28\xb5\x2f\xfd|\xfd7zXZ\x00|BZh|7z\xbc\xaf\x27\x1c)/u;

export class Artifacts {
  private state: "open" | "stopped" | "closed" = "open";
  // Until this performance.now() reading, the route said uploads are off.
  private pausedUntil = 0;
  private readonly held = new Map<string, { captures: Capture[]; dropped: number }>();
  private heldBytes = 0;
  private readonly queue: Capture[] = [];
  private queuedBytes = 0;
  // Queued or in flight, by digest.
  private readonly pending = new Set<string>();
  // Stored or refused, by digest, oldest first, so a file attached on every turn is sent once.
  private readonly answered = new Set<string>();
  private draining: Promise<void> | undefined;
  private wake: (() => void) | undefined;

  constructor(private readonly options: { apiKey: string; endpoint: string }) {}

  attach(span: Span, role: Role, source: unknown, options: ArtifactOptions): void {
    try {
      if (!this.accepting() || !span.isRecording()) return;
      const { traceId, spanId } = span.spanContext();
      let held = this.held.get(spanId);
      if (!held) {
        held = { captures: [], dropped: 0 };
        this.held.set(spanId, held);
      }
      const budgetFull = this.heldBytes >= heldBytesLimit;
      const capture =
        held.captures.length >= filesPerSpan || budgetFull
          ? undefined
          : captureOf(role, source, options, traceId);
      const overBudget =
        capture !== undefined && this.heldBytes + capture.sizeBytes > heldBytesLimit;
      if (!capture || overBudget) {
        held.dropped += 1;
        span.setAttribute(droppedKey, held.dropped);
        if (budgetFull || overBudget) {
          reportOnce(
            "artifact_held_full",
            `Convergent did not record a file because files held for spans that have not ended passed ${heldBytesLimit / MiB} MiB. The span counts it in ${droppedKey}.`,
          );
        }
        return;
      }
      held.captures.push(capture);
      this.heldBytes += capture.sizeBytes;
      span.setAttribute(
        listKeys[role],
        JSON.stringify(held.captures.filter((entry) => entry.role === role).map(listing)),
      );
    } catch {
      reportOnce("artifact_attach_failed", "Convergent could not record a file on a span.");
    }
  }

  // A span the filters withhold releases its files without sending them.
  spanEnded(span: ReadableSpan, send: boolean): void {
    try {
      const { spanId } = span.spanContext();
      const held = this.held.get(spanId);
      if (!held) return;
      this.held.delete(spanId);
      for (const capture of held.captures) this.heldBytes -= capture.sizeBytes;
      if (send) this.offer(held.captures);
    } catch {
      reportOnce("artifact_offer_failed", "Convergent could not queue a span's files.");
    }
  }

  drained(): Promise<void> {
    return this.draining ?? Promise.resolve();
  }

  close(): void {
    this.state = "closed";
    this.discard();
  }

  private offer(captures: readonly Capture[]): void {
    for (const capture of captures) {
      const { digest } = capture;
      if (this.answered.delete(digest)) {
        this.answered.add(digest);
        continue;
      }
      if (this.pending.has(digest) || !this.accepting()) continue;
      if (this.queuedBytes + capture.sizeBytes > queuedBytesLimit) {
        reportOnce(
          "artifact_queue_full",
          `Convergent did not upload a file because files waiting for upload passed ${queuedBytesLimit / MiB} MiB.`,
        );
        continue;
      }
      this.pending.add(digest);
      this.queuedBytes += capture.sizeBytes;
      this.queue.push(capture);
    }
    if (this.queue.length > 0 && !this.draining) {
      // Suppressed so an HTTP instrumentation neither traces the SDK's own
      // uploads nor parents them under the application's span.
      this.draining = context
        .with(suppressTracing(ROOT_CONTEXT), () => this.drain())
        .catch(() => {
          reportOnce("artifact_upload_failed", "Convergent could not upload a file.");
        });
    }
  }

  private async drain(): Promise<void> {
    try {
      while (this.state === "open") {
        const capture = this.queue.shift();
        if (!capture) break;
        const step = await this.deliver(capture).catch((): Step => {
          reportOnce("artifact_upload_failed", "Convergent could not upload a file.");
          return "fail";
        });
        this.pending.delete(capture.digest);
        this.queuedBytes -= capture.sizeBytes;
        if (step === "stored" || step === "refused") this.remember(capture.digest);
        if (step === "stop") this.stop();
        else if (step === "pause") this.pause();
      }
    } finally {
      this.draining = undefined;
    }
  }

  private async deliver(capture: Capture): Promise<Step> {
    const url = new URL(`${this.options.endpoint}/v1/artifacts/${capture.digest}`);
    const authorization = `Bearer ${this.options.apiKey}`;
    // Only a 200 carrying the file's size counts, so a proxy that answers 200
    // to everything cannot make uploads stop.
    const held = await send(url, "HEAD", {
      Authorization: authorization,
      "User-Agent": userAgent,
    }).catch(() => undefined);
    if (held?.status === 200 && held.headers["content-length"] === String(capture.sizeBytes)) {
      return "stored";
    }
    // The off answer comes before any bytes are sent.
    if (held?.headers[reasonHeader] === uploadsOffReason) return "pause";
    const isCompressed = compressedSignature.test(capture.body.subarray(0, 12).toString("latin1"));
    // The receiver sizes its memory hold from the size the frame declares.
    const body = isCompressed
      ? capture.body
      : await compress(capture.body, { pledgedSrcSize: capture.body.length });
    const headers: OutgoingHttpHeaders = {
      Authorization: authorization,
      "User-Agent": userAgent,
      "Content-Type": capture.mediaType,
      "Content-Length": body.length,
      "X-Convergent-Trace-Id": capture.traceId,
    };
    if (!isCompressed) headers["Content-Encoding"] = "zstd";
    if (capture.filename) headers["Content-Disposition"] = contentDisposition(capture.filename);
    let step: Step = "retry";
    for (let attempt = 0; attempt < attempts; attempt++) {
      const answer = await send(url, "PUT", headers, body).catch(() => undefined);
      const reason = answer?.headers[reasonHeader];
      step = answer
        ? stepFor(answer.status, typeof reason === "string" ? reason : errorReason(answer.body))
        : "retry";
      if (step !== "retry" || attempt === attempts - 1) break;
      const retryAfter = seconds(answer?.headers["retry-after"]);
      await this.sleep(
        retryAfter === undefined
          ? Math.min(2000 * 2 ** attempt, 60_000) * Math.random()
          : Math.min(Math.max(retryAfter, 1), retryAfterCapS) * 1000,
      );
      if (this.state !== "open") break;
    }
    return step;
  }

  private sleep(ms: number): Promise<void> {
    return new Promise((resolve) => {
      this.wake = resolve;
      setTimeout(resolve, ms).unref();
    });
  }

  private remember(digest: string): void {
    this.answered.add(digest);
    if (this.answered.size > answeredUploads) {
      this.answered.delete(this.answered.values().next().value!);
    }
  }

  private accepting(): boolean {
    return this.state === "open" && performance.now() >= this.pausedUntil;
  }

  private stop(): void {
    if (this.state !== "open") return;
    this.state = "stopped";
    this.discard();
    reportOnce(
      "artifact_uploads_stopped",
      "Convergent stopped uploading files for this process: the upload route refused the API key. Spans keep exporting and list no more files.",
    );
  }

  private pause(): void {
    this.pausedUntil = performance.now() + pauseMs;
    this.discard();
    reportOnce(
      "artifact_uploads_paused",
      "Convergent paused file uploads: the upload route says they are off. Spans keep exporting and list no files until it asks again in 10 minutes.",
    );
  }

  private discard(): void {
    for (const capture of this.queue.splice(0)) {
      this.pending.delete(capture.digest);
      this.queuedBytes -= capture.sizeBytes;
    }
    this.wake?.();
  }
}

function listing({
  digest,
  sizeBytes,
  mediaType,
  filename,
  origin,
}: Capture): Record<string, unknown> {
  return {
    digest,
    size_bytes: sizeBytes,
    media_type: mediaType,
    ...(filename ? { filename } : {}),
    ...(origin ? { origin } : {}),
  };
}

// Copies and hashes now, because the source can change or disappear before the span ends.
function captureOf(
  role: Role,
  source: unknown,
  options: ArtifactOptions,
  traceId: string,
): Capture | undefined {
  const { mediaType } = options;
  let { filename, origin } = options;
  // The upload sends the media type as a header.
  if (mediaType && !(/^[\x20-\x7e]+$/u.test(mediaType) && mediaType.length <= mediaTypeChars)) {
    reportOnce(
      "artifact_media_type",
      `Convergent did not record a file whose mediaType is not printable ASCII of at most ${mediaTypeChars} characters, such as 'text/csv'. The span counts it in ${droppedKey}.`,
    );
    return undefined;
  }
  if (origin !== undefined && !origins.has(origin)) {
    reportOnce(
      "artifact_origin",
      "Convergent recorded a file without its origin, which is not one of user, agent, model, tool, or external.",
    );
    origin = undefined;
  }
  let body: Buffer | undefined;
  if (source instanceof Uint8Array) {
    body = source.byteLength > fileBytes ? undefined : Buffer.copyBytesFrom(source);
  } else if (typeof source === "string" && /^https?:\/\//u.test(source)) {
    reportOnce(
      "artifact_url",
      `Convergent did not record a file given as a URL. Pre-signed URLs are not supported yet; pass a local path or bytes. The span counts it in ${droppedKey}.`,
    );
    return undefined;
  } else if (typeof source === "string") {
    filename ||= basename(source);
    try {
      body = readBounded(source);
    } catch {
      reportOnce(
        "artifact_unreadable",
        `Convergent could not read a file passed to inputArtifact() or outputArtifact(). The span counts it in ${droppedKey}.`,
      );
      return undefined;
    }
  } else {
    reportOnce(
      "artifact_source_type",
      `Convergent could not record a file that is not a local path or a Uint8Array. The span counts it in ${droppedKey}.`,
    );
    return undefined;
  }
  if (!body) {
    reportOnce(
      "artifact_too_large",
      `Convergent did not record a file larger than ${fileBytes / MiB} MiB. The span counts it in ${droppedKey}.`,
    );
    return undefined;
  }
  if (filename && filename.length > filenameChars) {
    reportOnce(
      "artifact_filename",
      `Convergent recorded a file without its filename, which is longer than ${filenameChars} characters.`,
    );
    filename = undefined;
  }
  return {
    role,
    digest: `sha256:${createHash("sha256").update(body).digest("hex")}`,
    sizeBytes: body.length,
    mediaType: mediaType || "application/octet-stream",
    filename,
    origin,
    traceId,
    body,
  };
}

// Reads in chunks so a source past the limit is never read whole. A pipe or
// device could block the event loop, so only regular files are read.
function readBounded(path: string): Buffer | undefined {
  const descriptor = openSync(path, constants.O_RDONLY | constants.O_NONBLOCK);
  try {
    if (!fstatSync(descriptor).isFile()) throw new Error("not a regular file");
    const chunks: Buffer[] = [];
    let size = 0;
    for (;;) {
      const chunk = Buffer.allocUnsafe(chunkBytes);
      const read = readSync(descriptor, chunk, 0, chunkBytes, null);
      if (read === 0) return Buffer.concat(chunks, size);
      size += read;
      if (size > fileBytes) return undefined;
      chunks.push(chunk.subarray(0, read));
    }
  } finally {
    closeSync(descriptor);
  }
}

// RFC 5987 does not allow the ' ( ) * that encodeURIComponent leaves, and a
// lone surrogate would make encodeURIComponent throw.
function contentDisposition(filename: string): string {
  const encoded = encodeURIComponent(filename.replace(/\p{Cs}/gu, "\uFFFD")).replace(
    /['()*]/gu,
    (char) => `%${char.charCodeAt(0).toString(16).toUpperCase()}`,
  );
  return `attachment; filename*=UTF-8''${encoded}`;
}

function errorReason(body: string): string | undefined {
  try {
    const reason: unknown = JSON.parse(body)?.details?.reason;
    return typeof reason === "string" ? reason : undefined;
  } catch {
    return undefined;
  }
}

function stepFor(status: number, reason?: string): Step {
  if (status >= 200 && status < 300) return "stored";
  if (reason === uploadsOffReason) return "pause";
  if (stopStatus.has(status)) return "stop";
  if (refusedStatus.has(status)) return "refused";
  return retryableStatus.has(status) ? "retry" : "fail";
}

function seconds(value: string | undefined): number | undefined {
  const parsed = value?.trim() ? Number(value) : Number.NaN;
  return Number.isFinite(parsed) ? Math.max(0, parsed) : undefined;
}

// Redirects are not followed, which keeps the Bearer key off a redirect target.
// Sockets are unref'd so a pending upload never keeps the process alive.
function send(
  url: URL,
  method: "HEAD" | "PUT",
  headers: OutgoingHttpHeaders,
  body?: Buffer,
): Promise<Answer> {
  return new Promise((resolve, reject) => {
    const request = (url.protocol === "https:" ? httpsRequest : httpRequest)(
      url,
      { method, headers, timeout: requestTimeoutMs },
      (response) => {
        let body = "";
        response.setEncoding("utf8");
        response.on("data", (chunk: string) => {
          if (body.length < errorBodyBytes) body += chunk;
        });
        response.on("end", () =>
          resolve({ status: response.statusCode ?? 0, headers: response.headers, body }),
        );
        response.on("close", () => reject(new Error("Convergent upload response closed early.")));
        response.on("error", reject);
      },
    );
    request.on("socket", (socket) => socket.unref());
    request.on("timeout", () => request.destroy(new Error("Convergent upload timed out.")));
    request.on("error", reject);
    request.end(body);
  });
}
