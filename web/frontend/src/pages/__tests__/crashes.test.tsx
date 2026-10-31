import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { requests } from "../../test/api";
import { renderApp } from "../../test/render";
import { dialog } from "../../test/ui";

const snap = { id: "c1", hostId: "local", containerId: "x", containerName: "shop-web-1", workload: "dep~d-shop", image: "nginx:1.27",
  exitCode: 137, oomKilled: true, repeats: 2, createdAt: "2026-10-31T10:00:00Z", lastAt: "2026-10-31T10:04:00Z" };
const data = { exitCode: 137, oomKilled: true, error: "", startedAt: "2026-10-31T09:00:00Z", finishedAt: "2026-10-31T10:00:00Z",
  restartCount: 3, restartPolicy: "unless-stopped", image: "nginx:1.27", imageId: "sha256:a", command: ["nginx"], entrypoint: [],
  workingDir: "", user: "", memoryLimit: 268435456, cpus: 0, mounts: [{ type: "volume", source: "web-data", destination: "/data", rw: true }],
  networks: ["shop_default"], health: { status: "", lastOutput: "" }, env: [{ name: "DB_PASSWORD", value: "••••••" }, { name: "MODE", value: "prod" }],
  labels: {}, logs: "starting\nout of memory", metrics: [{ time: "t", cpu: 1, mem: 260000000, memLimit: 268435456 }] };

describe("crash snapshots", () => {
  it("lists a workload's crashes and opens one", async () => {
    const { user } = renderApp("/workloads/dep~d-shop?tab=crashes", { api: {
      "GET /crash-snapshots": [snap], "GET /crash-snapshots/:id": { ...snap, data },
    } });
    expect(await screen.findByRole("tab", { name: /crashes\s*1/i }, { timeout: 10_000 })).toBeInTheDocument();
    expect(await screen.findByText("killed for using too much memory (OOM)")).toBeInTheDocument();
    expect(screen.getByText("+2 more")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /open the crash of shop-web-1/i }));
    const d = await dialog(/shop-web-1/);
    expect(await within(d).findByText(/out of memory/)).toBeInTheDocument();
    expect(within(d).getByText("••••••")).toBeInTheDocument();
    expect(within(d).getByText("web-data → /data")).toBeInTheDocument();
    expect(requests("GET", "/crash-snapshots")[0].query.workload).toBe("dep~d-shop");
  });
});
