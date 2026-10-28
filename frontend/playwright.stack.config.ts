import { defineConfig, devices } from "@playwright/test";

// Browser tests against a running REAL stack (scripts/stack-e2e.sh starts one): Caddy over HTTPS,
// the web app, the API and a development Keycloak. Not part of `pnpm test:e2e`, which runs against
// a mocked API.
const base = process.env.STACK_URL;
if (!base) {
  throw new Error("STACK_URL is not set; run `make test-stack-e2e`");
}

export default defineConfig({
  testDir: "./e2e-stack",
  // The tests share one database and one identity provider, and some change them.
  fullyParallel: false,
  workers: 1,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [["github"], ["list"]] : "list",
  timeout: 60_000,
  use: {
    baseURL: base,
    // Caddy signs with its own authority in this stack.
    ignoreHTTPSErrors: true,
    trace: "retain-on-failure",
  },
  projects: [{ name: "desktop", use: { ...devices["Desktop Chrome"] } }],
});
