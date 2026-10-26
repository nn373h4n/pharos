import path from "node:path";
import { defineConfig } from "vitest/config";

export default defineConfig({
  resolve: {
    alias: { "@": path.resolve(import.meta.dirname, "src") },
  },
  test: {
    environment: "jsdom",
    include: ["src/**/*.test.{ts,tsx}", "scripts/**/*.test.mjs"],
    // Playwright specs live in e2e/ and run with `pnpm test:e2e`.
    exclude: ["e2e/**", "node_modules/**", ".next/**"],
  },
});
