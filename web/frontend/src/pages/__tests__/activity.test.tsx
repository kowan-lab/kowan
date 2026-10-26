import { act, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { reply, sockets } from "../../test/api";
import { currentPath, pageRoot, renderApp } from "../../test/render";

const t = (s: number) => new Date(Date.UTC(2026, 9, 26, 9, 0, s)).toISOString();
const ev = (id: string, s: number, type: string, action: string, over: Record<string, unknown> = {}) => ({
  id, time: t(s), type, action, actorId: "c-web".padEnd(64, "0"), actorName: "shop-web-1", image: "nginx:1.27", attributes: {}, ...over,
});
const history = [
  ev("6", 6, "container", "health_status", { attributes: { detail: "unhealthy" } }),
  ev("5", 5, "container", "die", { attributes: { exitCode: "137", signal: "9" }, actorName: "shop-db-1", actorId: "c-db" }),
  ev("4", 4, "network", "connect", { actorId: "n-shop", actorName: "shop_default", attributes: { container: "c-web".padEnd(64, "0") } }),
  ev("3", 3, "volume", "mount", { actorId: "", actorName: "nfs-data", attributes: { driver: "nfs" } }),
  ev("2", 2, "image", "pull", { actorId: "sha256:" + "a".repeat(64), actorName: "" }),
  ev("1", 1, "container", "destroy", { actorName: "old-job" }),
];

// The page's own stream (the shell keeps a separate one without `since`).
const streams = () => sockets.filter((s) => s.url.includes("/events/ws") && s.url.includes("since="));
const stream = () => streams().at(-1)!;

async function ready() {
  return within(pageRoot()).findByText("shop-db-1", {}, { timeout: 10_000 });
}

describe("activity page", () => {
  it("merges history and live events with details and links", async () => {
    const { user } = renderApp("/activity", { api: { "GET /hosts/:id/events": history } });
    await ready();
    expect(screen.getByText("exit 137")).toBeInTheDocument();
    expect(screen.getByText("signal 9")).toBeInTheDocument();
    expect(screen.getByText("unhealthy")).toBeInTheDocument();
    expect(screen.getByText(/container c-web0000000/)).toBeInTheDocument();
    expect(screen.getByText("nfs")).toBeInTheDocument();
    expect(screen.getByText("a".repeat(12))).toBeInTheDocument();
    // Destroyed objects have no link.
    expect(screen.getByText("old-job").closest("a")).toBeNull();
    await waitFor(() => expect(screen.getByText("Live")).toBeInTheDocument());

    const ws = stream();
    act(() => {
      ws.emit(JSON.stringify(ev("7", 7, "container", "start", { actorName: "fresh-1", actorId: "c-fresh" })));
      ws.emit(JSON.stringify(history[0])); // duplicate of history: dropped
      ws.emit("not json");
    });
    expect(await screen.findByText("fresh-1")).toBeInTheDocument();
    expect(screen.getAllByText("unhealthy")).toHaveLength(1);
    await user.click(screen.getByText("shop_default"));
    await waitFor(() => expect(currentPath()).toBe("/networks/n-shop?host=local"));
  });

  it("holds new events while paused", async () => {
    const { user } = renderApp("/activity", { api: { "GET /hosts/:id/events": history } });
    await ready();
    await user.click(screen.getByRole("button", { name: /^pause$/i }));
    const ws = stream();
    act(() => ws.emit(JSON.stringify(ev("8", 8, "image", "tag", { actorName: "nginx:latest" }))));
    expect(await screen.findByRole("button", { name: "Resume (1)" }, { timeout: 3000 })).toBeInTheDocument();
    expect(screen.queryByText("nginx:latest")).toBeNull();
    await user.click(screen.getByRole("button", { name: "Resume (1)" }));
    expect(await screen.findByText("nginx:latest")).toBeInTheDocument();
  });

  it("reports a broken stream and reconnects", async () => {
    renderApp("/activity", { api: { "GET /hosts/:id/events": history } });
    await ready();
    await waitFor(() => expect(screen.getByText("Live")).toBeInTheDocument());
    const first = stream();
    act(() => first.emit("error: engine gone"));
    act(() => first.close());
    expect(await screen.findByText(/no live stream from local/i)).toBeInTheDocument();
    // The client retries with a new socket after a back-off.
    await waitFor(() => expect(streams().length).toBeGreaterThan(1), { timeout: 5000 });
  });

  it("stops at a forbidden host", async () => {
    renderApp("/activity", { api: { "GET /hosts/:id/events": reply(403, { error: "forbidden" }) } });
    await waitFor(() => expect(screen.getByText(/no host access/i)).toBeInTheDocument(), { timeout: 10_000 });
    expect(streams()).toHaveLength(0);
  });

  it("aggregates online hosts and handles none", async () => {
    localStorage.setItem("kowan_host", "all");
    renderApp("/activity", { api: { "GET /hosts/:id/events": [], "GET /hosts": [{ id: "h-edge", name: "edge-1", type: "agent", online: false }] } });
    expect(await screen.findByText(/no online hosts/i, {}, { timeout: 10_000 })).toBeInTheDocument();
  });

  it("shows the empty state for a quiet host", async () => {
    renderApp("/activity", { api: { "GET /hosts/:id/events": [] } });
    expect(await screen.findByText(/no events in the last hour/i, {}, { timeout: 10_000 })).toBeInTheDocument();
  });
});
