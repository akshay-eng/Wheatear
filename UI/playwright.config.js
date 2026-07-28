import { defineConfig, devices } from "@playwright/test";

const externalBaseUrl = process.env.WHEATEAR_E2E_URL;

export default defineConfig({
  testDir: "./e2e",
  timeout: 30_000,
  webServer: externalBaseUrl
    ? undefined
    : {
        command: "npm run dev -- --host 127.0.0.1 --port 4173 --strictPort",
        url: "http://127.0.0.1:4173",
        reuseExistingServer: true,
      },
  use: {
    baseURL: externalBaseUrl || "http://127.0.0.1:4173",
    trace: "retain-on-failure",
  },
  projects: [
    {
      name: "desktop",
      use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 960 } },
    },
    {
      name: "mobile",
      use: { ...devices["Pixel 7"] },
    },
  ],
});
