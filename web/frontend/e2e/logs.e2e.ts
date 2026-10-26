import { expect, test } from "@playwright/test";
import { dialog, toast, unique, watchErrors } from "./helpers";

// Retained logs against the real server: a workload that keeps its logs is
// listed in Logs → History, searching works (nothing stored without Docker),
// and an operator can change and stop its retention.
test("logs history lists kept workloads and manages their retention", async ({ page }) => {
  const errors = watchErrors(page);
  const name = unique("web");
  await page.goto("/logs");
  const status = await page.evaluate(async (workload) => {
    const r = await fetch(`/api/v1/log-archive/retention/${workload}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${localStorage.getItem("kowan_token")}` },
      body: JSON.stringify({ days: 7 }),
    });
    return r.status;
  }, `ctr~local~${name}`);
  expect(status).toBe(200);

  await page.getByRole("tab", { name: "History" }).click();
  await expect(page).toHaveURL(/mode=history/);
  const row = page.getByRole("checkbox", { name: `Filter by ${name}` });
  await expect(row).toBeVisible();
  await expect(page.getByText(/7 days · 0 lines/)).toBeVisible();
  await page.getByLabel("Search logs").fill("error -health");
  await page.getByRole("button", { name: "Search", exact: true }).click();
  await expect(page.getByText("No matching lines")).toBeVisible();
  await row.click();
  await expect(row).toHaveAttribute("aria-checked", "true");

  await page.getByRole("button", { name: `Retention of ${name}` }).click();
  await page.getByRole("menuitem", { name: "30 days" }).click();
  await toast(page, "Retention changed");
  await expect(page.getByText(/30 days · 0 lines/)).toBeVisible();
  await page.screenshot({ path: "test-results/logs-history.png", fullPage: true });

  await page.getByRole("button", { name: `Retention of ${name}` }).click();
  await page.getByRole("menuitem", { name: "Stop keeping…" }).click();
  await dialog(page, "Stop keeping logs?").getByRole("button", { name: "Stop and delete" }).click();
  await toast(page, "Logs no longer kept");
  await expect(page.getByRole("checkbox", { name: `Filter by ${name}` })).toHaveCount(0);
  expect(errors).toEqual([]);
});
