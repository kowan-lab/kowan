import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { reply, requests } from "../../test/api";
import { renderApp } from "../../test/render";
import { growthText, pointLabel, RANGES, rates } from "../../components/apps/WorkloadResources";

const MiB = 1024 * 1024;
const point = (i: number, over: Record<string, unknown> = {}) => ({
  time: new Date(Date.UTC(2026, 9, 26, 8, i * 5)).toISOString().replace(".000", ""),
  containers: 1, cpu: 10 + i, cpuMax: 20 + i, mem: (200 + i * 3) * MiB, memMax: (210 + i * 3) * MiB,
  memLimit: 1024 * MiB, netRx: 0, netTx: 0, ...over,
});
const growth = {
  points: 36, spanHours: 3, slopeBytesPerHour: 36 * MiB, r2: 0.99, start: 200 * MiB, end: 305 * MiB,
  limit: 1024 * MiB, hoursToLimit: 20, restarted: false, hostId: "local", growing: true,
};

describe("workload resources helpers", () => {
  it("labels points and words the growth warning", () => {
    expect(RANGES.map((r) => r.label)).toEqual(["6h", "24h", "7d", "30d"]);
    expect(pointLabel("2026-10-26T08:05:00Z", 24)).toMatch(/\d{2}.\d{2}/);
    expect(pointLabel("2026-10-26T08:00:00Z", 168)).toMatch(/Oct|26/);
    expect(growthText(growth)).toMatch(/from 200(\.0)? MB to 305(\.0)? MB over 3h.*limit in about 20h/);
    expect(growthText({ ...growth, hoursToLimit: null })).not.toMatch(/limit/);
    // 300 s apart: 3000 bytes -> 10 B/s; a reset after a restart counts as 0.
    const pts = [point(0, { netRx: 1000 }), point(1, { netRx: 4000 }), point(2, { netRx: 10 })];
    expect(rates(pts, "netRx")).toEqual([0, 10, 0]);
  });
});

describe("workload resources panel", () => {
  it("charts CPU and memory, warns about growth and switches ranges", async () => {
    const { user } = renderApp("/workloads/dep~d-shop", {
      api: {
        "GET /resource-history/:w": (r) => ({
          hours: Number(r.query.hours), points: [point(0), point(1), point(2)], growth: r.query.hours === "24" ? growth : null,
        }),
      },
    });
    const band = (await screen.findByRole("heading", { name: "Resources" }, { timeout: 10_000 })).closest("section")!;
    expect(await within(band).findByText(/memory keeps growing/i)).toBeInTheDocument();
    expect(within(band).getByText(/now 12\.0% CPU/)).toBeInTheDocument();
    expect(within(band).getByText(/peak 22\.0%/)).toBeInTheDocument();
    expect(within(band).getByText(/limit 1(\.0)? GB/)).toBeInTheDocument();
    expect(within(band).getByText("Network (KiB/s)")).toBeInTheDocument();
    expect(requests("GET", "/resource-history/:w")[0]).toMatchObject({ path: "/resource-history/dep~d-shop", query: { hours: "24" } });
    await user.click(within(band).getByRole("tab", { name: "7d" }));
    await waitFor(() => expect(requests("GET", "/resource-history/:w").at(-1)!.query.hours).toBe("168"));
    await waitFor(() => expect(within(band).queryByText(/memory keeps growing/i)).toBeNull());
  });

  it("says when there is no history yet and hides on errors", async () => {
    renderApp("/workloads/dep~d-shop", {
      api: { "GET /resource-history/:w": { hours: 24, points: [point(0, { memLimit: 0 })], growth: null } },
    });
    expect(await screen.findByText(/no history for this range yet/i, {}, { timeout: 10_000 })).toBeInTheDocument();
  });

  it("hides the panel when the history cannot be read", async () => {
    renderApp("/workloads/dep~d-shop", { api: { "GET /resource-history/:w": reply(403, { error: "insufficient permissions" }) } });
    await screen.findByRole("heading", { name: /policies and wiring/i }, { timeout: 10_000 });
    await waitFor(() => expect(requests("GET", "/resource-history/:w").length).toBeGreaterThan(0));
    expect(screen.queryByRole("heading", { name: "Resources" })).toBeNull();
  });

  it("shows the memory without a limit", async () => {
    renderApp("/workloads/dep~d-shop", {
      api: { "GET /resource-history/:w": { hours: 24, points: [point(0, { memLimit: 0 }), point(1, { memLimit: 0 })], growth: null } },
    });
    expect(await screen.findByText(/no limit/i, {}, { timeout: 10_000 })).toBeInTheDocument();
  });
});
