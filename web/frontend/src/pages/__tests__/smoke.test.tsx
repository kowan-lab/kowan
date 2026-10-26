// Every route renders its page (lazy chunk loaded, data fetched from the fake
// backend, no crash) for an admin; a heading names the page.
import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { pageRoot, renderApp } from "../../test/render";

// [path, text naming the page, fixture data the page must show]
const ROUTES: [string, RegExp, RegExp?][] = [
  ["/hosts", /^hosts$/i, /edge-1/],
  ["/hosts/local", /last seen/i],
  ["/images", /^images$/i, /postgres:16/],
  ["/volumes", /^volumes$/i, /orphan-data/],
  ["/volume-explorer", /volume explorer/i],
  ["/networks", /^networks$/i, /shop_default/],
  ["/networks/map", /network map/i],
  ["/exposure", /exposure/i],
  ["/credentials", /credentials/i],
  ["/registries", /registries/i],
  ["/registry-explorer", /registry explorer/i],
  ["/secrets", /secrets/i],
  ["/workloads", /workloads/i, /shop/],
  ["/deployments/new", /new (deployment|workload)/i],
  ["/templates", /templates/i],
  ["/workflows", /workflows/i],
  ["/workflows/new", /workflow/i],
  ["/backups", /backups/i],
  ["/schedules", /schedules/i],
  ["/gitops", /gitops/i],
  ["/logs", /logs/i],
  ["/activity", /activity/i],
  ["/monitoring", /monitoring/i],
  ["/channels", /notifications/i],
  ["/users", /users/i],
  ["/api-keys", /api keys/i],
  ["/audit", /audit/i],
  ["/settings", /profile|settings/i],
  ["/ai", /ai assistants/i],
  ["/settings/network", /network access/i],
  ["/auto-update", /auto-update/i],
  ["/teams", /teams/i],
  ["/sso", /single sign-on/i],
  ["/no/such/page", /there is no page at/i],
];

describe("every page renders", () => {
  for (const [path, title, sample] of ROUTES) {
    it(path, async () => {
      renderApp(path);
      // The page (not the sidebar) names itself; list pages show fixture rows.
      await waitFor(() => expect(within(pageRoot()).getAllByText(title).length).toBeGreaterThan(0), { timeout: 10_000 });
      if (sample) expect((await within(pageRoot()).findAllByText(sample, {}, { timeout: 10_000 })).length).toBeGreaterThan(0);
      expect(screen.queryByText(/something went wrong/i)).not.toBeInTheDocument();
    });
  }
});
