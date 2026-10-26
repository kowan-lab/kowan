import { expect, test } from "@playwright/test";
import { dialog, toast, unique, watchErrors } from "./helpers";

// Preview environments against the real server: turn them on for a git
// deployment, start one by hand, see it listed and labelled, and remove it.
// The repository is on the reserved .invalid domain, so deploys fail fast
// without reaching any real host; the preview records still behave.
test("previews are set up, created and removed from a git deployment", async ({ page }) => {
  const errors = watchErrors(page);
  const name = unique("prev");
  await page.goto("/workloads");
  const dep = await page.evaluate(async (n) => {
    const r = await fetch("/api/v1/deployments", {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${localStorage.getItem("kowan_token")}` },
      body: JSON.stringify({ name: n, type: "compose", strategy: "standalone", targets: ["local"],
                             git: { repoUrl: "https://git.invalid/acme/shop.git", ref: "main" } }),
    });
    return r.json();
  }, name);
  await page.goto(`/workloads/dep~${dep.id}`);
  const band = page.locator("section").filter({ has: page.getByRole("heading", { name: "Preview environments" }) });
  await band.getByRole("button", { name: "Set up previews" }).click();
  const settings = dialog(page, "Preview environments");
  await settings.getByLabel("Branches").fill("feature/*");
  await settings.getByRole("button", { name: "Save" }).click();
  await toast(page, "Previews on");
  await expect(band.getByText("0 of 5")).toBeVisible();

  await band.getByRole("button", { name: "New preview" }).click();
  const create = dialog(page, "New preview");
  await create.getByLabel("Branch").fill("feature/cart");
  await create.getByRole("button", { name: "Create preview" }).click();
  await toast(page, "Preview deploying");
  const link = band.getByRole("link", { name: `${name}-feature-cart` });
  await expect(link).toBeVisible();
  await page.screenshot({ path: "test-results/previews.png", fullPage: true });

  await link.click();
  await expect(page.getByText("Preview of feature/cart")).toBeVisible();
  await page.getByRole("link", { name, exact: true }).first().click();
  await band.getByRole("button", { name: `Remove ${name}-feature-cart` }).click();
  await dialog(page, "Remove preview?").getByRole("button", { name: "Remove" }).click();
  await toast(page, "Preview removed");
  await expect(band.getByText("No previews running.")).toBeVisible();
  expect(errors).toEqual([]);
});
