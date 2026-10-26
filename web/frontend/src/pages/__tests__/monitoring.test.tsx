import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { mockApi, reply, requests } from "../../test/api";
import { pageRoot, renderApp } from "../../test/render";
import { choose, dialog, press, toast } from "../../test/ui";

const NOW = "2026-10-26T10:00:00Z";
const alerts = [
  { id: "a1", ruleId: "r1", hostId: "local", level: "critical", title: "Container down", message: "shop-db-1 stopped", resource: "shop-db-1", channelId: "", resolved: false, createdAt: NOW },
  { id: "a2", ruleId: "r2", hostId: "h-edge", level: "warning", title: "CPU usage", message: "92% for 5 min", resource: "", channelId: "", resolved: true, createdAt: NOW },
];
const rules = [
  { id: "r1", name: "db down", type: "container_down", operator: "", threshold: 0, severity: "critical", forDuration: 0, hostId: "", match: "db", labelKey: "", labelValue: "", channelId: "ch-ops", autoResolve: true, enabled: true, createdAt: NOW },
  { id: "r2", name: "hot cpu", type: "cpu_high", operator: ">", threshold: 90, severity: "", forDuration: 300, hostId: "local", match: "", labelKey: "app", labelValue: "web", channelId: "", autoResolve: false, enabled: false, createdAt: NOW },
];

