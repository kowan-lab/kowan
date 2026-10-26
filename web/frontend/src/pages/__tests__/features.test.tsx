// Features on/off: the Settings card, and a feature that is off leaving the
// navigation, its links and its page.
import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { requests } from "../../test/api";
import { pageRoot, renderApp } from "../../test/render";

const off = { templates: false, networkMap: false, gitops: false };

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

  it("says a page is off when reached directly", async () => {
    renderApp("/templates", { api: { "GET /features": off } });
    expect(await screen.findByText("Templates is turned off", {}, { timeout: 10_000 })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /open settings/i })).toHaveAttribute("href", "/settings");
  });
});
