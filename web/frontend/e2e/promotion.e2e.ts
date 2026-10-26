import { expect, test } from "@playwright/test";
import { choose, dialog, toast, unique, watchErrors } from "./helpers";

// Promotion against the real server: pair staging with production, read the
// plan (image change and diff), promote without deploying, and find the new
// revision in production's history. Nothing runs on Docker.
test("staging is promoted into production as a new revision", async ({ page }) => {
  const errors = watchErrors(page);
  const stage = unique("stage");
  const prod = unique("prod");
  await page.goto("/workloads");
  const ids = await page.evaluate(async ([s, p]) => {
    const make = async (name: string, tag: string) => {
      const r = await fetch("/api/v1/deployments", {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${localStorage.getItem("kowan_token")}` },
        body: JSON.stringify({ name, type: "compose", strategy: "standalone", targets: ["local"],
                               compose: `services:\n  web:\n    image: nginx:${tag}\n` }),
      });
      return (await r.json()).id as string;
    };
    return [await make(s, "1.28"), await make(p, "1.27")];
  }, [stage, prod]);

  await page.goto(`/workloads/dep~${ids[0]}`);
  const band = page.locator("section").filter({ has: page.getByRole("heading", { name: "Promotion" }) });
  await band.getByRole("button", { name: "Promote to…" }).click();
  await choose(page, band, "Promotes to", prod);
  await toast(page, `Promotes to ${prod}`);
  await band.getByRole("button", { name: `Promote to ${prod}` }).click();
  const d = dialog(page, `Promote ${stage} to ${prod}`);
  await expect(d.getByText("web image")).toBeVisible();
  await expect(d.getByRole("table", { name: "Diff" })).toContainText("nginx:1.28");
  await d.getByRole("checkbox", { name: `Deploy ${prod} now` }).uncheck({ force: true });
  await page.screenshot({ path: "test-results/promotion.png", fullPage: true });
  await d.getByRole("button", { name: `Promote to ${prod}` }).click();
  await toast(page, `Promoted to ${prod}`);

  await page.goto(`/workloads/dep~${ids[1]}?tab=history`);
  await expect(page.getByText(new RegExp(`from ${stage}: web image nginx:1.28`))).toBeVisible();
  expect(errors).toEqual([]);
});
