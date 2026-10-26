// Features on/off: the Settings card, and a feature that is off leaving the
// navigation, its links and its page.
import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { requests } from "../../test/api";
import { containers } from "../../test/fixtures";
import { isKowanContainer } from "../../lib/features";
import { currentPath, pageRoot, renderApp } from "../../test/render";

const off = { templates: false, networkMap: false, gitops: false, systemContainers: false };
const kowan = { ...containers[2], id: "k".padEnd(64, "0"), names: ["/kowan"], state: "running", status: "Up 1 hour",
  labels: { "org.opencontainers.image.title": "kowan" } };

describe("features", () => {
  it("are turned off and on from Settings", async () => {
    const { user } = renderApp("/settings", { api: { "PUT /features": (r: { body: object }) => ({ templates: true, networkMap: true, gitops: true, ...r.body }) } });
    const toggle = await screen.findByRole("switch", { name: /^templates/i }, { timeout: 10_000 });
    expect(toggle).toBeChecked();
    await user.click(toggle);
    await waitFor(() => expect(requests("PUT", "/features")[0].body).toEqual({ templates: false }));
    await waitFor(() => expect(screen.getByRole("switch", { name: /^templates/i })).not.toBeChecked());
  });

  it("hides a feature that is off from the navigation and its links", async () => {
    const { user } = renderApp("/workloads", { api: { "GET /features": off } });
    await within(pageRoot()).findAllByText("shop", {}, { timeout: 10_000 });
    // The sidebar keeps Images but drops Templates and Network map.
    await waitFor(() => expect(screen.queryByRole("link", { name: /^templates$/i })).toBeNull());
    expect(screen.queryByRole("link", { name: /^network map$/i })).toBeNull();
    expect(screen.getAllByRole("link", { name: /^images$/i }).length).toBeGreaterThan(0);
    await user.click(screen.getByRole("button", { name: /new workload/i }));
    expect(screen.queryByRole("menuitem", { name: /from a template/i })).toBeNull();
    expect(screen.getByRole("menuitem", { name: /import stacks/i })).toBeInTheDocument();
  });

  it("shows every feature while it is on", async () => {
    renderApp("/workloads");
    await within(pageRoot()).findAllByText("shop", {}, { timeout: 10_000 });
    expect(screen.getAllByRole("link", { name: /^templates$/i }).length).toBeGreaterThan(0);
    expect(screen.getAllByRole("link", { name: /^network map$/i }).length).toBeGreaterThan(0);
  });

  it("tells Kowan's own containers by their labels", () => {
    const own: Record<string, string>[] = [{ "org.opencontainers.image.title": "kowan" }, { "org.opencontainers.image.title": "kowan-agent" },
      { "kowan.support": "true" }, { "kowan.role": "server-updater" }, { "kowan.volbackup.helper": "1" }];
    const others: (Record<string, string> | undefined)[] = [undefined, {}, { "kowan.support": "false" },
      { "org.opencontainers.image.title": "nginx" }, { "kowan.deployment.id": "d" }];
    for (const labels of own) expect(isKowanContainer(labels)).toBe(true);
    for (const labels of others) expect(isKowanContainer(labels)).toBe(false);
  });

  it("hides Kowan's own containers unless Settings shows them", async () => {
    renderApp("/workloads", { api: { "GET /hosts/:id/containers": [...containers, kowan] } });
    await within(pageRoot()).findAllByText("shop", {}, { timeout: 10_000 });
    expect(within(pageRoot()).queryByText("kowan")).toBeNull();
  });

  it("shows Kowan's own containers when asked to", async () => {
    renderApp("/workloads", { api: { "GET /hosts/:id/containers": [...containers, kowan], "GET /features": { ...off, systemContainers: true } } });
    expect(await within(pageRoot()).findByText("kowan", {}, { timeout: 10_000 })).toBeInTheDocument();
  });

  it("opens Settings from the bottom of the sidebar, not the account menu", async () => {
    const { user } = renderApp("/workloads");
    await within(pageRoot()).findAllByText("shop", {}, { timeout: 10_000 });
    await user.click(screen.getByRole("button", { name: "Account and theme" }));
    expect(screen.queryByRole("menuitem", { name: /settings/i })).toBeNull();
    await user.keyboard("{Escape}");
    await user.click(screen.getByRole("link", { name: /^settings$/i }));
    expect(await screen.findByRole("heading", { name: "Settings" }, { timeout: 10_000 })).toBeInTheDocument();
  });

  it("switching host goes back to the overview", async () => {
    const { user } = renderApp("/workloads");
    await within(pageRoot()).findAllByText("shop", {}, { timeout: 10_000 });
    await user.click(screen.getByRole("button", { name: /change host/i }));
    await user.click(await screen.findByRole("menuitem", { name: /edge-1/i }));
    await waitFor(() => expect(currentPath()).toBe("/"));
    expect(localStorage.getItem("kowan_host")).toBe("h-edge");
  });

  it("says a page is off when reached directly", async () => {
    renderApp("/templates", { api: { "GET /features": off } });
    expect(await screen.findByText("Templates is turned off", {}, { timeout: 10_000 })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /open settings/i })).toHaveAttribute("href", "/settings");
  });
});
