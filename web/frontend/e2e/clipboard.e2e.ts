import { readFileSync } from "node:fs";
import { expect, test } from "@playwright/test";
import { PORT } from "../playwright.config";
import { dialog, unique } from "./helpers";

// A server reached as http://<address> is not a secure context, so the page
// has no navigator.clipboard. kowan.internal maps to 127.0.0.1 but, unlike
// localhost, is not treated as secure: the copy must use its fallback.
const ORIGIN = `http://kowan.internal:${PORT}`;
test.use({ launchOptions: { args: ["--host-resolver-rules=MAP kowan.internal 127.0.0.1"] } });

test("copies the join command where the Clipboard API is missing", async ({ page, context }) => {
  // The saved session belongs to 127.0.0.1: carry its token over.
  const state = JSON.parse(readFileSync("e2e/.data/admin.json", "utf8"));
  const storage = state.origins[0].localStorage as { name: string; value: string }[];
  await context.addInitScript((items) => {
    for (const { name, value } of items) localStorage.setItem(name, value);
  }, storage);

  await page.goto(`${ORIGIN}/hosts?add=1`);
  expect(await page.evaluate(() => [window.isSecureContext, "clipboard" in navigator])).toEqual([false, false]);
  const add = dialog(page, "Add host");
  await add.getByRole("tab", { name: "Edge" }).click();
  await add.getByLabel("Name").fill(unique("edge-copy"));
  await add.getByRole("button", { name: "Add host" }).click();
  const join = dialog(page, /join command/i);
  const command = (await join.locator("pre, code").last().innerText()).trim();
  expect(command).toContain("docker run");
  // The browser's own copy event says what it put on the clipboard.
  await page.evaluate(() => {
    document.addEventListener("copy", () => {
      (window as unknown as { copied: string }).copied = String(document.getSelection());
    });
  });
  await join.getByRole("button", { name: "Copy Docker command" }).click();
  await expect.poll(() => page.evaluate(() => (window as unknown as { copied?: string }).copied)).toBe(command);
  await expect(page.getByText("Could not copy")).toHaveCount(0);
  await expect(join).toBeVisible();
});
