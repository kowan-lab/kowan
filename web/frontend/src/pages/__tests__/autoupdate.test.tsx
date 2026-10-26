import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { reply, requests } from "../../test/api";
import catalog from "../../test/catalog.json";
import { currentPath, pageRoot, policy, renderApp } from "../../test/render";
import { choose, press, toast } from "../../test/ui";

const NOW = "2026-10-26T10:00:00Z";
const item = (over: Record<string, unknown>) => {
  const p = policy("apply");
  return {
    kind: "container", hostId: "local", hostName: "local", target: "shop-web-1", name: "shop-web-1", stored: null,
    effective: { ...p.effective, sources: { ...p.effective.sources, mode: "label" }, minAge: "2d", scanBlock: "critical" },
    window: { valid: true, always: true }, lastCheckAt: NOW, lastResult: "updated", lastMessage: "nginx 1.27.1 → 1.27.2",
    available: false, pending: false, image: "nginx:1.27", nextCheckAt: null, last: null, ...over,
  };
};
const overview = {
  settings: catalog.autoupdateSettings, lastTickAt: NOW,
  objects: [
    item({}),
    item({ kind: "deployment", target: "d-api", name: "api", effective: policy("notify").effective, window: { valid: true, always: false, open: false, nextOpen: "2026-10-27T02:00:00Z", describe: "Sun 02:00-04:00" }, lastResult: "", nextCheckAt: "2026-10-26T12:00:00Z" }),
    item({ target: "bad", name: "bad-window", window: { valid: false, error: "invalid window spec" }, lastResult: "rolled_back", lastMessage: "health check failed" }),
  ],
};
const entry = (id: string, outcome: string) => ({
  id, kind: "container", hostId: "local", target: "shop-web-1", targetName: "shop-web-1", image: "nginx:1.27", fromDigest: "sha256:aaa", toDigest: "sha256:bbb",
  mode: "apply", trigger: "auto", outcome, message: `${outcome} message`, scan: { critical: 1, high: 2 }, startedAt: NOW, finishedAt: NOW,
});
const api = {
  "GET /autoupdate/overview": overview,
  "GET /autoupdate/history": (r: { query: Record<string, string> }) => ({
    items: r.query.outcome ? [entry("h9", r.query.outcome)] : [entry("h1", "updated"), entry("h2", "blocked")],
    total: r.query.outcome ? 1 : 40,
  }),
};

async function ready() {
  return within(pageRoot()).findByText("bad-window", {}, { timeout: 10_000 });
}

describe("auto-update page", () => {
  it("shows policies with windows, gates and results", async () => {
    const { user } = renderApp("/auto-update", { api });
    await ready();
    expect(screen.getByText("invalid window spec")).toBeInTheDocument();
    expect(screen.getByText("any time")).toBeInTheDocument();
    expect(screen.getByText(/^next /)).toBeInTheDocument();
    expect(screen.getAllByText("min age 2d").length).toBeGreaterThan(0);
    expect(screen.getByText(/not checked yet · next/)).toBeInTheDocument();
    expect(screen.getAllByText("label").length).toBeGreaterThan(0);
    await user.click(screen.getByRole("link", { name: "api" }));
    await waitFor(() => expect(currentPath()).toBe("/workloads/dep~d-api"));
  });

  it("saves settings as admin", async () => {
    const { user } = renderApp("/auto-update", { api: { ...api, "PUT /autoupdate/settings": (r) => ({ ...catalog.autoupdateSettings, ...r.body }) } });
    await ready();
    await user.click(screen.getByRole("switch", { name: /background checks/i }));
    const interval = screen.getByLabelText(/check interval/i);
    await user.clear(interval);
    await user.type(interval, "12");
    await choose(user, /default scan gate/i, /block on critical or high/i);
    const deadline = screen.getByLabelText(/health verify deadline/i);
    await user.clear(deadline);
    await user.type(deadline, "300");
    await press(user, /save settings/i);
    await toast(/settings saved/i);
    expect(requests("PUT", "/autoupdate/settings")[0].body).toMatchObject({ enabled: false, intervalSeconds: 43200, defaultScanBlock: "high", verifyTimeoutSeconds: 300 });
  });

  it("reports a settings failure and runs a check", async () => {
    const { user } = renderApp("/auto-update", {
      api: { ...api, "PUT /autoupdate/settings": reply(400, { error: "interval too short" }), "POST /autoupdate/check": { started: true } },
    });
    await ready();
    await press(user, /save settings/i);
    await toast(/interval too short/i);
    await press(user, /check all now/i);
    await toast(/checking every host now/i);
    expect(requests("POST", "/autoupdate/check")[0].body).toEqual({});
  });

  it("reports a failed check", async () => {
    const { user } = renderApp("/auto-update", { api: { ...api, "POST /autoupdate/check": reply(409, { error: "check already running" }) } });
    await ready();
    await press(user, /check all now/i);
    await toast(/check already running/i);
  });

  it("pages and filters the history", async () => {
    const { user } = renderApp("/auto-update", { api });
    await ready();
    expect(await screen.findByText("40 events")).toBeInTheDocument();
    expect(screen.getByText("1–25")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Newer" })).toBeDisabled();
    await press(user, "Older");
    expect(await screen.findByText("26–40")).toBeInTheDocument();
    expect(requests("GET", "/autoupdate/history").at(-1)!.query).toMatchObject({ offset: "25" });
    await press(user, "Newer");
    await choose(user, /outcome/i, /rolled back/i);
    expect(await screen.findByText("1 event")).toBeInTheDocument();
    expect(requests("GET", "/autoupdate/history").at(-1)!.query).toMatchObject({ outcome: "rolled_back", offset: "0" });
  });

  it("is read-only for viewers", async () => {
    renderApp("/auto-update", { role: "viewer", api });
    await ready();
    expect(screen.queryByRole("button", { name: /check all now/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /save settings/i })).toBeNull();
    expect(screen.getByRole("switch", { name: /background checks/i })).toBeDisabled();
  });
});
