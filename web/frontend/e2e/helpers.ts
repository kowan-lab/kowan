import { expect, type Page } from "@playwright/test";

export const ADMIN = { username: "root", password: "e2e-admin-password" };

/** Fail the test on uncaught page errors and console errors. */
export function watchErrors(page: Page): string[] {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(`pageerror: ${e.message}`));
  page.on("console", (m) => {
    // A refused request (expected 4xx in a test) logs a resource error; the
    // page's own handling is what the test checks.
    if (m.type() === "error" && !/Failed to load resource/.test(m.text())) errors.push(`console: ${m.text()}`);
  });
  return errors;
}

/** Wait for the page heading inside the app shell. */
export async function heading(page: Page, name: string | RegExp) {
  await expect(page.getByRole("heading", { name, level: 1 }).or(page.getByRole("heading", { name })).first()).toBeVisible();
}

/** A toast with ``text``. */
export async function toast(page: Page, text: string | RegExp) {
  await expect(page.locator("[data-sonner-toast]").filter({ hasText: text }).first()).toBeVisible();
}

/** The open dialog titled ``title``. */
export function dialog(page: Page, title: string | RegExp) {
  return page.getByRole("dialog", { name: title });
}

/** Pick ``option`` in the kit Select labelled ``label``. */
export async function choose(page: Page, root: ReturnType<Page["locator"]> | Page, label: string | RegExp, option: string | RegExp) {
  await root.getByRole("combobox", { name: label }).click();
  await page.getByRole("option", { name: option }).click();
}

export const unique = (prefix: string) => `${prefix}-${Date.now().toString(36)}`;
