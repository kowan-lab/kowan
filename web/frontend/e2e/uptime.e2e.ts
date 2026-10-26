import { expect, test } from "@playwright/test";
import { PORT } from "../playwright.config";
import { dialog, toast, unique, watchErrors } from "./helpers";

// Uptime against the real server: a check probes the server itself, shows up,
// and is listed on the public status page, which opens without signing in.
test("an HTTP check goes up and appears on the public status page", async ({ page, browser }) => {
  const errors = watchErrors(page);
  const name = unique("self");
  await page.goto("/uptime");
  await page.getByRole("button", { name: "New check" }).first().click();
  const d = dialog(page, "New uptime check");
  await d.getByLabel("Name").fill(name);
  await d.getByLabel("URL").fill(`http://127.0.0.1:${PORT}/api/v1/auth/status`);
  await d.getByLabel("Page contains").fill("initialized");
  await d.getByRole("switch", { name: "Show on the status page" }).check({ force: true });
  await d.getByRole("button", { name: "Create check" }).click();
  await toast(page, "Check created");
  const row = page.getByRole("row").filter({ hasText: name });
  await row.getByRole("button", { name: "Check now" }).click();
  await toast(page, `${name} is up`);
  await expect(row.getByText("Up", { exact: true })).toBeVisible();

  await page.getByRole("switch", { name: "Public status page" }).check({ force: true });
  await page.getByLabel("Title").fill("E2E status");
  await page.getByRole("button", { name: "Save" }).last().click();
  await toast(page, "Status page saved");
  await page.screenshot({ path: "test-results/uptime.png", fullPage: true });

  const anon = await browser.newContext({ storageState: { cookies: [], origins: [] } });
  const pub = await anon.newPage();
  await pub.goto("/status");
  await expect(pub.getByRole("heading", { name: "E2E status" })).toBeVisible();
  await expect(pub.getByText(name)).toBeVisible();
  await expect(pub.getByText("All systems operational")).toBeVisible();
  await pub.screenshot({ path: "test-results/status-page.png", fullPage: true });
  await anon.close();
  expect(errors).toEqual([]);
});
