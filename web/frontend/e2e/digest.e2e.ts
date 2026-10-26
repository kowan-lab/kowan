import { expect, test } from "@playwright/test";
import { choose, dialog, toast, watchErrors } from "./helpers";

// The digest against the real server: schedule it, preview its text. The
// updates section is turned off so the preview does not ask registries
// about the containers of the machine running the tests.
test("the digest is scheduled and previewed", async ({ page }) => {
  const errors = watchErrors(page);
  await page.goto("/channels");
  const card = page.locator("section").filter({ has: page.getByRole("heading", { name: "Digest" }) });
  await card.getByRole("switch", { name: "Send the digest" }).check({ force: true });
  await choose(page, card, "Every", "Day");
  await card.getByText("Container updates waiting").click();
  await expect(card.getByRole("checkbox", { name: /container updates waiting/i })).not.toBeChecked();
  await card.getByRole("button", { name: "Save" }).click();
  await toast(page, "Digest scheduled");
  await card.getByRole("button", { name: "Preview" }).click();
  const d = dialog(page, "Digest preview");
  await expect(d.getByText(/^Kowan digest/)).toBeVisible({ timeout: 30_000 });
  await page.screenshot({ path: "test-results/digest.png", fullPage: true });
  expect(errors).toEqual([]);
});
