import { expect, test } from "@playwright/test";
import { SETUP_TOKEN } from "../playwright.config";
import { ADMIN, watchErrors } from "./helpers";

// First run on an empty server: the login page sends to setup, the admin is
// created with the setup token and the session is saved for the other specs.
test("first-run setup creates the administrator", async ({ page }) => {
  const errors = watchErrors(page);
  await page.goto("/");
  await expect(page).toHaveURL(/\/setup$/);
  await page.getByLabel("Setup token").fill("wrong-token");
  await page.getByLabel("Username").fill(ADMIN.username);
  await page.getByLabel("Password", { exact: true }).fill(ADMIN.password);
  await page.getByLabel("Confirm password").fill(ADMIN.password);
  await page.getByRole("button", { name: "Create administrator" }).click();
  await expect(page.getByText("invalid setup token")).toBeVisible();
  await page.getByLabel("Setup token").fill(SETUP_TOKEN);
  await page.getByRole("button", { name: "Create administrator" }).click();
  await expect(page).toHaveURL(/\/$/);
  await expect(page.getByText(ADMIN.username).first()).toBeVisible();
  await page.context().storageState({ path: "e2e/.data/admin.json" });
  expect(errors).toEqual([]);
});
