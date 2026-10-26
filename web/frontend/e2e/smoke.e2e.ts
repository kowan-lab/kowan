import { expect, test } from "@playwright/test";
import { heading, watchErrors } from "./helpers";

// Every page of the console renders against the real API without errors.
const pages: [string, string | RegExp][] = [
  ["/", /overview|local/i],
  ["/workloads", /workloads/i],
  ["/hosts", "Hosts"],
  ["/images", /images/i],
  ["/volumes", /volumes/i],
  ["/networks", /networks/i],
  ["/networks/map", /network map/i],
  ["/exposure", "Exposure"],
  ["/templates", "Templates"],
  ["/deployments/new", /new deployment|deploy/i],
  ["/workflows", /workflows/i],
  ["/gitops", "GitOps"],
  ["/backups", "Backups"],
  ["/schedules", "Schedules"],
  ["/auto-update", "Auto-update"],
  ["/logs", "Logs"],
  ["/activity", "Activity"],
  ["/monitoring", "Monitoring"],
  ["/audit", "Audit log"],
  ["/credentials", "Credentials"],
  ["/registries", "Registries"],
  ["/registry-explorer", "Registry Explorer"],
  ["/secrets", "Secrets"],
  ["/channels", "Notifications"],
  ["/users", "Users"],
  ["/teams", "Teams"],
  ["/api-keys", /api keys/i],
  ["/sso", "Single sign-on"],
  ["/ai", /ai assistants/i],
  ["/settings", "Settings"],
  ["/settings/network", "Network access"],
  ["/volume-explorer", "Volume Explorer"],
  ["/uptime", "Uptime"],
  ["/import", "Import stacks"],
  // Read-only here: it lists what could go on the local Docker, never removes.
  ["/hosts/local/cleanup", "Disk cleanup"],
];

for (const [path, title] of pages) {
  test(`renders ${path}`, async ({ page }) => {
    const errors = watchErrors(page);
    await page.goto(path);
    await heading(page, title);
    await expect(page.getByText("Something went wrong on this page")).toHaveCount(0);
    // Let the first polls land before checking for errors.
    await page.waitForLoadState("networkidle").catch(() => undefined);
    expect(errors).toEqual([]);
  });
}

test("unknown routes show the not-found page", async ({ page }) => {
  await page.goto("/does-not-exist");
  await expect(page.getByText(/not found|doesn't exist|does not exist/i).first()).toBeVisible();
});
