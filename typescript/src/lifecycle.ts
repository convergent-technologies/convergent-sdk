import { trace } from "@opentelemetry/api";
import { activeConfiguration } from "./configuration.js";
import { reportOnce } from "./diagnostics.js";
import type { ConvergentProcessor } from "./processor.js";

export interface FlushOptions {
  timeoutMs?: number;
  uploads?: boolean;
  uploadTimeoutMs?: number;
}

export interface FlushResult {
  ok: boolean;
  elapsedMs: number;
}

function budget(value: number | undefined, fallback: number, name: string): number {
  if (value === undefined) return fallback;
  if (Number.isFinite(value) && value >= 0) return value;
  reportOnce(
    `invalid_flush_${name}`,
    `Convergent flush ${name} must be a finite nonnegative number; using ${fallback}.`,
  );
  return fallback;
}

/** Resolves false when `work` rejects or outlasts `ms`, logging `failed` or `timedOut` once. */
async function within(
  work: () => Promise<unknown>,
  ms: number,
  failed: [string, string],
  timedOut: [string, string],
): Promise<boolean> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  const ok = await Promise.race([
    Promise.resolve()
      .then(work)
      .then(
        () => true,
        () => {
          reportOnce(...failed);
          return false;
        },
      ),
    new Promise<false>((resolve) => {
      timer = setTimeout(() => {
        reportOnce(...timedOut);
        resolve(false);
      }, ms);
    }),
  ]);
  clearTimeout(timer);
  return ok;
}

/**
 * `ok` means Convergent's drain methods resolved within the budget. It is not delivery
 * evidence: an exporter can resolve its drain after an export failed.
 */
export async function flush(options: FlushOptions = {}): Promise<FlushResult> {
  const started = performance.now();
  const configuration = activeConfiguration();
  if (!configuration) return { ok: true, elapsedMs: 0 };
  // options can be null from JavaScript callers.
  const timeoutMs = budget(options?.timeoutMs, 5000, "timeoutMs");
  const uploadTimeoutMs = budget(options?.uploadTimeoutMs, 60_000, "uploadTimeoutMs");
  if (trace.getActiveSpan()?.isRecording()) {
    reportOnce(
      "flush_inside_span",
      "Convergent flushed inside a traced function, so the enclosing span has not ended and is not in this flush. If you expected this run's own span in it, flush after the function that observe() or span() wraps returns.",
    );
  }
  let ok = await within(
    () => configuration.processor.forceFlush(),
    timeoutMs,
    ["flush_failed", "Convergent could not flush completed spans."],
    ["flush_timeout", "Convergent could not finish flushing before the timeout."],
  );
  if (options?.uploads === true) {
    const uploaded = await within(
      () => configuration.processor.artifacts.drained(),
      uploadTimeoutMs,
      ["upload_failed", "Convergent could not finish uploading files."],
      ["upload_timeout", "Convergent could not finish uploading files before uploadTimeoutMs."],
    );
    ok = ok && uploaded;
  }
  return { ok, elapsedMs: Math.trunc(performance.now() - started) };
}

/**
 * The exit drain: flush, then shut down Convergent's processor, which discards queued
 * uploads without waiting. Best effort for a script that runs out of work. Not called by
 * process.exit() or a crash. Called once, by whichever setup claims the process.
 */
export function drainBeforeExit(processor: ConvergentProcessor): void {
  process.once("beforeExit", () => {
    void flush()
      .then(() => processor.shutdown())
      .catch(() => {});
  });
}
