import { defineConfig, devices } from "@playwright/test";

const PORT = 3100;

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [["github"], ["list"]] : "list",
  use: {
    baseURL: `http://127.0.0.1:${PORT}`,
    trace: "retain-on-failure",
  },
  projects: [
    { name: "desktop", use: { ...devices["Desktop Chrome"] } },
    { name: "mobile", use: { ...devices["Pixel 5"] } },
  ],
  webServer: {
    // The production build is what users get, so the end-to-end tests run against it.
    // This installation's default language is Azerbaijani, to prove it can differ from "en".
    command: "pnpm build && pnpm start",
    url: `http://127.0.0.1:${PORT}/healthz`,
    env: { PORT: String(PORT), HOSTNAME: "127.0.0.1", PHAROS_DEFAULT_LOCALE: "az" },
    reuseExistingServer: !process.env.CI,
    timeout: 240_000,
  },
});
