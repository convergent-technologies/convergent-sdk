import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { createHash, randomUUID } from "node:crypto";
import { writeFileSync } from "node:fs";
import { copyFile, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { createServer } from "node:http";
import { createRequire } from "node:module";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { gunzipSync, zstdDecompressSync } from "node:zlib";

const script = fileURLToPath(import.meta.url);
const order = process.argv[2];

if (!order) {
  const packageDir = dirname(dirname(script));
  const manifest = JSON.parse(await readFile(join(packageDir, "package.json"), "utf8"));
  const consumer = await mkdtemp(join(tmpdir(), "convergent-sdk-consumer-"));
  const tarball = join(consumer, "convergent-sdk.tgz");
  try {
    execFileSync("pnpm", ["--ignore-workspace", "pack", "--out", tarball], {
      cwd: packageDir,
      stdio: "inherit",
    });
    await writeFile(
      join(consumer, "package.json"),
      JSON.stringify({
        private: true,
        type: "module",
        dependencies: {
          "convergent-sdk": `file:${tarball}`,
          "@opentelemetry/api": manifest.devDependencies["@opentelemetry/api"],
        },
      }),
    );
    execFileSync("pnpm", ["--ignore-workspace", "install", "--ignore-scripts"], {
      cwd: consumer,
      stdio: "inherit",
    });
    const consumerScript = join(consumer, "consumer.mjs");
    await copyFile(script, consumerScript);
    const env = Object.fromEntries(
      Object.entries(process.env).filter(([key]) => !/^(OTEL_|CONVERGENT_)/u.test(key)),
    );
    for (const loadOrder of ["import-first", "require-first"]) {
      execFileSync(process.execPath, [consumerScript, loadOrder], { env, stdio: "inherit" });
    }
  } finally {
    await rm(consumer, { recursive: true, force: true });
  }
} else {
  const require = createRequire(import.meta.url);
  const { trace } = require("@opentelemetry/api");
  const first =
    order === "import-first" ? await import("convergent-sdk") : require("convergent-sdk");
  const second =
    order === "import-first" ? require("convergent-sdk") : await import("convergent-sdk");
  const bodies = [];
  const puts = [];
  const held = new Map();
  let putStatus = 201;
  const server = createServer(async (request, response) => {
    const chunks = [];
    for await (const chunk of request) chunks.push(chunk);
    const raw = Buffer.concat(chunks);
    const encoding = request.headers["content-encoding"];
    const decoders = { gzip: gunzipSync, zstd: zstdDecompressSync };
    const body = encoding ? decoders[encoding](raw) : raw;
    const digest = request.url.replace("/v1/artifacts/", "");
    if (request.method === "HEAD") {
      const size = held.get(digest);
      response.writeHead(
        size === undefined ? 404 : 200,
        size === undefined ? {} : { "content-length": size },
      );
    } else if (request.method === "PUT") {
      // A zstd frame declares its size when the header's size field or single-segment bit is set.
      const declaresSize = encoding !== "zstd" || (raw[4] & 0xe0) !== 0;
      puts.push({ digest, headers: request.headers, body: body.toString(), declaresSize });
      if (putStatus === 201) held.set(digest, body.length);
      response.writeHead(putStatus);
    } else {
      bodies.push(body);
      response.writeHead(200, { "content-type": "application/x-protobuf" });
    }
    response.end();
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  try {
    const apiKey = randomUUID();
    const configured = first.init({
      apiKey,
      endpoint: `http://127.0.0.1:${server.address().port}`,
      release: "packed-consumer",
    });
    assert.equal(configured.enabled, true);
    assert.deepEqual(second.init({ release: "ignored" }), {
      ...configured,
      reason: "already_configured",
    });
    assert.equal(first.tracerProvider(), second.tracerProvider());
    const ids = await first.span({ name: "package-root", operation: "agent_run" }, async (root) => {
      await Promise.resolve();
      return second.span({ name: "package-child", operation: "tool_call" }, (child) => {
        assert.match(root.traceId, /^[0-9a-f]{32}$/u);
        assert.equal(child.traceId, root.traceId);
        assert.equal(trace.getActiveSpan().parentSpanContext.spanId, root.spanId);
        assert.match(child.spanId, /^[0-9a-f]{16}$/u);
        assert.notEqual(child.spanId, root.spanId);
        return { traceId: root.traceId, parentSpanId: root.spanId, childSpanId: child.spanId };
      });
    });
    assert.equal((await second.flush()).ok, true);
    const exported = Buffer.concat(bodies);
    for (const value of [
      "invoke_agent package-root",
      "execute_tool package-child",
      "packed-consumer",
    ]) {
      assert.ok(exported.includes(Buffer.from(value)), `Export must contain ${value}`);
    }
    for (const value of Object.values(ids)) {
      assert.ok(
        exported.includes(Buffer.from(value, "hex")),
        `Export must contain span ID ${value}`,
      );
    }

    const digest = (bytes) => `sha256:${createHash("sha256").update(bytes).digest("hex")}`;
    const csv = Buffer.from("invoice,total\n1042,20\n");
    const csvName = "invoices (Q3) é.csv";
    const csvPath = join(dirname(script), csvName);
    await writeFile(csvPath, csv);
    const png = Buffer.from("\x89PNG\r\n\x1a\n chart", "latin1");
    const pngSource = Uint8Array.from(png);
    const earlier = Buffer.from("uploaded by an earlier process");
    held.set(digest(earlier), earlier.length);
    const filesTraceId = await first.span(
      { name: "files", operation: "agent_run" },
      async (run) => {
        second.span({ name: "load", operation: "tool_call" }, (call) => {
          call.inputArtifact(csvPath);
          call.outputArtifact(pngSource, {
            filename: "chart.png",
            mediaType: "image/png",
            origin: "tool",
          });
          pngSource.fill(0);
          writeFileSync(csvPath, "rewritten after the call");
        });
        second.span({ name: "reread", operation: "tool_call" }, (call) => {
          call.inputArtifact(csv, { filename: csvName });
          call.inputArtifact(earlier, { mediaType: "text/plain", origin: "subagent" });
          call.inputArtifact(csv, { filename: "é".repeat(1025) });
          call.inputArtifact(csv, { mediaType: `text/${"x".repeat(300)}` });
        });
        return run.traceId;
      },
    );
    assert.equal((await second.flush({ uploads: true })).ok, true);
    assert.deepEqual(
      puts.map((put) => [
        put.digest,
        put.headers["content-type"],
        put.headers["content-encoding"],
        put.headers["content-disposition"],
        put.body,
      ]),
      [
        [
          digest(csv),
          "application/octet-stream",
          "zstd",
          "attachment; filename*=UTF-8''invoices%20%28Q3%29%20%C3%A9.csv",
          csv.toString(),
        ],
        [
          digest(png),
          "image/png",
          undefined,
          "attachment; filename*=UTF-8''chart.png",
          png.toString(),
        ],
      ],
    );
    for (const put of puts) {
      assert.equal(put.declaresSize, true, "the receiver refuses zstd without a declared size");
      assert.equal(put.headers.authorization, `Bearer ${apiKey}`);
      assert.equal(put.headers["x-convergent-trace-id"], filesTraceId);
    }

    const notes = Buffer.from("# notes");
    putStatus = 422;
    second.span({ name: "markdown", operation: "tool_call" }, (call) =>
      call.inputArtifact(notes, { mediaType: "text/markdown" }),
    );
    await second.flush({ uploads: true });
    putStatus = 201;
    second.span({ name: "retyped", operation: "tool_call" }, (call) =>
      call.inputArtifact(notes, { mediaType: "text/plain" }),
    );
    await second.flush({ uploads: true });
    assert.deepEqual(
      puts.slice(2).map((put) => [put.digest, put.headers["content-type"]]),
      [[digest(notes), "text/markdown"]],
    );

    putStatus = 404;
    const refused = Buffer.from("first file after uploads were turned off");
    const unlisted = Buffer.from("a file after the 404");
    second.span({ name: "refused", operation: "tool_call" }, (call) => call.inputArtifact(refused));
    await second.flush({ uploads: true });
    second.span({ name: "after", operation: "tool_call" }, (call) => call.inputArtifact(unlisted));
    await second.flush({ uploads: true });
    assert.deepEqual(puts.map((put) => put.digest).slice(3), [digest(refused)]);

    const csvListing = `{"digest":"${digest(csv)}","size_bytes":${csv.length},"media_type":"application/octet-stream","filename":"${csvName}"}`;
    const exportedFiles = Buffer.concat(bodies);
    for (const listing of [
      `[${csvListing}]`,
      `[{"digest":"${digest(png)}","size_bytes":${png.length},"media_type":"image/png","filename":"chart.png","origin":"tool"}]`,
      `[${csvListing},{"digest":"${digest(earlier)}","size_bytes":${earlier.length},"media_type":"text/plain"},{"digest":"${digest(csv)}","size_bytes":${csv.length},"media_type":"application/octet-stream"}]`,
      digest(refused),
    ]) {
      assert.ok(exportedFiles.includes(Buffer.from(listing)), `Export must contain ${listing}`);
    }
    assert.ok(
      !exportedFiles.includes(Buffer.from(digest(unlisted))),
      "A span after a 404 lists no file",
    );
    console.log(
      JSON.stringify({ ok: true, node: process.version, order, ...ids, uploads: puts.length }),
    );
  } finally {
    try {
      await first.tracerProvider().shutdown();
    } finally {
      await new Promise((resolve) => server.close(resolve));
    }
  }
}
