import { disabledStatus, type ResolvedOptions, type Status } from "./configuration.js";

type FailureReason = "invalid_config" | "existing_provider" | "setup_failed";

// The last setup that did not start, and what it resolved, for check().
let last: { reason: FailureReason; resolved: ResolvedOptions | undefined } | undefined;

/** Record a setup that did not start and return its status. */
export function setupFailure(reason: FailureReason, resolved?: ResolvedOptions): Status {
  last = { reason, resolved };
  return disabledStatus(reason);
}

export function lastSetupFailure(): typeof last {
  return last;
}
