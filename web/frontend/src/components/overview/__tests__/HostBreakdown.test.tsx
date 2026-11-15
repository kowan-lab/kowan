import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { mockApi } from "../../../test/api";
import { renderWithProviders } from "../../../test/render";
import HostBreakdown, { workloadLabel } from "../HostBreakdown";

const MiB = 1024 * 1024;

describe("HostBreakdown", () => {
  it("names workloads the way the UI does", () => {
    const deps = new Map([["d1", "shop"]]);
    expect(workloadLabel("dep~d1", deps)).toBe("shop");
    expect(workloadLabel("stack~h1~billing", deps)).toBe("billing");
    expect(workloadLabel("ctr~h1~redis", deps)).toBe("redis");
  });

  it("lists every workload, biggest first, and switches to CPU", async () => {
    mockApi({
      "GET /resource-history/host/h1": () => ({
        hours: 24, hostId: "h1", times: ["2026-11-15T10:00:00Z", "2026-11-15T10:05:00Z"],
        workloads: [
          { workload: "stack~h1~billing", cpu: 2, cpuMax: 3, cpuNow: 2, mem: 900 * MiB, memMax: 1000 * MiB, memNow: 950 * MiB, containers: 3 },
          { workload: "ctr~h1~redis", cpu: 40, cpuMax: 60, cpuNow: 50, mem: 100 * MiB, memMax: 120 * MiB, memNow: 110 * MiB, containers: 1 },
        ],
        series: [
          { workload: "stack~h1~billing", cpu: [2, 2], mem: [900 * MiB, 900 * MiB] },
          { workload: "ctr~h1~redis", cpu: [30, 50], mem: [90 * MiB, 110 * MiB] },
        ],
        other: { cpu: [0, 0], mem: [0, 0] },
      }),
      "GET /deployments": () => [],
    });
    const { user } = renderWithProviders(<HostBreakdown hostId="h1" />);
    const table = await screen.findByRole("table", {}, { timeout: 10_000 });
    let rows = within(table).getAllByRole("row").slice(1);
    expect(rows.map((r) => within(r).getByRole("link").textContent)).toEqual(["billing", "redis"]);
    expect(within(rows[0]).getByText("3 containers")).toBeInTheDocument();
    await user.click(screen.getByRole("tab", { name: "CPU" }));
    rows = within(table).getAllByRole("row").slice(1);
    expect(rows.map((r) => within(r).getByRole("link").textContent)).toEqual(["redis", "billing"]);
    expect(within(rows[0]).getByText("60.0%")).toBeInTheDocument();
  });
});
