import { expect, test } from "@playwright/test";
import { watchErrors } from "./helpers";

// The console as an installed app and on a phone, against the real server.

test("installable: manifest, icons and a service worker that starts offline", async ({ page, context }) => {
  await page.goto("/workloads");
  await expect(page.getByRole("heading", { name: "Workloads" })).toBeVisible();
  const manifest = await page.evaluate(async () => {
    const href = document.querySelector<HTMLLinkElement>('link[rel="manifest"]')!.href;
    const m = await (await fetch(href)).json();
    const icons = await Promise.all(m.icons.map(async (i: { src: string }) => (await fetch(i.src)).status));
    return { name: m.name, display: m.display, icons };
  });
  expect(manifest).toEqual({ name: "Kowan", display: "standalone", icons: [200, 200, 200, 200] });
  const state = await page.evaluate(async () => (await navigator.serviceWorker.ready).active?.state);
  expect(state).toBe("activated");
  // Load once under the worker so the shell and bundles are cached, then go offline.
  await page.reload();
  await expect(page.getByRole("heading", { name: "Workloads" })).toBeVisible();
  await context.setOffline(true);
  await page.reload();
  await expect(page.getByText(/you are offline/i)).toBeVisible();
  await context.setOffline(false);
  await expect(page.getByText(/you are offline/i)).toHaveCount(0);
});

test.describe("on a phone", () => {
  test.use({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true });

  test("pages fit the screen, tables become cards, the menu is a drawer", async ({ page }) => {
    const errors = watchErrors(page);
    for (const path of ["/", "/workloads", "/images", "/volumes", "/users", "/logs", "/uptime", "/settings", "/hosts/local", "/deployments/new"]) {
      await page.goto(path);
      await page.waitForLoadState("networkidle").catch(() => undefined);
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
      expect(overflow, `${path} scrolls sideways`).toBeLessThanOrEqual(0);
    }
    await page.goto("/users");
    const table = page.locator(".kit-table").first();
    await expect(table.locator("tbody tr").first()).toBeVisible();
    expect(await table.locator("thead").evaluate((el) => getComputedStyle(el).display)).toBe("none");
    expect(await table.locator("tbody td").nth(1).evaluate((el) => getComputedStyle(el, "::before").content)).toBe('"Email"');

    await page.goto("/workloads");
    await page.getByRole("button", { name: "Toggle Sidebar" }).click();
    await page.getByRole("link", { name: "Uptime" }).click();
    await expect(page).toHaveURL(/\/uptime$/);
    await expect(page.getByRole("heading", { name: "Uptime" })).toBeVisible();
    expect(errors).toEqual([]);
  });
});
