import { expect, test } from "@playwright/test";
import { unique } from "./helpers";

// Touches the local Docker Engine: pulls alpine, runs a throwaway
// "kowan-e2e-*" container and removes it again. Opt in with KOWAN_E2E_DOCKER=1.
test.skip(process.env.KOWAN_E2E_DOCKER !== "1", "set KOWAN_E2E_DOCKER=1 to deploy a real container");

test("deploys a container, stops it and deletes the deployment", async ({ page }) => {
  test.setTimeout(180_000);
  const name = unique("kowan-e2e");
  await page.goto("/deployments/new");
  await page.getByLabel(/^name/i).first().fill(name);
  await page.getByLabel(/^image/i).first().fill("alpine:3.22");
  await page.getByLabel("Command", { exact: true }).fill("sleep 600");
  await page.getByRole("button", { name: /^create|^deploy$/i }).last().click();
  await expect(page).toHaveURL(/\/workloads\/dep~/);
  await expect(page.getByText(/running/i).first()).toBeVisible({ timeout: 120_000 });
  // The overview has the resource history panel (empty until the monitor's
  // first 5-minute bucket closes) and the logs tab the "Keep logs" switch.
  await expect(page.getByRole("heading", { name: "Resources" })).toBeVisible();
  await page.getByRole("tab", { name: /^logs$/i }).click();
  await expect(page.getByTestId("log-retention")).toBeVisible();
  // The Overview tab carries the container count ("Overview 1").
  await page.getByRole("tab", { name: /^overview\b/i }).click();

  await page.getByRole("button", { name: /^stop$/i }).click();
  await expect(page.getByText(/stopped|exited/i).first()).toBeVisible({ timeout: 60_000 });

  await page.getByRole("button", { name: /^more$/i }).click();
  await page.getByRole("menuitem", { name: /remove containers/i }).click();
  await page.getByRole("dialog").getByRole("button").last().click();
  await page.getByRole("button", { name: /^more$/i }).click();
  await page.getByRole("menuitem", { name: /delete deployment/i }).click();
  const confirm = page.getByRole("dialog").getByRole("textbox");
  if (await confirm.count()) await confirm.fill(name);
  await page.getByRole("dialog").getByRole("button").last().click();
  await expect(page).toHaveURL(/\/workloads$/);
});
