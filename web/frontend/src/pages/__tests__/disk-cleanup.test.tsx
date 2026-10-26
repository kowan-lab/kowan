import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { reply, requests } from "../../test/api";
import { currentPath, renderApp } from "../../test/render";
import { choose, dialog, press, toast } from "../../test/ui";
import { defaultPicks, pickedSize } from "../DiskCleanup";
import type { CleanupPlan } from "../../types";

const MB = 1024 * 1024;
const policy = { enabled: false, every: "weekly", weekday: 6, hour: 4, containers: true, dangling: true, imagesOlderThanDays: 0, anonymousVolumes: false, buildCache: true } as const;
const plan = (over: Partial<CleanupPlan> = {}): CleanupPlan => ({
  categories: [
    { id: "containers", title: "Stopped containers", help: "Writable layers.", total: 3 * MB, selectedTotal: 2 * MB, items: [
      { id: "c1", name: "old-job", size: 2 * MB, detail: "alpine · exited", selected: true, note: "" },
      { id: "c2", name: "api-1", size: MB, detail: "api · exited", selected: false, note: "part of api, stopped on purpose" },
    ] },
    { id: "dangling", title: "Untagged images", help: "Leftovers.", total: 50 * MB, selectedTotal: 50 * MB, items: [
      { id: "sha256:d", name: "dddddddddddd", size: 50 * MB, detail: "", selected: true, note: "" },
    ] },
    { id: "images", title: "Unused images", help: "Pulled again when needed.", total: 0, selectedTotal: 0, items: [] },
    { id: "volumes", title: "Unused volumes", help: "Data loss.", total: 9 * MB, selectedTotal: MB, items: [
      { id: "a".repeat(64), name: "aaaaaaaaaaaa", size: MB, detail: "anonymous", selected: true, note: "" },
      { id: "pgdata", name: "pgdata", size: 8 * MB, detail: "named", selected: false, note: "may hold data" },
    ] },
    { id: "build", title: "Build cache", help: "Rebuilt.", total: 5 * MB, selectedTotal: 5 * MB, items: [
      { id: "build-cache", name: "Build cache", size: 5 * MB, detail: "not in use", selected: true, note: "" },
    ] },
  ],
  total: 67 * MB, selectedTotal: 58 * MB,
  disk: { images: 900 * MB, containers: 3 * MB, volumes: 9 * MB, buildCache: 6 * MB },
  schedule: { ...policy }, lastRun: null, ...over,
});

describe("cleanup helpers", () => {
  it("picks the defaults and sums them", () => {
    const p = plan();
    const picks = defaultPicks(p);
    expect([...picks.containers]).toEqual(["c1"]);
    expect([...picks.volumes]).toEqual(["a".repeat(64)]);
    expect(pickedSize(p, picks)).toBe(58 * MB);
  });
});

