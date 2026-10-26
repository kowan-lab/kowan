// The overview: "Needs you" cards built from hosts, containers, deployments,
// alerts and image updates, the hosts table and the recent activity feed.
import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { reply, requests } from "../../test/api";
import { containers, deployments, hosts } from "../../test/fixtures";
import { currentPath, pageRoot, renderApp } from "../../test/render";
import { press, toast } from "../../test/ui";

const now = Date.now();
const iso = (agoSec: number) => new Date(now - agoSec * 1000).toISOString();
const ctr = (id: string, name: string, state: string, status: string, labels: Record<string, string> = {}) => ({
  ...containers[0], id: id.padEnd(64, "0"), names: [`/${name}`], state, status, labels, ports: [],
});
const fleet = [
  ctr("c-a", "shop-web-1", "running", "Up 3 hours", { "com.docker.compose.project": "shop", "com.docker.compose.service": "web" }),
  ctr("c-b", "shop-worker-1", "exited", "Exited (137) 5 minutes ago", { "com.docker.compose.project": "shop", "com.docker.compose.service": "worker" }),
  ctr("c-c", "api", "running", "Up 2 minutes (unhealthy)", { "kowan.deployment.id": "d-api", "kowan.deployment.name": "api" }),
  ctr("c-d", "zombie", "dead", "Dead"),
  ctr("c-e", "flappy", "restarting", "Restarting (1) Less than a second ago"),
  ctr("c-f", "done-job", "exited", "Exited (0) 2 days ago"),
];
const alerts = [
  { id: "al-1", ruleId: "r", hostId: "local", level: "critical", title: "Disk almost full", message: "92% used", resource: "", channelId: "", resolved: false, createdAt: iso(120) },
  { id: "al-2", ruleId: "r", hostId: "local", level: "warning", title: "api unhealthy", message: "", resource: "api", channelId: "", resolved: false, createdAt: iso(60) },
  { id: "al-3", ruleId: "r", hostId: "local", level: "info", title: "Backup slow", message: "", resource: "", channelId: "", resolved: true, createdAt: iso(3600) },
];
const api = {
  "GET /hosts": hosts,
  "GET /hosts/:id/containers": fleet,
  "GET /deployments": [{ ...deployments[0], status: "error", updatedAt: iso(300) }, deployments[1]],
  "GET /alerts": alerts,
  "GET /hosts/:id/metrics": [{ hostId: "local", time: iso(30), running: 3, total: 6, images: 3, cpuPercent: 42.5, memUsed: 6 * 2 ** 30, memTotal: 16 * 2 ** 30 }],
  "GET /hosts/:id/updates": [{ containerId: fleet[0].id, containerName: "shop-web-1", image: "nginx:1.27", localDigest: "a", remoteDigest: "b", updateAvailable: true, checkedAt: iso(10) }],
  "GET /hosts/:id/events": [
    { id: "e1", time: iso(40), type: "container", action: "die", actorId: fleet[1].id, actorName: "shop-worker-1", image: "", attributes: { exitCode: "137" } },
    { id: "e2", time: iso(50), type: "container", action: "exec_start", actorId: fleet[0].id, actorName: "shop-web-1", image: "", attributes: {} },
  ],
  "GET /audit": {
    total: 3,
    items: [
      { id: "au1", time: iso(70), userId: "u", username: "root", action: "POST", path: "/api/v1/deployments/d-shop/deploy", status: 200, ip: "10.0.0.9" },
      { id: "au2", time: iso(80), userId: "u", username: "ops", action: "DELETE", path: "/api/v1/hosts/local/volumes/old", status: 409, ip: "10.0.0.8" },
      { id: "au3", time: iso(90), userId: "u", username: "root", action: "POST", path: "/api/v1/auth/login", status: 200, ip: "1.1.1.1" },
    ],
  },
};

async function ready() {
  return within(pageRoot()).findByText(/shop-worker-1 exited 137|worker exited 137/i, {}, { timeout: 10_000 });
}

