import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { containers, hosts } from "../../test/fixtures";
import { pageRoot, renderApp } from "../../test/render";

// The local host keeps the id `local` (workload ids, URLs and keys use it) but
// was renamed: everything the user reads names it server-docker-1.
const renamed = hosts.map((h) => (h.id === "local" ? { ...h, name: "server-docker-1" } : h));
const blog = [
  { ...containers[1], id: "b1".padEnd(64, "0"), names: ["/blog-app-1"], labels: { "com.docker.compose.project": "blog", "com.docker.compose.service": "app" } },
];
const api = { "GET /hosts": renamed, "GET /hosts/:id": renamed[0], "GET /hosts/:id/containers": [...containers, ...blog] };
const LOCAL = /\blocal\b/;

describe("a renamed local host", () => {
  it("is named on a workload page: breadcrumb, host fact and top bar", async () => {
    renderApp("/workloads/stack~local~blog", { api });
    await screen.findByRole("heading", { name: "blog" }, { timeout: 10_000 });
    const crumbs = within(pageRoot()).getByRole("navigation", { name: "Breadcrumb" });
    expect(await within(crumbs).findByText("server-docker-1")).toBeInTheDocument();
    expect(crumbs.textContent).not.toMatch(LOCAL);
    const host = within(pageRoot()).getByRole("link", { name: "server-docker-1" });
    expect(host).toHaveAttribute("href", "/hosts/local");
    expect(within(pageRoot()).queryAllByText(LOCAL)).toHaveLength(0);
    expect(screen.getAllByText("server-docker-1").length).toBeGreaterThan(2);
  });

  it("is named in the list's cards and table", async () => {
    localStorage.setItem("kowan_host", "all");
    renderApp("/workloads", { api });
    await within(pageRoot()).findAllByText("blog", {}, { timeout: 10_000 });
    expect(within(pageRoot()).getAllByText(/· server-docker-1$/).length).toBeGreaterThan(0);
    expect(within(pageRoot()).queryAllByText(LOCAL)).toHaveLength(0);

    localStorage.setItem("kowan-workloads-view", "table");
    renderApp("/workloads", { api });
    await screen.findAllByRole("columnheader", { name: "Workload" }, { timeout: 10_000 });
    const rows = within(pageRoot()).getAllByRole("row").filter((r) => within(r).queryByText("blog"));
    expect(within(rows[0]).getByText("server-docker-1")).toBeInTheDocument();
    expect(within(pageRoot()).queryAllByText(LOCAL)).toHaveLength(0);
  });

  it("is named when the workload is gone", async () => {
    renderApp("/workloads/stack~local~gone", { api });
    expect(await screen.findByText(/Nothing answers to the stack gone on server-docker-1\./, {}, { timeout: 10_000 })).toBeInTheDocument();
    expect(within(pageRoot()).queryAllByText(LOCAL)).toHaveLength(0);
  });
});
