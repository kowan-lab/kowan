import { screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { requests } from "../../test/api";
import { renderApp } from "../../test/render";

const health = {
  status: "degraded", problems: ["host edge-1 is offline"],
  server: { version: "0.1.16", startedAt: "2026-10-31T10:00:00Z", python: "3.13.1" },
  storage: { files: [{ name: "kowan.db", description: "Configuration, history, metrics", bytes: 4_200_000 }, { name: "logs.db", description: "Retained logs", bytes: 0 }],
    disk: { path: "/data", total: 100_000_000_000, free: 40_000_000_000 } },
  hosts: [
    { id: "local", name: "local", type: "local", tags: ["production"], lastSeen: "2026-10-31T10:00:00Z", online: true, pingMs: 1.2, error: "", crashWatch: true },
    { id: "h-edge", name: "edge-1", type: "agent", tags: [], lastSeen: "", online: false, pingMs: null, error: "no answer in 3s", crashWatch: false },
  ],
  loops: [{ name: "Monitor", description: "Alert rules", lastRun: "2026-10-31T10:00:00Z", everySeconds: 15, late: false },
    { name: "Auto-update", description: "Image checks", lastRun: "", everySeconds: 3600, late: true }],
  work: { runbooksRunning: 1, pulls: 0, websockets: 3, crashSnapshotsTaken: 2 },
};

describe("health and inventory", () => {
  it("shows Kowan's own health", async () => {
    renderApp("/settings/health", { api: { "GET /system/health": health } });
    expect(await screen.findByText("Needs a look", {}, { timeout: 10_000 })).toBeInTheDocument();
    expect(screen.getByText("host edge-1 is offline")).toBeInTheDocument();
    expect(screen.getByText("1.2 ms")).toBeInTheDocument();
    expect(screen.getByText("no answer in 3s")).toBeInTheDocument();
    expect(screen.getByText("Late")).toBeInTheDocument();
    expect(screen.getByText(/1 runbook, 0 pulls, 3 open WebSockets/)).toBeInTheDocument();
  });

  it("exports the inventory from the workloads menu", async () => {
    const { user } = renderApp("/workloads", { api: { "GET /inventory": "host,container\nlocal,web\n" } });
    await user.click(await screen.findByRole("button", { name: /more actions/i }, { timeout: 10_000 }));
    await user.click(await screen.findByRole("menuitem", { name: /export inventory \(csv\)/i }));
    await waitFor(() => expect(requests("GET", "/inventory")[0].query.format).toBe("csv"));
  });
});
