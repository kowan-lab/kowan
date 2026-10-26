import { expect, test } from "@playwright/test";
import { dialog, toast, unique, watchErrors } from "./helpers";

// Deployment history against the real server, without touching Docker: a
// deployment is created stopped, edited, then rolled back from the History tab.
test("edits become revisions that diff and roll back", async ({ page }) => {
  const errors = watchErrors(page);
  const name = unique("hist");
  await page.goto("/deployments/new");
  await page.getByLabel(/^name/i).first().fill(name);
  await page.getByLabel(/^image/i).first().fill("nginx:1.27");
  await page.getByLabel("Deploy right after creating").uncheck();
  await page.getByRole("button", { name: /^create$/i }).last().click();
  await expect(page).toHaveURL(/\/workloads\/dep~/);

  await page.getByRole("tab", { name: "Configuration" }).click();
  const image = page.getByLabel(/^image/i).first();
  await image.fill("nginx:1.28");
  await page.getByRole("button", { name: /^save$/i }).click();
  await expect(page.getByText(/^saved/i).first()).toBeVisible();

  await page.getByRole("tab", { name: "History" }).click();
  const list = page.getByRole("list", { name: "Revisions" });
  await expect(list.getByText("Edited")).toBeVisible();
  await expect(list.getByText("Created")).toBeVisible();
  await expect(page.getByText("image: nginx:1.28")).toBeVisible();
  await page.screenshot({ path: "test-results/history.png", fullPage: true });

  await list.getByText("#1").click();
  await page.getByRole("button", { name: /roll back to #1/i }).click();
  const d = dialog(page, /roll back to #1/i);
  await d.getByLabel("Deploy it now").uncheck();
  await d.getByRole("button", { name: /^roll back$/i }).click();
  await toast(page, /rolled back to #1/i);
  await expect(list.getByText("#3")).toBeVisible();
  await expect(list.getByText("rollback to #1")).toBeVisible();
  expect(errors).toEqual([]);
});
