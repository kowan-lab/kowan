import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { reply, requests } from "../../test/api";
import { currentPath, pageRoot, renderApp } from "../../test/render";
import { choose, dialog, press, toast } from "../../test/ui";
import { daysLeft, duration, ms, pct } from "../Uptime";

const NOW = new Date().toISOString();
const soon = new Date(Date.now() + 5 * 86_400_000).toISOString();
const check = (over: Record<string, unknown> = {}) => ({
  id: "u1", name: "Shop", kind: "http", target: "https://shop.example.com/health", method: "GET", expectStatus: "200-399",
  keyword: "", verifyTls: true, intervalSec: 60, timeoutSec: 10, failThreshold: 2, deploymentId: "", channelId: "",
  level: "critical", enabled: true, public: true, createdAt: NOW, updatedAt: NOW, status: "up", statusSince: NOW,
  lastCheckedAt: NOW, lastLatencyMs: 120, lastCode: 200, lastError: "", certExpiresAt: soon,
  uptime24h: 99.5, uptime7d: 99.9, uptime30d: 100, latency24h: 118.4,
  recent: [{ at: NOW, ok: true, latencyMs: 120, code: 200, error: "" }, { at: NOW, ok: false, latencyMs: 10, code: 500, error: "HTTP 500" }],
  ...over,
});
const checks = [
  check(),
  check({ id: "u2", name: "DB port", kind: "tcp", target: "db:5432", status: "down", lastError: "connection refused", certExpiresAt: "", uptime24h: null, latency24h: null, recent: [] }),
  check({ id: "u3", name: "Old API", status: "paused", enabled: false, certExpiresAt: "2020-01-01T00:00:00Z" }),
];
const results = Array.from({ length: 5 }, (_, i) => ({ at: new Date(Date.now() - i * 60_000).toISOString(), ok: i !== 2, latencyMs: 100 + i, code: 200, error: "" }));
const api = {
  "GET /uptime/checks": checks,
  "GET /uptime/checks/:id/results": results,
  "GET /uptime/incidents": [
    { id: "i1", checkId: "u1", startedAt: NOW, endedAt: "", error: "HTTP 502" },
    { id: "i2", checkId: "u1", startedAt: "2026-10-20T10:00:00Z", endedAt: "2026-10-20T10:05:30Z", error: "" },
  ],
  "GET /uptime/status-page": { enabled: false, title: "Service status", description: "" },
};

async function ready(extra: Record<string, unknown> = {}, role?: "viewer" | "operator") {
  const r = renderApp("/uptime", { api: { ...api, ...extra } as never, role });
  await within(pageRoot()).findByText("DB port", {}, { timeout: 10_000 });
  return r;
}

describe("uptime helpers", () => {
  it("formats percentages, latency, days and durations", () => {
    expect(pct(null)).toBe("—");
    expect(pct(99.999)).toBe("100%");
    expect(pct(99.5)).toBe("99.50%");
    expect(ms(undefined)).toBe("—");
    expect(ms(1500)).toBe("1.50 s");
    expect(ms(42.4)).toBe("42 ms");
    expect(daysLeft("")).toBeNull();
    expect(daysLeft("nope")).toBeNull();
    expect(daysLeft(soon)).toBe(4);
    expect(duration("2026-01-01T00:00:00Z", "2026-01-01T00:00:30Z")).toBe("30s");
    expect(duration("2026-01-01T00:00:00Z", "2026-01-01T00:02:05Z")).toBe("2m 5s");
    expect(duration("2026-01-01T00:00:00Z", "2026-01-01T03:30:00Z")).toBe("3h 30m");
    expect(duration("2026-01-01T00:00:00Z", "2026-01-03T05:00:00Z")).toBe("2d 5h");
  });
});

