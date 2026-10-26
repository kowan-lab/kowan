import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { reply, requests } from "../../test/api";
import { containers } from "../../test/fixtures";
import { currentPath, pageRoot, renderApp } from "../../test/render";
import { dialog, press, toast } from "../../test/ui";

const updates = [{ containerId: containers[0].id, containerName: "shop-web-1", image: "nginx:1.27", localDigest: "a", remoteDigest: "b", updateAvailable: true, checkedAt: "2026-10-26T10:00:00Z" }];

async function ready() {
  return within(pageRoot()).findAllByText("shop", {}, { timeout: 10_000 });
}

describe("workloads list", () => {
  it("filters by attention, updates and public ports", async () => {
    const { user } = renderApp("/workloads", { api: { "GET /hosts/:id/updates": updates } });
    await ready();
    const group = screen.getByRole("group", { name: /filter workloads/i });
    await user.click(within(group).getByRole("button", { name: /needs you/i }));
    await waitFor(() => expect(currentPath()).toBe("/workloads?filter=attn"));
    expect(await screen.findAllByText("nightly-job")).not.toHaveLength(0);
    await user.click(within(group).getByRole("button", { name: /updates/i }));
    await waitFor(() => expect(currentPath()).toBe("/workloads?filter=upd"));
    await user.click(within(group).getByRole("button", { name: /public/i }));
    expect(await screen.findAllByText("shop")).not.toHaveLength(0);
    await user.click(within(group).getByRole("button", { name: /^all/i }));
    await waitFor(() => expect(currentPath()).toBe("/workloads"));
  });

  it("switches to the table, expands a workload and opens a container", async () => {
    const { user } = renderApp("/workloads");
    await ready();
    await user.click(within(screen.getByRole("group", { name: /^view$/i })).getAllByRole("button")[1]);
    expect(await screen.findByText(/^managed$/i)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /show containers of shop/i }));
    expect(screen.getByRole("button", { name: /hide containers of shop/i })).toHaveAttribute("aria-expanded", "true");
    await user.click(screen.getAllByText("shop-db-1").at(-1)!);
    await waitFor(() => expect(currentPath()).toMatch(/^\/containers\//));
  });

  it("nests a discovered swarm stack: stack, services, containers", async () => {
    const swarm = (name: string, svc: string) => ({
      ...containers[2], id: name.padEnd(64, "0"), names: [`/${name}`], image: `${svc}:1`, state: "running", status: "Up 1 hour",
      labels: { "com.docker.stack.namespace": "mon", "com.docker.swarm.service.name": `mon_${svc}` },
    });
    const { user } = renderApp("/workloads?view=table", {
      api: { "GET /hosts/:id/containers": [...containers, swarm("mon_web.1.a", "web"), swarm("mon_web.2.b", "web"), swarm("mon_db.1.c", "db")] },
    });
    await ready();
    await user.click(within(screen.getByRole("group", { name: /^view$/i })).getAllByRole("button")[1]);
    await user.click(await screen.findByRole("button", { name: /show containers of mon$/i }));
    const web = screen.getByRole("button", { name: /show containers of service web/i });
    expect(screen.getByRole("button", { name: /show containers of service db/i })).toBeInTheDocument();
    const replica = screen.getByText("mon_web.2.b").closest("tr")!;
    expect(replica).toHaveAttribute("aria-hidden", "true");
    expect(within(web.closest("tr")!).getByText("Running · 2/2")).toBeInTheDocument();
    await user.click(web);
    expect(screen.getByRole("button", { name: /hide containers of service web/i })).toHaveAttribute("aria-expanded", "true");
    expect(replica).not.toHaveAttribute("aria-hidden");
    expect(screen.getByText("mon_db.1.c").closest("tr")).toHaveAttribute("aria-hidden", "true");
    await user.click(screen.getByText("mon_web.2.b"));
    await waitFor(() => expect(currentPath()).toMatch(/^\/containers\//));
  });

  it("remembers the table view", async () => {
    localStorage.setItem("kowan-workloads-view", "table");
    renderApp("/workloads");
    expect(await screen.findByText(/^managed$/i, {}, { timeout: 10_000 })).toBeInTheDocument();
  });

  it("starts a new workload from the menu", async () => {
    const { user } = renderApp("/workloads");
    await ready();
    await press(user, /new workload/i, pageRoot());
    await user.click(await screen.findByRole("menuitem", { name: /from a template/i }));
    await waitFor(() => expect(currentPath()).toBe("/templates"));
  });

  it("checks for updates and prunes stopped containers", async () => {
    const { user } = renderApp("/workloads", { api: { "GET /hosts/:id/updates": updates, "POST /hosts/:id/prune/containers": { spaceReclaimed: 2048 } } });
    await ready();
    await press(user, "More actions", pageRoot());
    await user.click(await screen.findByRole("menuitem", { name: /check for updates/i }));
    await waitFor(() => expect(requests("GET", "/hosts/local/updates").some((r) => r.query.refresh === "1")).toBe(true));
    await press(user, "More actions", pageRoot());
    await user.click(await screen.findByRole("menuitem", { name: /prune stopped containers/i }));
    await press(user, /prune/i, await dialog());
    await toast(/reclaimed 2(\.0)? kb/i);
  });

  it("reports a failed prune", async () => {
    const { user } = renderApp("/workloads", { api: { "POST /hosts/:id/prune/containers": reply(500, { error: "prune busy" }) } });
    await ready();
    await press(user, "More actions", pageRoot());
    await user.click(await screen.findByRole("menuitem", { name: /prune stopped containers/i }));
    await press(user, /prune/i, await dialog());
    await toast(/prune busy/i);
  });

  it("shows the empty and error states and an offline host", async () => {
    renderApp("/workloads", { api: { "GET /hosts/:id/containers": [], "GET /deployments": [] } });
    expect(await screen.findByText(/nothing runs on local yet/i, {}, { timeout: 10_000 })).toBeInTheDocument();
  });

  it("offers a retry when loading fails", async () => {
    const { user } = renderApp("/workloads", { api: { "GET /hosts/:id/containers": reply(500, { error: "engine down" }), "GET /deployments": reply(500, { error: "db down" }) } });
    expect(await screen.findByText(/couldn't load the workloads/i, {}, { timeout: 10_000 })).toBeInTheDocument();
    await press(user, /^retry$/i);
  });

  it("lists offline hosts in the all-hosts view", async () => {
    localStorage.setItem("kowan_host", "all");
    renderApp("/workloads");
    expect(await screen.findByText(/edge-1 is offline/i, {}, { timeout: 10_000 })).toBeInTheDocument();
  });

  it("explains a filter with no match", async () => {
    renderApp("/workloads?filter=upd");
    expect(await screen.findByText(/no workloads match/i, {}, { timeout: 10_000 })).toBeInTheDocument();
  });
});

describe("workloads list for viewers", () => {
  it("hides the create and prune actions", async () => {
    renderApp("/workloads", { role: "viewer" });
    await within(pageRoot()).findAllByText("shop", {}, { timeout: 10_000 });
    expect(screen.queryByRole("button", { name: /new workload/i })).toBeNull();
    expect(screen.queryByRole("button", { name: "More actions" })).toBeNull();
  });
});
