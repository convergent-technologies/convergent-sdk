import { defineConfig } from "oxlint";

export default defineConfig({
  plugins: ["typescript"],
  options: { typeAware: true },
  env: { node: true, es2022: true },
  categories: { correctness: "error" },
  rules: {
    "no-nested-ternary": "error",
    "typescript/no-explicit-any": "error",
    "typescript/no-floating-promises": "error",
    "typescript/no-misused-promises": "error",
  },
});