describe("uptime page", () => {
  it("lists checks with status, uptime, latency, beats and certificates", async () => {
    await ready();
    expect(screen.getByText(/1 check is down/i)).toBeInTheDocument();
    expect(screen.getByText("tcp db:5432")).toBeInTheDocument();
    expect(screen.getAllByText("99.50%").length).toBeGreaterThan(0);
    expect(screen.getAllByText("118 ms").length).toBeGreaterThan(0);
    expect(screen.getByRole("img", { name: "Shop: 1 of 2 recent probes passed" })).toBeInTheDocument();
    expect(screen.getByText("4 days")).toBeInTheDocument();
    expect(screen.getByText("expired")).toBeInTheDocument();
    expect(screen.getByText("Paused")).toBeInTheDocument();
    expect(screen.getByText("no probes yet")).toBeInTheDocument();
  });

  it("creates an HTTP check and a TCP check", async () => {
    const { user } = await ready({ "POST /uptime/checks": (r: { body: { name: string; intervalSec: number } }) => check({ ...r.body, id: "u9" }) });
    await press(user, /new check/i, pageRoot());
    let d = await dialog(/new uptime check/i);
    await press(user, /create check/i, d);
    expect(await within(d).findByText(/name is required/i)).toBeInTheDocument();
    await user.type(within(d).getByLabelText(/^name/i), "Blog");
    await press(user, /create check/i, d);
    expect(await within(d).findByText(/url is required/i)).toBeInTheDocument();
    await user.type(within(d).getByLabelText(/^url/i), "https://blog.example.com");
    await user.type(within(d).getByLabelText(/page contains/i), "Welcome");
    await choose(user, /^method/i, /^HEAD$/, d);
    await choose(user, /^interval/i, /every 5 minutes/i, d);
    await choose(user, /^notify/i, /ops-hook/i, d);
    await choose(user, /alert level/i, /warning/i, d);
    await choose(user, /^workload/i, /^shop$/i, d);
    await user.click(within(d).getByRole("switch", { name: /verify tls/i }));
    await user.click(within(d).getByRole("switch", { name: /show on the status page/i }));
    await press(user, /create check/i, d);
    await toast(/check created/i);
    expect(requests("POST", "/uptime/checks")[0].body).toMatchObject({
      name: "Blog", kind: "http", target: "https://blog.example.com", keyword: "Welcome", method: "HEAD", intervalSec: 300,
      channelId: "ch-ops", level: "warning", deploymentId: "d-shop", verifyTls: false, public: true,
    });

    await press(user, /new check/i, pageRoot());
    d = await dialog(/new uptime check/i);
    await user.click(within(d).getByRole("tab", { name: /tcp port/i }));
    expect(within(d).queryByLabelText(/page contains/i)).toBeNull();
    await user.type(within(d).getByLabelText(/^name/i), "Redis");
    await press(user, /create check/i, d);
    expect(await within(d).findByText(/host and port are required/i)).toBeInTheDocument();
    await user.type(within(d).getByLabelText(/host and port/i), "redis:6379");
    await press(user, /create check/i, d);
    await waitFor(() => expect(requests("POST", "/uptime/checks")).toHaveLength(2));
    expect(requests("POST", "/uptime/checks")[1].body).toMatchObject({ kind: "tcp", target: "redis:6379" });
  });

  it("shows a server validation error in the form", async () => {
    const { user } = await ready({ "PUT /uptime/checks/:id": reply(400, { error: "timeout must be shorter than the interval" }) });
    await press(user, "Edit Shop");
    const d = await dialog(/edit shop/i);
    expect(within(d).getByLabelText(/^url/i)).toHaveValue("https://shop.example.com/health");
    await press(user, /^save$/i, d);
    expect(await within(d).findByText(/timeout must be shorter/i)).toBeInTheDocument();
  });

  it("checks now, deletes and reports failures", async () => {
    const { user } = await ready({
      "POST /uptime/checks/:id/run": (r: { params: Record<string, string> }) =>
        r.params.id === "u1" ? { ok: true, latencyMs: 87, code: 200, error: "", check: check() } : { ok: false, latencyMs: 2, code: 0, error: "connection refused", check: checks[1] },
      "DELETE /uptime/checks/:id": reply(204),
    });
    const rows = screen.getAllByRole("button", { name: /check now/i });
    await user.click(rows[0]);
    await toast(/answered in 87 ms/i);
    await user.click(rows[1]);
    await toast(/connection refused/i);
    await press(user, "Delete DB port");
    await press(user, /^delete$/i, await dialog(/delete check/i));
    await waitFor(() => expect(requests("DELETE", "/uptime/checks/u2")).toHaveLength(1));
  });

  it("reports failed actions", async () => {
    const { user } = await ready({ "POST /uptime/checks/:id/run": reply(404, { error: "uptime check not found" }), "DELETE /uptime/checks/:id": reply(500, { error: "locked" }) });
    await user.click(screen.getAllByRole("button", { name: /check now/i })[0]);
    await toast(/uptime check not found/i);
    await press(user, "Delete Shop");
    await press(user, /^delete$/i, await dialog(/delete check/i));
    await toast(/locked/i);
  });

  it("opens a check with its response times and incidents", async () => {
    const { user } = await ready();
    await user.click(screen.getByRole("button", { name: "Shop" }));
    const d = await dialog("Shop");
    expect(await within(d).findByText("Ongoing")).toBeInTheDocument();
    expect(within(d).getByText(/lasted 5m 30s/)).toBeInTheDocument();
    expect(within(d).getByText(/4 days left/)).toBeInTheDocument();
    await choose(user, "Period", /last 7 days/i, d);
    await waitFor(() => expect(requests("GET", "/uptime/checks/u1/results").at(-1)!.query).toEqual({ hours: "168" }));
  });

  it("explains a check without enough data", async () => {
    const { user } = await ready({ "GET /uptime/checks/:id/results": [], "GET /uptime/incidents": [] });
    await user.click(screen.getByRole("button", { name: "DB port" }));
    const d = await dialog("DB port");
    expect(await within(d).findByText(/not enough probes/i)).toBeInTheDocument();
    expect(within(d).getByText(/no outages recorded/i)).toBeInTheDocument();
  });

  it("saves the status page settings", async () => {
    const { user } = await ready({ "PUT /uptime/status-page": (r: { body: object }) => r.body });
    await user.click(await screen.findByRole("switch", { name: /public status page/i }));
    await user.clear(screen.getByLabelText(/^title/i));
    await user.type(screen.getByLabelText(/^title/i), "Acme status");
    await user.type(screen.getByLabelText(/^description/i), "Shop and APIs");
    await press(user, /^save$/i, pageRoot());
    await toast(/status page saved/i);
    expect(requests("PUT", "/uptime/status-page")[0].body).toEqual({ enabled: true, title: "Acme status", description: "Shop and APIs" });
    expect(await screen.findByRole("link", { name: /open .*\/status/i })).toBeInTheDocument();
  });

  it("reports a refused status page save", async () => {
    const { user } = await ready({ "PUT /uptime/status-page": reply(400, { error: "title is too long" }) });
    await user.click(await screen.findByRole("switch", { name: /public status page/i }));
    await press(user, /^save$/i, pageRoot());
    await toast(/title is too long/i);
  });

  it("is read-only for viewers and handles empty and error states", async () => {
    await ready({}, "viewer");
    expect(screen.queryByRole("button", { name: /new check/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /check now/i })).toBeNull();
    expect(screen.queryByRole("switch", { name: /public status page/i })).toBeNull();
  });

  it("shows the empty state", async () => {
    renderApp("/uptime", { api: { "GET /uptime/checks": [] } });
    expect(await screen.findByText(/no checks yet/i, {}, { timeout: 10_000 })).toBeInTheDocument();
  });

  it("shows a load error", async () => {
    renderApp("/uptime", { api: { "GET /uptime/checks": reply(500, { error: "db gone" }) } });
    expect(await screen.findByText(/db gone/i, {}, { timeout: 10_000 })).toBeInTheDocument();
  });
});