describe("dashboard", () => {
  it("lists what needs attention, worst first", async () => {
    const { user } = renderApp("/", { api });
    await ready();
    expect(screen.getByText(/zombie is dead/i)).toBeInTheDocument();
    expect(screen.getAllByText(/api unhealthy/i).length).toBeGreaterThan(0);
    expect(screen.getByText(/flappy is restarting/i)).toBeInTheDocument();
    expect(screen.getByText(/shop: deploy failed/i)).toBeInTheDocument();
    expect(screen.getAllByText("Disk almost full").length).toBeGreaterThan(0);
    // A clean exit raises nothing (the resolved alert only shows in the feed).
    expect(screen.queryByText(/done-job/)).toBeNull();
    const more = screen.queryByRole("button", { name: /show all \d+/i });
    if (more) {
      await user.click(more);
      expect(await screen.findByRole("button", { name: /show fewer/i })).toBeInTheDocument();
    }
    expect(await screen.findByText(/newer image digests for/i)).toBeInTheDocument();
  });

  it("restarts a container and resolves an alert from the cards", async () => {
    const { user } = renderApp("/", { api: { ...api, "POST /hosts/:h/containers/:id/restart": {}, "POST /alerts/:id/resolve": {} } });
    await ready();
    await user.click(screen.getAllByRole("button", { name: /^restart$/i })[0]);
    await toast(/^restarted$/i);
    expect(requests("POST", "/hosts/local/containers/:id/restart")).toHaveLength(1);
    await user.click(screen.getAllByRole("button", { name: /^resolve$/i })[0]);
    await toast(/alert resolved/i);
  });

  it("reports failed card actions", async () => {
    const { user } = renderApp("/", {
      api: { ...api, "POST /hosts/:h/containers/:id/restart": reply(500, { error: "daemon busy" }), "POST /alerts/:id/resolve": reply(404, { error: "alert gone" }) },
    });
    await ready();
    await user.click(screen.getAllByRole("button", { name: /^restart$/i })[0]);
    await toast(/daemon busy/i);
    await user.click(screen.getAllByRole("button", { name: /^resolve$/i })[0]);
    await toast(/alert gone/i);
  });

  it("follows a card's link", async () => {
    const { user } = renderApp("/", { api });
    await ready();
    await user.click(screen.getAllByRole("link", { name: /open logs/i })[0]);
    await waitFor(() => expect(currentPath()).toMatch(/tab=logs/));
  });

  it("shows the recent feed with alerts, events and audit lines", async () => {
    renderApp("/", { api });
    await ready();
    expect(await screen.findByText(/deployed/i)).toBeInTheDocument();
    expect(screen.getByText(/failed · HTTP 409/)).toBeInTheDocument();
    // Logins and exec noise are left out.
    expect(screen.queryByText(/1\.1\.1\.1/)).toBeNull();
  });

  it("aggregates all hosts with an offline one", async () => {
    localStorage.setItem("kowan_host", "all");
    const { user } = renderApp("/", { api });
    expect(await screen.findByText(/edge-1 is offline/i, {}, { timeout: 10_000 })).toBeInTheDocument();
    const rows = screen.getAllByText("edge-1");
    await user.click(rows[rows.length - 1]);
    await waitFor(() => expect(localStorage.getItem("kowan_host")).not.toBe("all"));
  });

  it("shows a calm overview when nothing is wrong", async () => {
    renderApp("/", {
      api: {
        "GET /hosts": [hosts[0]],
        "GET /hosts/:id/containers": [fleet[0]],
        "GET /deployments": [],
        "GET /alerts": [],
        "GET /hosts/:id/updates": [],
        "GET /hosts/:id/events": [],
        "GET /audit": { items: [], total: 0 },
      },
    });
    expect(await screen.findByText(/nothing happened here in the last few hours/i, {}, { timeout: 10_000 })).toBeInTheDocument();
  });

  it("shows the offline banner for a single offline host", async () => {
    localStorage.setItem("kowan_host", "h-edge");
    renderApp("/", { api });
    expect(await screen.findAllByText(/offline/i, {}, { timeout: 10_000 })).not.toHaveLength(0);
    void press;
  });
});