describe("disk cleanup page", () => {
  it("shows the plan, lets the operator change picks and runs it", async () => {
    const { user } = renderApp("/hosts/local/cleanup", {
      api: {
        "GET /hosts/:h/cleanup": plan(),
        "POST /hosts/:h/cleanup": { freed: 61 * MB, removed: { containers: 1, images: 1, volumes: 2, buildCache: 1 }, problems: [] },
      },
    });
    expect(await screen.findByText("58.0 MB", {}, { timeout: 10_000 })).toBeInTheDocument();
    expect(screen.getByText(/picked of 67.0 MB that can go · 4 objects/)).toBeInTheDocument();
    expect(screen.getByText("part of api, stopped on purpose")).toBeInTheDocument();
    expect(screen.queryByText("Unused images · 0 B")).toBeNull(); // empty categories are hidden
    expect(screen.getByRole("checkbox", { name: "Remove api-1" })).not.toBeChecked();
    await user.click(screen.getByRole("checkbox", { name: "Remove pgdata" }));
    await user.click(screen.getByRole("checkbox", { name: "Remove old-job" }));
    expect(await screen.findByText("64.0 MB")).toBeInTheDocument();
    await press(user, /clean up 64.0 mb/i);
    const dlg = await dialog(/clean up local/i);
    expect(within(dlg).getByText(/the data in pgdata is deleted for good/i)).toBeInTheDocument();
    await press(user, /^remove$/i, dlg);
    await toast(/freed 61.0 mb/i);
    expect(requests("POST", "/hosts/:h/cleanup")[0].body).toEqual({
      containers: [], images: ["sha256:d"], volumes: ["a".repeat(64), "pgdata"], buildCache: true,
    });
  });

  it("selects whole categories and reports partial runs and failures", async () => {
    let n = 0;
    const { user } = renderApp("/hosts/local/cleanup", {
      api: {
        "GET /hosts/:h/cleanup": plan(),
        "POST /hosts/:h/cleanup": () => (n++ === 0
          ? { freed: MB, removed: { containers: 2 }, problems: [{ id: "c2", error: "running again or gone" }] }
          : reply(502, { error: "host offline" })),
      },
    });
    const card = (await screen.findByRole("list", { name: "Stopped containers" }, { timeout: 10_000 })).closest("section")!;
    await press(user, "All", card);
    expect(screen.getByRole("checkbox", { name: "Remove api-1" })).toBeChecked();
    await press(user, /clean up/i);
    await press(user, /^remove$/i, await dialog(/clean up local/i));
    await toast(/1 skipped: running again or gone/i);
    await press(user, "None", card);
    expect(screen.getByRole("checkbox", { name: "Remove old-job" })).not.toBeChecked();
    await press(user, /clean up/i);
    await press(user, /^remove$/i, await dialog(/clean up local/i));
    await toast(/host offline/i);
  });

  it("is read-only for viewers, and says when there is nothing to clean", async () => {
    renderApp("/hosts/local/cleanup", {
      role: "viewer",
      api: { "GET /hosts/:h/cleanup": plan({ categories: plan().categories.map((c) => ({ ...c, items: [], total: 0 })), total: 0,
        lastRun: { at: "2026-10-25T04:00:00Z", freed: 3 * MB, removed: {}, problems: 0, error: "", seconds: 1 } }) },
    });
    expect(await screen.findByText(/nothing to clean/i, {}, { timeout: 10_000 })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /clean up/i })).toBeNull();
    expect(screen.getByText(/freed 3.0 mb/i)).toBeInTheDocument();
    expect(screen.getByRole("switch", { name: /clean up this host on a schedule/i })).toBeDisabled();
  });

  it("reports a host it cannot read", async () => {
    renderApp("/hosts/local/cleanup", { api: { "GET /hosts/:h/cleanup": reply(502, { error: "agent unreachable" }) } });
    expect(await screen.findByText("agent unreachable", {}, { timeout: 10_000 })).toBeInTheDocument();
  });

  it("saves the schedule", async () => {
    const { user } = renderApp("/hosts/local/cleanup", {
      api: {
        "GET /hosts/:h/cleanup": plan({ lastRun: { at: "2026-10-25T04:00:00Z", freed: 0, removed: {}, problems: 0, error: "host offline", seconds: 0 } }),
        "PUT /hosts/:h/cleanup/schedule": (r) => r.body,
      },
    });
    expect(await screen.findByText(/failed: host offline/i, {}, { timeout: 10_000 })).toBeInTheDocument();
    const card = screen.getByRole("heading", { name: "On a schedule" }).closest("section")!;
    expect(within(card).getByRole("button", { name: "Save" })).toBeDisabled();
    await user.click(within(card).getByRole("switch", { name: /clean up this host on a schedule/i }));
    await choose(user, "On", "Wednesday", card);
    await choose(user, "At (UTC)", "02:00", card);
    await user.click(within(card).getByRole("switch", { name: /anonymous volumes/i }));
    await user.clear(within(card).getByLabelText(/unused images older than/i));
    await user.type(within(card).getByLabelText(/unused images older than/i), "30");
    await choose(user, "Every", "Day", card);
    expect(within(card).queryByRole("combobox", { name: "On" })).toBeNull();
    await press(user, "Save", card);
    await toast(/cleanup scheduled/i);
    expect(requests("PUT", "/hosts/:h/cleanup/schedule")[0].body).toEqual({
      ...policy, enabled: true, every: "daily", weekday: 2, hour: 2, anonymousVolumes: true, imagesOlderThanDays: 30,
    });
  });

  it("reports a schedule that cannot be saved", async () => {
    const { user } = renderApp("/hosts/local/cleanup", {
      api: { "GET /hosts/:h/cleanup": plan({ schedule: { ...policy, enabled: true } }), "PUT /hosts/:h/cleanup/schedule": reply(400, { error: "hour must be" }) },
    });
    const sw = await screen.findByRole("switch", { name: /clean up this host on a schedule/i }, { timeout: 10_000 });
    await user.click(sw);
    await press(user, "Save", screen.getByRole("heading", { name: "On a schedule" }).closest("section")!);
    await toast(/hour must be/i);
  });

  it("is linked from the overview and the host page", async () => {
    const { user } = renderApp("/hosts/local", { api: { "GET /hosts/:h/cleanup": plan() } });
    await user.click(await screen.findByRole("button", { name: /disk cleanup/i }, { timeout: 10_000 }));
    await waitFor(() => expect(currentPath()).toBe("/hosts/local/cleanup"));
  });
});
