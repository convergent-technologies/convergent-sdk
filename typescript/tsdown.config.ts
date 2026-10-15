import { readFileSync } from "node:fs";
import { defineConfig } from "tsdown";

const { version } = JSON.parse(
  readFileSync(new URL("./package.json", import.meta.url), "utf8"),
) as { version: string };

export default defineConfig({
  entry: ["src/index.ts"],
  format: ["esm"],
  platform: "node",
  target: "node22",
  dts: true,
  fixedExtension: false,
  clean: true,
  // Uploads name the SDK and its version, so the server can tell clients apart.
  define: { __CONVERGENT_SDK_VERSION__: JSON.stringify(version) },
});
