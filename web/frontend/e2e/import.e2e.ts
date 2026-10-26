import { mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { expect, test } from "@playwright/test";
import { toast, unique, watchErrors } from "./helpers";

// Importing a folder of compose stacks against the real server: the browser
// reads the folder, the server groups the stacks and applies .env, and the
// picked one becomes a deployment (created, not deployed).
test("a compose folder is imported as deployments", async ({ page }) => {
  const errors = watchErrors(page);
  const name = unique("imp");
  const root = mkdtempSync(join(tmpdir(), "kowan-import-"));
  const dir = join(root, "stacks", name);
  mkdirSync(dir, { recursive: true });
  writeFileSync(join(dir, "compose.yml"), "services:\n  web:\n    image: nginx:${TAG}\n");
  writeFileSync(join(dir, ".env"), "TAG=1.27\n");
  writeFileSync(join(dir, "README.md"), "not read");

  await page.goto("/workloads");
  await page.getByRole("button", { name: /new workload/i }).click();
  await page.getByRole("menuitem", { name: /import stacks/i }).click();
  await expect(page).toHaveURL(/\/import$/);
  await page.getByRole("tab", { name: "From compose folders" }).click();
  await page.getByLabel("Choose folder").setInputFiles(join(root, "stacks"));
  await expect(page.getByText("2 files read.")).toBeVisible();
  await expect(page.getByLabel(`Name of ${name}`)).toHaveValue(name);
  await page.screenshot({ path: "test-results/import.png", fullPage: true });
  await page.getByRole("button", { name: "Import 1" }).click();
  await toast(page, "Imported 1 of 1");
  await page.getByRole("link", { name: `Imported: open ${name}` }).click();
  await expect(page).toHaveURL(/\/workloads\/dep~/);
  const id = decodeURIComponent(page.url().split("dep~")[1]);
  const compose = await page.evaluate(async (depId) => {
    const r = await fetch(`/api/v1/deployments/${depId}`, { headers: { Authorization: `Bearer ${localStorage.getItem("kowan_token")}` } });
    return (await r.json()).compose as string;
  }, id);
  expect(compose).toContain("image: nginx:1.27"); // .env applied
  expect(errors).toEqual([]);
});
