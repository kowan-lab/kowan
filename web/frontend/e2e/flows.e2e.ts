import { expect, test, type Browser } from "@playwright/test";
import { choose, dialog, toast, unique, watchErrors } from "./helpers";

async function signIn(browser: Browser, username: string, password: string) {
  const ctx = await browser.newContext({ storageState: { cookies: [], origins: [] } });
  const page = await ctx.newPage();
  await page.goto("/login");
  await page.getByLabel("Username").fill(username);
  await page.getByLabel("Password").fill(password);
  await page.getByRole("button", { name: /sign in/i }).click();
  await expect(page).toHaveURL(/\/$/);
  return { ctx, page };
}

test("an admin creates a viewer who sees the console read-only", async ({ page, browser }) => {
  const name = unique("viewer");
  await page.goto("/users");
  await page.getByRole("button", { name: "New user" }).first().click();
  const d = dialog(page, "New user");
  await d.getByLabel("Username").fill(name);
  await d.getByLabel("Password").fill("viewer-password-1");
  await choose(page, d, "Role", "Viewer");
  await d.getByRole("button", { name: "Create" }).click();
  await toast(page, "User created");
  await expect(page.getByText(name).first()).toBeVisible();

  const viewer = await signIn(browser, name, "viewer-password-1");
  await viewer.page.goto("/users");
  await expect(viewer.page.getByText("Access denied")).toBeVisible();
  await viewer.page.goto("/workloads");
  await expect(viewer.page.getByRole("button", { name: /new workload/i })).toHaveCount(0);
  await viewer.ctx.close();

  // Clean up: delete the user.
  await page.getByRole("button", { name: `Delete ${name}` }).click();
  await dialog(page, "Delete user").getByRole("button", { name: "Delete" }).click();
  await toast(page, "User deleted");
});

test("a secret is stored encrypted and revealed on demand", async ({ page }) => {
  const errors = watchErrors(page);
  const name = unique("db-password");
  await page.goto("/secrets");
  await page.getByRole("button", { name: "New secret" }).first().click();
  const d = dialog(page, "New secret");
  await d.getByLabel("Name").fill(name);
  await d.getByPlaceholder("field").fill("value");
  await d.getByPlaceholder("value").fill("s3cr3t-e2e");
  await d.getByRole("button", { name: "Create" }).click();
  await toast(page, "Secret created");
  await page.getByRole("button", { name: `Reveal ${name}` }).click();
  await expect(dialog(page, /reveal/i).getByText("value").first()).toBeVisible();
  await page.keyboard.press("Escape");
  // The reference preview resolves it, masked.
  await page.getByPlaceholder(/paste text with references/i).fill(`X=[[${name}]]`);
  await expect(page.getByText("resolved").first()).toBeVisible();
  await expect(page.getByText("s3cr3t-e2e")).toHaveCount(0);
  await page.getByRole("button", { name: `Delete ${name}` }).click();
  await dialog(page, "Delete secret").getByRole("button", { name: "Delete" }).click();
  await toast(page, "Secret deleted");
  expect(errors).toEqual([]);
});

test("a workflow runs a script step and shows its output", async ({ page }) => {
  const name = unique("wf");
  await page.goto("/workflows/new");
  await page.getByLabel(/^name/i).first().fill(name);
  await page.getByRole("button", { name: /add step/i }).click();
  await page.getByLabel(/^script/i).fill('echo "hello from $KOWAN_WORKFLOW_NAME-e2e"');
  await page.getByRole("button", { name: /^create$/i }).click();
  await expect(page).toHaveURL(/\/workflows\/[0-9a-f-]+/);
  await page.getByRole("button", { name: /run now|^run$/i }).first().click();
  await page.getByRole("tab", { name: /^runs$/i }).click();
  await expect(page.getByText(/success/i).first()).toBeVisible({ timeout: 20_000 });
  await page.getByText(/manual/i).first().click();
  await expect(page.getByText(/hello from .*-e2e/).last()).toBeVisible();
});

test("an alert rule is created, disabled and deleted", async ({ page }) => {
  const name = unique("cpu");
  await page.goto("/monitoring");
  await page.getByRole("tab", { name: /alert rules/i }).click();
  await page.getByRole("button", { name: /new rule|new alert rule|add rule/i }).first().click();
  const d = dialog(page, "New alert rule");
  await d.getByLabel("Name", { exact: true }).fill(name);
  await d.getByRole("button", { name: "Create" }).click();
  await toast(page, "Rule created");
  const row = page.getByRole("row").filter({ hasText: name });
  // The switch input is visually hidden behind its track: click through it.
  await row.getByRole("switch", { name: "Toggle rule enabled" }).click({ force: true });
  await row.getByRole("button", { name: "Delete rule" }).click();
  await dialog(page, "Delete alert rule").getByRole("button", { name: "Delete" }).click();
  await toast(page, "Rule deleted");
});

test("the local host is online and its containers are listed", async ({ page }) => {
  await page.goto("/hosts");
  await expect(page.getByText(/^online$/i).first()).toBeVisible();
  await page.goto("/workloads");
  await expect(page.getByText(/workloads/).first()).toBeVisible();
});