describe("monitoring page", () => {
  it("lists alerts and resolves an open one", async () => {
    const { user } = renderApp("/monitoring", { api: { "GET /alerts": alerts } });
    expect(await within(pageRoot()).findByText("shop-db-1 stopped", {}, { timeout: 10_000 })).toBeInTheDocument();
    expect(screen.getByText("edge-1")).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "Resolve alert" })).toHaveLength(1);
    await press(user, "Resolve alert");
    await toast(/alert resolved/i);
    expect(requests("POST", "/alerts/a1/resolve")).toHaveLength(1);
  });

  it("reports resolve and load failures", async () => {
    const { user } = renderApp("/monitoring", { api: { "GET /alerts": alerts, "POST /alerts/:id/resolve": reply(404, { error: "alert not found" }) } });
    await within(pageRoot()).findByText("shop-db-1 stopped", {}, { timeout: 10_000 });
    await press(user, "Resolve alert");
    await toast(/alert not found/i);
  });

  it("shows the alerts error and empty states", async () => {
    renderApp("/monitoring", { api: { "GET /alerts": reply(500, { error: "x" }) } });
    expect(await screen.findByText(/failed to load alerts/i, {}, { timeout: 10_000 })).toBeInTheDocument();
  });

  it("shows no alerts", async () => {
    renderApp("/monitoring");
    expect(await screen.findByText(/^no alerts$/i, {}, { timeout: 10_000 })).toBeInTheDocument();
  });

  it("creates a numeric, container-scoped rule", async () => {
    const { user } = renderApp("/monitoring", { api: { "GET /alert-rules": rules } });
    mockApi({ "POST /alert-rules": (r) => ({ id: "r3", ...(r.body as object) }) });
    await user.click(await screen.findByRole("tab", { name: /alert rules/i }, { timeout: 10_000 }));
    expect(await screen.findByText("hot cpu")).toBeInTheDocument();
    expect(screen.getByText("> 90%")).toBeInTheDocument();
    expect(screen.getByText('name~"db"')).toBeInTheDocument();
    await press(user, /new rule|new alert rule|add rule/i, pageRoot());
    const d = await dialog(/new alert rule/i);
    await press(user, /^create$/i, d);
    expect(await within(d).findByText(/name is required/i)).toBeInTheDocument();
    await user.type(within(d).getByLabelText(/^name/i), "mem web");
    await choose(user, /condition type/i, /memory usage/i, d);
    await choose(user, /^operator/i, /at least/i, d);
    const thr = within(d).getByLabelText(/threshold/i);
    await user.clear(thr);
    await user.type(thr, "75");
    await choose(user, /severity/i, /critical/i, d);
    await choose(user, /sustained for/i, /5 min/i, d);
    await choose(user, /^host/i, /^local$/i, d);
    await user.type(within(d).getByLabelText(/container name filter/i), "web");
    await user.type(within(d).getByLabelText(/label key/i), "app");
    await choose(user, /notification channel/i, /ops-hook/i, d);
    await press(user, /^create$/i, d);
    await toast(/rule created/i);
    expect(requests("POST", "/alert-rules")[0].body).toMatchObject({
      name: "mem web", type: "mem_high", operator: ">=", threshold: 75, severity: "critical", forDuration: 300,
      hostId: "local", match: "web", labelKey: "app", channelId: "ch-ops",
    });
  });

  it("a non-numeric type hides operator and threshold", async () => {
    const { user } = renderApp("/monitoring", { api: { "GET /alert-rules": rules } });
    await user.click(await screen.findByRole("tab", { name: /alert rules/i }, { timeout: 10_000 }));
    await press(user, /new rule|new alert rule|add rule/i, pageRoot());
    const d = await dialog(/new alert rule/i);
    await choose(user, /condition type/i, /container down/i, d);
    expect(within(d).queryByLabelText(/threshold/i)).toBeNull();
    await choose(user, /condition type/i, /docker disk usage/i, d);
    expect(within(d).getByLabelText(/threshold \(GB\)/i)).toBeInTheDocument();
    expect(within(d).queryByLabelText(/container name filter/i)).toBeNull();
  });

  it("toggles, edits and deletes rules, reporting failures", async () => {
    const { user } = renderApp("/monitoring", { api: { "GET /alert-rules": rules } });
    mockApi({ "PUT /alert-rules/:id": (r) => ({ ...rules[0], ...(r.body as object) }) });
    await user.click(await screen.findByRole("tab", { name: /alert rules/i }, { timeout: 10_000 }));
    await screen.findByText("hot cpu");
    const switches = screen.getAllByRole("switch", { name: "Toggle rule enabled" });
    await user.click(switches[1]);
    await waitFor(() => expect(requests("PUT", "/alert-rules/r2")[0]?.body).toMatchObject({ enabled: true }));

    await user.click(screen.getAllByRole("button", { name: "Edit rule" })[0]);
    const d = await dialog(/edit alert rule/i);
    expect(within(d).getByLabelText(/^name/i)).toHaveValue("db down");
    await user.type(within(d).getByLabelText(/^name/i), "!");
    await press(user, /^save$/i, d);
    await toast(/rule updated/i);
    expect(requests("PUT", "/alert-rules/r1")[0].body).toMatchObject({ name: "db down!", type: "container_down" });

    await user.click(screen.getAllByRole("button", { name: "Delete rule" })[1]);
    await press(user, /^delete$/i, await dialog(/delete alert rule/i));
    await toast(/rule deleted/i);
    expect(requests("DELETE", "/alert-rules/r2")).toHaveLength(1);

    mockApi({ "PUT /alert-rules/:id": reply(500, { error: "db locked" }), "DELETE /alert-rules/:id": reply(500, { error: "gone wrong" }) });
    await user.click(screen.getAllByRole("switch", { name: "Toggle rule enabled" })[0]);
    await toast(/db locked/i);
    await user.click(screen.getAllByRole("button", { name: "Delete rule" })[0]);
    await press(user, /^delete$/i, await dialog(/delete alert rule/i));
    await toast(/gone wrong/i);
  });

  it("reports a create failure", async () => {
    const { user } = renderApp("/monitoring", { api: { "GET /alert-rules": [], "POST /alert-rules": reply(400, { error: "threshold out of range" }) } });
    await user.click(await screen.findByRole("tab", { name: /alert rules/i }, { timeout: 10_000 }));
    expect(await screen.findByText(/no alert rules/i)).toBeInTheDocument();
    await user.click(screen.getAllByRole("button", { name: /new rule|new alert rule|add rule/i })[0]);
    const d = await dialog(/new alert rule/i);
    await user.type(within(d).getByLabelText(/^name/i), "x");
    await press(user, /^create$/i, d);
    await toast(/threshold out of range/i);
  });
});
