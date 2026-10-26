import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { mockApi, reply, requests } from "../../test/api";
import { currentPath, pageRoot, renderApp } from "../../test/render";
import { choose, dialog, press, toast } from "../../test/ui";

const NOW = "2026-10-26T10:00:00Z";
const schedules = [
  { id: "s1", name: "night stop", targetType: "container", targetId: "shop-web-1", hostId: "local", action: "stop", cron: "0 22 * * 1-5", timezone: "Europe/Rome",
    enabled: true, onlyIf: { noExecSession: true }, source: "manual", nextRun: "2026-10-26T21:00:00Z", lastRunAt: NOW, lastStatus: "success", createdAt: NOW, updatedAt: NOW },
  { id: "s2", name: "weekly deploy", targetType: "deployment", targetId: "d-shop", hostId: "", action: "deploy", cron: "0 4 * * 0", timezone: "",
    enabled: false, onlyIf: {}, source: "manual", nextRun: "", lastRunAt: "", lastStatus: "", createdAt: NOW, updatedAt: NOW },
  { id: "s3", name: "label start", targetType: "stack", targetId: "shop", hostId: "local", action: "start", cron: "bad", timezone: "",
    enabled: true, onlyIf: {}, source: "label", error: "invalid cron expression", nextRun: "", lastRunAt: "", lastStatus: "", createdAt: NOW, updatedAt: NOW },
];
const runs = [{ id: "r1", scheduleId: "s1", scheduleName: "night stop", action: "stop", targetId: "shop-web-1", trigger: "cron", status: "skipped", message: "exec session open", startedAt: NOW }];
const api = { "GET /schedules": schedules, "GET /schedules/runs": runs, "POST /schedules/preview": { nextRuns: ["2026-10-27T03:00:00Z"] } };

async function ready() {
  return within(pageRoot()).findByText("weekly deploy", {}, { timeout: 10_000 });
}

describe("schedules page", () => {
  it("lists schedules, label ones read-only, and the run history", async () => {
    const { user } = renderApp("/schedules", { api });
    await ready();
    expect(screen.getByText("invalid cron expression")).toBeInTheDocument();
    expect(screen.getByText("exec session open")).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "Edit" })).toHaveLength(2);
    await user.click(screen.getByText("deployment · d-shop"));
    await waitFor(() => expect(currentPath()).toBe("/workloads/dep~d-shop"));
  });

  it("runs, toggles, deletes and rescans", async () => {
    const { user } = renderApp("/schedules", { api: { ...api, "POST /schedules/:id/run": { ...runs[0], status: "failed", message: "container gone" } } });
    await ready();
    await user.click(screen.getAllByRole("button", { name: "Run now" })[0]);
    await toast(/container gone/i);
    await press(user, "Enable");
    await waitFor(() => expect(requests("PUT", "/schedules/s2")[0]?.body).toEqual({ enabled: true }));
    await press(user, /rescan labels/i, pageRoot());
    await waitFor(() => expect(requests("POST", "/schedules/rescan")).toHaveLength(1));
    await user.click(screen.getAllByRole("button", { name: "Delete" })[0]);
    await press(user, /^delete$/i, await dialog(/delete schedule/i));
    await waitFor(() => expect(requests("DELETE", "/schedules/s1")).toHaveLength(1));
  });

  it("reports failures", async () => {
    const { user } = renderApp("/schedules", {
      api: { ...api, "POST /schedules/:id/run": reply(500, { error: "engine down" }), "PUT /schedules/:id": reply(400, { error: "nope" }), "DELETE /schedules/:id": reply(500, { error: "locked" }) },
    });
    await ready();
    await user.click(screen.getAllByRole("button", { name: "Run now" })[0]);
    await toast(/engine down/i);
    await press(user, "Disable");
    await toast(/nope/i);
    await user.click(screen.getAllByRole("button", { name: "Delete" })[0]);
    await press(user, /^delete$/i, await dialog(/delete schedule/i));
    await toast(/locked/i);
  });

  it("creates a stack schedule", async () => {
    const { user } = renderApp("/schedules", { api: { ...api, "POST /schedules": (r) => ({ ...schedules[0], ...r.body, id: "s-st" }) } });
    await ready();
    await press(user, /new schedule/i, pageRoot());
    const d = await dialog(/new schedule/i);
    await choose(user, /target type/i, /^stack$/i, d);
    await choose(user, /^target$/i, /^shop$/i, d);
    await user.click(within(d).getByRole("button", { name: /^create|^save/i }));
    await toast(/schedule saved/i);
    expect(requests("POST", "/schedules")[0].body).toMatchObject({ targetType: "stack", targetId: "shop", onlyIf: {} });
  });

  it("creates a container schedule with a preset and preview", async () => {
    const { user } = renderApp("/schedules", { api: { ...api, "POST /schedules": (r) => ({ ...schedules[0], ...r.body, id: "s-new" }) } });
    await ready();
    await press(user, /new schedule/i, pageRoot());
    const d = await dialog(/new schedule/i);
    expect(within(d).getByRole("button", { name: /^create|^save/i })).toBeDisabled();
    await choose(user, /^target$/i, /shop-db-1/i, d);
    await choose(user, /^action/i, /^restart$/i, d);
    await press(user, /every day 03:00/i, d);
    expect(await within(d).findByText(/^next:/i)).toBeInTheDocument();
    await user.click(within(d).getByRole("switch", { name: /only if no exec session/i }));
    await user.type(within(d).getByLabelText(/^name/i), "db restart");
    await user.click(within(d).getByRole("button", { name: /^create|^save/i }));
    await toast(/schedule saved/i);
    expect(requests("POST", "/schedules")[0].body).toMatchObject({
      name: "db restart", targetType: "container", targetId: "shop-db-1", action: "restart", cron: "0 3 * * *", onlyIf: { noExecSession: true },
    });
  });

  it("edits a schedule with a fixed target", async () => {
    const { user } = renderApp("/schedules", { api: { ...api, "PUT /schedules/:id": reply(400, { error: "bad target" }) } });
    await ready();
    await user.click(screen.getAllByRole("button", { name: "Edit" })[1]);
    const d = await dialog(/edit schedule/i);
    expect(within(d).getByText("d-shop")).toBeInTheDocument();
    expect(within(d).queryByRole("combobox", { name: /target type/i })).toBeNull();
    await press(user, /every hour/i, d);
    await user.click(within(d).getByRole("button", { name: /^create|^save/i }));
    await toast(/bad target/i);
    expect(requests("PUT", "/schedules/s2")[0].body).toMatchObject({ targetType: "deployment", targetId: "d-shop", cron: "0 * * * *", onlyIf: {} });
  });

  it("shows empty and error states", async () => {
    renderApp("/schedules", { api: { "GET /schedules": [], "GET /schedules/runs": [] } });
    expect(await screen.findByText(/no schedules yet/i, {}, { timeout: 10_000 })).toBeInTheDocument();
  });

  it("shows a load error", async () => {
    renderApp("/schedules", { api: { "GET /schedules": reply(500, { error: "sched broken" }) } });
    expect(await screen.findByText(/sched broken/i, {}, { timeout: 10_000 })).toBeInTheDocument();
  });
});