describe("public status page", () => {
  const status = {
    title: "Acme status", description: "Shop and APIs", overall: "degraded", generatedAt: NOW,
    checks: [
      { id: "u1", name: "Shop", status: "up", statusSince: NOW, uptime30d: 99.95, latencyMs: 120, days: [{ date: new Date().toISOString().slice(0, 10), uptime: 100, probes: 10 }, { date: new Date(Date.now() - 86_400_000).toISOString().slice(0, 10), uptime: 96, probes: 10 }, { date: new Date(Date.now() - 2 * 86_400_000).toISOString().slice(0, 10), uptime: 50, probes: 10 }] },
      { id: "u2", name: "API", status: "down", statusSince: NOW, uptime30d: 90, latencyMs: null, days: [] },
      { id: "u3", name: "New", status: "unknown", statusSince: "", uptime30d: null, latencyMs: null, days: [] },
    ],
    incidents: [
      { id: "i1", checkId: "u2", name: "API", startedAt: NOW, endedAt: "" },
      { id: "i2", checkId: "u1", name: "Shop", startedAt: "2026-10-20T10:00:00Z", endedAt: "2026-10-20T10:01:00Z" },
    ],
  };

  it("renders without signing in", async () => {
    renderApp("/status", { role: null, api: { "GET /public/status": status } });
    expect(await screen.findByRole("heading", { name: "Acme status" }, { timeout: 10_000 })).toBeInTheDocument();
    expect(screen.getByText("Some systems are down")).toBeInTheDocument();
    expect(screen.getByText("Operational")).toBeInTheDocument();
    expect(screen.getAllByText("Down").length).toBeGreaterThan(0);
    expect(screen.getByText("Pending")).toBeInTheDocument();
    expect(screen.getByText(/99.95% over 30 days · 120 ms/)).toBeInTheDocument();
    expect(screen.getByText("ongoing")).toBeInTheDocument();
    expect(screen.getByText(/down for 1m 0s/)).toBeInTheDocument();
    expect(currentPath()).toBe("/status");
  });

  it("explains when there is no status page and when all is fine", async () => {
    renderApp("/status", { role: null, api: { "GET /public/status": reply(404, { error: "status page is not enabled" }) } });
    expect(await screen.findByText(/does not publish a status page/i, {}, { timeout: 10_000 })).toBeInTheDocument();
  });

  it("says when there are no incidents", async () => {
    renderApp("/status", { role: null, api: { "GET /public/status": { ...status, overall: "up", incidents: [], description: "" } } });
    expect(await screen.findByText(/all systems operational/i, {}, { timeout: 10_000 })).toBeInTheDocument();
    expect(screen.getByText(/no outages recently/i)).toBeInTheDocument();
  });
});

describe("uptime on a workload", () => {
  it("lists the checks linked to the deployment", async () => {
    renderApp("/workloads/dep~d-shop", { api: { "GET /uptime/checks": [check({ deploymentId: "d-shop" }), check({ id: "u7", name: "Other", deploymentId: "d-api" })] } });
    expect(await screen.findByText("Checks linked to this workload.", {}, { timeout: 10_000 })).toBeInTheDocument();
    expect(screen.getByText("https://shop.example.com/health")).toBeInTheDocument();
    expect(screen.queryByText("Other")).toBeNull();
  });
});
