import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { reply, requests } from "../../test/api";
import { currentPath, pageRoot, renderApp } from "../../test/render";
import { choose, press } from "../../test/ui";

const node = (id: string, kind: string, label: string, over: Record<string, unknown> = {}) => ({
  id, kind, label, sub: "", state: "", hostId: "local", stack: "shop", ref: {}, x: 0, y: 0, w: 180, h: 28, ...over,
});
const map = (over: Record<string, unknown> = {}) => ({
  nodes: [
    node("p", "port", "0.0.0.0:8080", { warn: true, x: 0, y: 40 }),
    node("d", "domain", "shop.example.com", { state: "tls", x: 0, y: 80 }),
    node("web", "container", "shop-web-1", { state: "running", ref: { containerId: "c-web" }, x: 300, y: 40 }),
    node("db", "container", "shop-db-1", { state: "exited", ref: { containerId: "c-db" }, x: 300, y: 90 }),
    node("n", "network", "shop_default", { ref: { networkId: "n-shop" }, x: 640, y: 40 }),
  ],
  edges: [
    { id: "e1", from: "p", to: "web", kind: "port" },
    { id: "e2", from: "d", to: "web", kind: "domain" },
    { id: "e3", from: "web", to: "n", kind: "network" },
    { id: "e4", from: "web", to: "db", kind: "depends" },
    { id: "e5", from: "ghost", to: "web", kind: "port" },
  ],
  groups: [
    { id: "g-host", kind: "host", label: "local", stack: "", hostId: "local", deploymentId: "", x: 0, y: 0, w: 800, h: 200 },
    { id: "g-shop", kind: "stack", label: "shop", stack: "shop", hostId: "local", deploymentId: "d-shop", x: 290, y: 30, w: 200, h: 100 },
  ],
  width: 820, height: 220, stacks: ["shop"], networks: ["shop_default"],
  hosts: [{ id: "local", name: "local", error: "" }],
  counts: { containers: 2, networks: 1, ingress: 2 },
  ...over,
});

async function graph() {
  return screen.findByRole("img", { name: /network and dependency map/i }, { timeout: 10_000 });
}

describe("network map", () => {
  it("draws the graph and opens nodes and stacks", async () => {
    renderApp("/networks/map", { api: { "GET /hosts/:id/netmap": map() } });
    const svg = await graph();
    expect(screen.getByText("2 containers")).toBeInTheDocument();
    const web = within(svg).getByRole("button", { name: "container shop-web-1, running" });
    // Hover lights the neighbourhood, Enter opens.
    fireEvent.mouseEnter(web);
    fireEvent.mouseLeave(web);
    web.focus();
    fireEvent.keyDown(web, { key: "Enter" });
    await waitFor(() => expect(currentPath()).toBe("/containers/c-web?host=local"));
  });

  it("opens networks and deployments from the graph", async () => {
    const { user } = renderApp("/networks/map", { api: { "GET /hosts/:id/netmap": map() } });
    const svg = await graph();
    await user.click(within(svg).getByRole("button", { name: "network shop_default" }));
    await waitFor(() => expect(currentPath()).toBe("/networks/n-shop?host=local"));
  });

  it("opens a stack's deployment", async () => {
    const { user } = renderApp("/networks/map", { api: { "GET /hosts/:id/netmap": map() } });
    const svg = await graph();
    await user.click(within(svg).getByText("shop"));
    await waitFor(() => expect(currentPath()).toBe("/workloads/dep~d-shop"));
  });

  it("zooms, pans and fits", async () => {
    const { user } = renderApp("/networks/map", { api: { "GET /hosts/:id/netmap": map() } });
    const svg = await graph();
    const g = svg.querySelector("g[transform]")!;
    const before = g.getAttribute("transform");
    await press(user, "Zoom in");
    expect(g.getAttribute("transform")).not.toBe(before);
    await press(user, "Zoom out");
    fireEvent.wheel(svg, { deltaY: -100, clientX: 10, clientY: 10 });
    fireEvent.wheel(svg, { deltaY: 100, clientX: 10, clientY: 10 });
    fireEvent.pointerDown(svg, { clientX: 10, clientY: 10, pointerId: 1 });
    fireEvent.pointerMove(svg, { clientX: 60, clientY: 30, pointerId: 1 });
    fireEvent.pointerUp(svg, { pointerId: 1 });
    const moved = g.getAttribute("transform");
    fireEvent.pointerMove(svg, { clientX: 200, clientY: 200 });
    expect(g.getAttribute("transform")).toBe(moved);
    await press(user, /^fit$/i);
    expect(g.getAttribute("transform")).toBe(before);
  });

  it("switches to the table and filters", async () => {
    const { user } = renderApp("/networks/map", { api: { "GET /hosts/:id/netmap": map() } });
    await graph();
    await press(user, /^table$/i, pageRoot());
    const db = await within(pageRoot()).findByRole("button", { name: "shop-db-1" });
    expect(screen.getByText("0.0.0.0:8080, shop.example.com")).toBeInTheDocument();
    await choose(user, /filter by stack/i, /^shop$/i);
    await waitFor(() => expect(requests("GET", "/hosts/local/netmap").some((r) => r.query.stack === "shop")).toBe(true));
    await choose(user, /filter by network/i, /shop_default/i);
    await waitFor(() => expect(currentPath()).toContain("network=shop_default"));
    await user.click(db);
    await waitFor(() => expect(currentPath()).toBe("/containers/c-db?host=local"));
  });

  it("defaults to the table for large hosts and aggregates all hosts", async () => {
    localStorage.setItem("kowan_host", "all");
    const many = Array.from({ length: 301 }, (_, i) => node(`c${i}`, "container", `c-${i}`, { hostId: "h-edge" }));
    renderApp("/networks/map", { api: { "GET /netmap": map({ nodes: many, edges: [], hosts: [{ id: "h-edge", name: "edge-1", error: "partial: agent slow" }] }) } });
    expect(await screen.findByText(/large host: showing the table/i, {}, { timeout: 10_000 })).toBeInTheDocument();
    expect(screen.getByText(/edge-1: partial: agent slow/)).toBeInTheDocument();
  });

  it("shows empty and error states", async () => {
    renderApp("/networks/map", { api: { "GET /hosts/:id/netmap": map({ nodes: [], edges: [], groups: [] }) } });
    expect(await screen.findByText(/nothing matches these filters/i, {}, { timeout: 10_000 })).toBeInTheDocument();
  });

  it("shows a load error", async () => {
    renderApp("/networks/map", { api: { "GET /hosts/:id/netmap": reply(500, { error: "map failed" }) } });
    expect(await screen.findByText(/map failed/i, {}, { timeout: 10_000 })).toBeInTheDocument();
  });
});
