// The workload "Policies" tab: update knob and details, backup jobs, schedules
// and the domain knob, each opening the shared editors.
import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { reply, requests } from "../../test/api";
import { pageRoot, policy, renderApp } from "../../test/render";
import { choose, dialog, press, toast } from "../../test/ui";

const NOW = "2026-10-26T10:00:00Z";
const job = {
  id: "j-shop", name: "shop volumes", hostId: "local", repoId: "r-s3", selector: { mode: "deployment", deploymentId: "d-shop" }, schedule: "0 3 * * *",
  timezone: "", retention: { keepDaily: 7 }, hooks: { stopContainers: true }, tags: [], enabled: true, lastRunAt: NOW, createdAt: NOW, updatedAt: NOW,
  nextRun: "2026-10-27T03:00:00Z", lastRun: { id: "run-1", status: "success", startedAt: NOW }, running: false,
};
const sched = {
  id: "s-shop", name: "night stop", targetType: "deployment", targetId: "d-shop", hostId: "", action: "stop", cron: "0 22 * * *", timezone: "",
  enabled: true, onlyIf: {}, source: "manual", nextRun: "2026-10-26T22:00:00Z", lastRunAt: NOW, lastStatus: "success", createdAt: NOW, updatedAt: NOW,
};
const repo = { id: "r-s3", name: "offsite", type: "s3", hostId: "", config: {}, secrets: {}, initialized: true, createdAt: NOW, updatedAt: NOW };
const api = {
  "GET /volbackups/jobs": [job],
  "GET /volbackups/repos": [repo],
  "POST /volbackups/jobs/preview": { volumes: ["shop_db"] },
  "GET /volbackups/runs/:id": { id: "run-2", jobId: "j-shop", jobName: "shop volumes", repoId: "r-s3", hostId: "local", kind: "backup", trigger: "manual", status: "running", volumes: [], snapshots: [], stats: {}, bytesAdded: 0, durationSec: 0, error: "", startedAt: NOW, finishedAt: "" },
  "GET /schedules": [sched],
  "GET /deployments/:id/autoupdate": { ...policy("notify"), kind: "deployment", stored: { mode: "notify", window: "", minAge: "", scanBlock: "" } },
  "PUT /deployments/:id/autoupdate": (r: { body: { mode: string } }) => ({ ...policy(r.body.mode), kind: "deployment", stored: { mode: r.body.mode, window: "", minAge: "", scanBlock: "" } }),
};

async function policies(extra: Record<string, unknown> = {}) {
  const r = renderApp("/workloads/dep~d-shop", { api: { ...api, ...extra } as never });
  await screen.findByRole("heading", { name: "shop" }, { timeout: 10_000 });
  await r.user.click(screen.getByRole("tab", { name: /policies/i }));
  await within(pageRoot()).findByRole("combobox", { name: /update policy for shop/i });
  return r;
}

describe("workload policies", () => {
  it("changes the update policy with an explicit selection", async () => {
    const { user } = await policies();
    const knob = screen.getByRole("combobox", { name: /update policy for shop/i });
    await waitFor(() => expect(knob).toHaveValue("notify"));
    await user.selectOptions(knob, "pull");
    await toast(/shop: pull/i);
    expect(requests("PUT", "/deployments/d-shop/autoupdate")[0].body).toMatchObject({ mode: "pull" });
  });

  it("edits the update details, checks now and resets", async () => {
    const { user } = await policies({ "POST /autoupdate/check": { started: true }, "DELETE /deployments/:id/autoupdate": { ...policy(), kind: "deployment" } });
    await press(user, /edit details/i, pageRoot());
    const d = await dialog(/auto-update · shop/i);
    await choose(user, "Scan gate", /block on critical$/i, d);
    await press(user, /save policy/i, d);
    await toast(/policy saved/i);
    expect(requests("PUT", "/deployments/d-shop/autoupdate").at(-1)!.body).toMatchObject({ scanBlock: "critical" });
    await press(user, /check now/i, d);
    await toast(/check started/i);
    await press(user, /reset to defaults/i, d);
    await toast(/stored policy removed/i);
  });

  it("manages backup jobs for the deployment", async () => {
    const { user } = await policies({ "POST /volbackups/jobs/:id/run": { id: "run-2", jobName: "shop volumes" } });
    const knob = screen.getByRole("switch", { name: /backups for shop/i });
    await user.click(knob);
    await toast(/backups paused/i);
    expect(requests("PUT", "/volbackups/jobs/j-shop")[0].body).toEqual({ enabled: false });
    await press(user, /manage jobs/i, pageRoot());
    const d = await dialog(/backups · shop/i);
    expect(await within(d).findByText("shop volumes")).toBeInTheDocument();
    await press(user, /back up now/i, d);
    expect(await screen.findByText(/backup: shop volumes/i)).toBeInTheDocument();
  });

  it("reports a failed backup run and opens the job editor", async () => {
    const { user } = await policies({ "POST /volbackups/jobs/:id/run": reply(409, { error: "repository locked" }) });
    await press(user, /manage jobs/i, pageRoot());
    const d = await dialog(/backups · shop/i);
    await press(user, /back up now/i, await within(d).findByRole("row", { name: /shop volumes/i }));
    await toast(/repository locked/i);
    await press(user, "Edit", d);
    expect(await dialog(/edit shop volumes/i)).toBeInTheDocument();
  });

  it("manages schedules: toggle, run, delete and add", async () => {
    const { user } = await policies({ "POST /schedules/:id/run": { status: "success", message: "stopped shop" }, "POST /schedules": { ...sched, id: "s-2" } });
    await user.click(screen.getByRole("switch", { name: /schedule for shop/i }));
    await toast(/timer off/i);
    expect(requests("PUT", "/schedules/s-shop")[0].body).toEqual({ enabled: false });
    await press(user, /manage schedules/i, pageRoot());
    const d = await dialog(/schedules · shop/i);
    await press(user, "Run now", d);
    await toast(/stopped shop/i);
    await press(user, "Delete", d);
    await press(user, /^delete$/i, await dialog(/delete schedule/i));
    await waitFor(() => expect(requests("DELETE", "/schedules/s-shop")).toHaveLength(1));
    await press(user, /add schedule/i, await dialog(/schedules · shop/i));
    const e = await dialog(/new schedule/i);
    expect(within(e).getByText(/target: deployment/i)).toBeInTheDocument();
    await user.click(within(e).getByRole("button", { name: /^create|^save/i }));
    await waitFor(() => expect(requests("POST", "/schedules")[0]?.body).toMatchObject({ targetType: "deployment", targetId: "d-shop" }));
  });

  it("reports schedule failures", async () => {
    const { user } = await policies({ "PUT /schedules/:id": reply(500, { error: "sched store down" }), "POST /schedules/:id/run": reply(500, { error: "run failed hard" }) });
    await user.click(screen.getByRole("switch", { name: /schedule for shop/i }));
    await toast(/sched store down/i);
    await press(user, /manage schedules/i, pageRoot());
    await press(user, "Run now", await dialog(/schedules · shop/i));
    await toast(/run failed hard/i);
  });

  it("opens the expose dialog from the domain knob", async () => {
    const { user } = await policies({
      "GET /hosts/:id/exposure": { hostId: "local", hostName: "local", ports: [], domains: [], proxies: [], summary: {} },
    });
    const knob = await screen.findByRole("switch", { name: /domain for shop/i });
    await waitFor(() => expect(knob).toBeEnabled());
    await user.click(knob);
    expect(await dialog(/expose shop/i)).toBeInTheDocument();
  });
});
