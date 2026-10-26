import { defineConfig, devices } from "@playwright/test";
import { fileURLToPath } from "node:url";
import path from "node:path";

// End-to-end tests against a real Kowan server: a fresh data dir, a fixed
// first-run setup token and the SPA built into web/dist (`make e2e` builds it).
// The server talks to the local Docker Engine; the specs only read from it
// unless KOWAN_E2E_DOCKER=1, which also deploys and removes a throwaway
// "kowan-e2e-*" container.
const here = path.dirname(fileURLToPath(import.meta.url));
const repo = path.resolve(here, "../..");
const dataDir = path.join(here, "e2e/.data");
export const PORT = Number(process.env.KOWAN_E2E_PORT ?? 18777);
export const SETUP_TOKEN = "e2e-setup-token";

export default defineConfig({
  testDir: "e2e",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 60_000,
  expect: { timeout: 10_000 },
  reporter: [["list"], ["html", { open: "never" }]],
  use: {
    baseURL: `http://127.0.0.1:${PORT}`,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [
    { name: "setup", testMatch: /setup\.e2e\.ts/ },
    {
      name: "chromium",
      testMatch: /.*\.e2e\.ts/,
      testIgnore: /setup\.e2e\.ts/,
      dependencies: ["setup"],
      use: { ...devices["Desktop Chrome"], storageState: "e2e/.data/admin.json" },
    },
  ],
  webServer: {
    command: `rm -rf "${dataDir}" && mkdir -p "${dataDir}" && exec "${repo}/.venv/bin/python" -m kowan.server`,
    cwd: repo,
    url: `http://127.0.0.1:${PORT}/api/v1/auth/status`,
    reuseExistingServer: false,
    timeout: 60_000,
    env: {
      KOWAN_ADDR: `127.0.0.1:${PORT}`,
      KOWAN_DATA_DIR: dataDir,
      KOWAN_SETUP_TOKEN: SETUP_TOKEN,
      KOWAN_MONITOR_INTERVAL: "5s",
    },
    stdout: "ignore",
    // The server logs every request at INFO; e2e/.data/server.log is not written,
    // so keep the runner output readable.
    stderr: "ignore",
  },
});
