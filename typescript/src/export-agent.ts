import { convertLegacyHttpOptions } from "@opentelemetry/otlp-exporter-base/node-http";

// The managed ingest ALB retires idle connections after 60 seconds.
const IDLE_REFRESH_MS = 55_000;

// OpenTelemetry gives the first attempt the whole export deadline, so a pooled socket
// silently dropped by the load balancer can consume it and lose the batch.
export function createExportAgent() {
  const envAgentFactory = convertLegacyHttpOptions({}, "TRACES", "v1/traces", {}).agentFactory;
  let created: Promise<Awaited<ReturnType<typeof envAgentFactory>>> | undefined;
  let agent: Awaited<ReturnType<typeof envAgentFactory>> | undefined;
  const idleSince = new WeakMap<object, number>();

  return {
    factory: (protocol: string) =>
      (created ??= Promise.resolve(envAgentFactory(protocol)).then((resolved) => {
        const keepSocketAlive = resolved.keepSocketAlive.bind(resolved);
        resolved.keepSocketAlive = function (socket) {
          idleSince.set(socket, performance.now());
          return keepSocketAlive(socket);
        };
        agent = resolved;
        return resolved;
      })),
    refreshIdle(): void {
      if (!agent) return;
      const now = performance.now();
      // Node reuses the most recently freed socket and skips destroyed ones at the
      // front of the free list, so removing every socket idle >= 55 s prevents stale reuse.
      for (const sockets of Object.values(agent.freeSockets)) {
        for (const socket of sockets ?? []) {
          const since = idleSince.get(socket);
          if (since !== undefined && now - since >= IDLE_REFRESH_MS) socket.destroy();
        }
      }
    },
  };
}
