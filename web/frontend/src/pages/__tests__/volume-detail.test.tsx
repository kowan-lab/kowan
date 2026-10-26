import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { mockApi, reply, requests } from "../../test/api";
import { currentPath, pageRoot, renderApp } from "../../test/render";
import { dialog, press, toast } from "../../test/ui";

const NOW = "2026-10-26T10:00:00Z";
const inspect = {
  Name: "shop_db", Driver: "local", Mountpoint: "/var/lib/docker/volumes/shop_db/_data", Scope: "local", CreatedAt: NOW,
  Labels: { "com.docker.compose.project": "shop" }, Options: { type: "nfs" },
};
const repo = { id: "r-s3", name: "offsite", type: "s3", hostId: "", config: { bucket: "b" }, secrets: {}, initialized: true, createdAt: NOW, updatedAt: NOW };
const snap = { id: "abcdef1234", shortId: "abcdef12", time: NOW, hostname: "local", paths: ["/data"], tags: ["kowan"], volume: "shop_db", jobId: "", sizeBytes: 2048, fileCount: 3, repoId: "r-s3" };
const run = { id: "run-9", jobId: "", jobName: "shop_db", repoId: "r-s3", hostId: "local", kind: "backup", trigger: "manual", status: "running",
  volumes: ["shop_db"], snapshots: [], stats: {}, bytesAdded: 0, durationSec: 0, error: "", startedAt: NOW, finishedAt: "" };
const api = {
  "GET /hosts/:h/volumes/:name/inspect": inspect,
  "GET /volbackups/repos": [repo],
  "GET /volbackups/volumes/:h/:name/snapshots": { snapshots: [snap], errors: [], repoIds: ["r-s3"] },
  "GET /volbackups/runs": [{ ...run, status: "success", bytesAdded: 2048, durationSec: 4 }],
  "GET /volbackups/runs/:id": run,
  "GET /volbackups/repos/:id/snapshots/:snap/ls": (r: { query: Record<string, string> }) => ({
    path: r.query.path,
    truncated: false,
    entries: r.query.path.endsWith("/data")
      ? [{ name: "pg", path: "/data/pg", type: "dir", isDir: true, size: 0, mtime: NOW, mode: "drwx------" }, { name: "PG_VERSION", path: "/data/PG_VERSION", type: "file", isDir: false, size: 3, mtime: NOW, mode: "-rw-------" }]
      : [],
  }),
};
const URL = "/volumes/shop_db?host=local";

async function ready() {
  return within(pageRoot()).findByText("/var/lib/docker/volumes/shop_db/_data", {}, { timeout: 10_000 });
}

describe("volume detail", () => {
  it("shows the inspect data and links to the explorer", async () => {
    const { user } = renderApp(URL, { api });
    await ready();
    expect(screen.getByText("com.docker.compose.project=shop")).toBeInTheDocument();
    await press(user, /browse files/i, pageRoot());
    await waitFor(() => expect(currentPath()).toMatch(/^\/volume-explorer\?.*shop_db/));
  });

  it("removes the volume, reporting failures", async () => {
    const { user } = renderApp(URL, { api: { ...api, "DELETE /hosts/:h/volumes/:name": reply(409, { error: "volume is in use" }) } });
    await ready();
    await press(user, /^remove$/i, pageRoot());
    await press(user, /^remove$/i, await dialog());
    await toast(/volume is in use/i);
    mockApi({ "DELETE /hosts/:h/volumes/:name": reply(204) });
    await press(user, /^remove$/i, await dialog());
    await waitFor(() => expect(currentPath()).toBe("/volumes"));
  });

  it("backs the volume up now and lists snapshots and runs", async () => {
    const { user } = renderApp(URL, { api: { ...api, "POST /volbackups/volumes/:h/:name/backup": run } });
    expect(await screen.findByText("abcdef12", {}, { timeout: 10_000 })).toBeInTheDocument();
    await press(user, /back up now/i, pageRoot());
    await toast(/shop_db → offsite/i);
    expect(requests("POST", "/volbackups/volumes/local/shop_db/backup")[0].body).toEqual({ repoId: "r-s3" });
  });

  it("browses a snapshot", async () => {
    const { user } = renderApp(URL, { api });
    await user.click(await screen.findByRole("button", { name: /^browse$/i }, { timeout: 10_000 }));
    const d = await dialog(/snapshot abcdef12/i);
    await user.click(await within(d).findByText("pg"));
    await waitFor(() => expect(requests("GET", "/volbackups/repos/r-s3/snapshots/abcdef1234/ls").at(-1)!.query.path).toBe("/data/pg"));
    expect(await within(d).findByText(/empty directory/i)).toBeInTheDocument();
    await press(user, /^up$/i, d);
    expect(await within(d).findByText("PG_VERSION")).toBeInTheDocument();
  });

  it("restores into a new volume or over the existing one", async () => {
    const { user } = renderApp(URL, { api: { ...api, "POST /volbackups/restore": { ...run, kind: "restore" } } });
    await user.click(await screen.findByRole("button", { name: /^restore$/i }, { timeout: 10_000 }));
    let d = await dialog(/restore snapshot abcdef12/i);
    const target = within(d).getByLabelText(/new volume name/i);
    await user.clear(target);
    expect(within(d).getByRole("button", { name: /restore/i })).toBeDisabled();
    await user.type(target, "shop_db_copy");
    await user.click(within(d).getByRole("button", { name: /restore/i }));
    await toast(/restore started/i);
    expect(requests("POST", "/volbackups/restore")[0].body).toMatchObject({ target: "shop_db_copy", newVolume: true, snapshotId: "abcdef1234", repoId: "r-s3" });
    // The started run opens in its own dialog; close it to get back to the page.
    await dialog(/restore: shop_db|backup: shop_db/i);
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());

    await user.click(screen.getByRole("button", { name: /^restore$/i }));
    d = await dialog(/restore snapshot/i);
    await user.click(within(d).getByRole("radio", { name: /into shop_db \(overwrite\)/i }));
    await user.click(within(d).getByRole("switch", { name: /exact restore/i }));
    await user.click(within(d).getAllByRole("button", { name: /restore|overwrite/i }).at(-1)!);
    await waitFor(() => expect(requests("POST", "/volbackups/restore")).toHaveLength(2));
    expect(requests("POST", "/volbackups/restore")[1].body).toMatchObject({ target: "shop_db", newVolume: false, stopContainers: true, exact: false });
  });

  it("reports backup and restore failures", async () => {
    const { user } = renderApp(URL, {
      api: { ...api, "POST /volbackups/volumes/:h/:name/backup": reply(409, { error: "a backup is running" }), "POST /volbackups/restore": reply(500, { error: "restic failed" }) },
    });
    await screen.findByText("abcdef12", {}, { timeout: 10_000 });
    await press(user, /back up now/i, pageRoot());
    await toast(/a backup is running/i);
    await user.click(screen.getByRole("button", { name: /^restore$/i }));
    const d = await dialog(/restore snapshot/i);
    await user.click(within(d).getByRole("button", { name: /restore/i }));
    await toast(/restic failed/i);
  });

  it("opens the job editor from the menu", async () => {
    const { user } = renderApp(URL, { api });
    await screen.findByText("abcdef12", {}, { timeout: 10_000 });
    await press(user, "More", pageRoot());
    await user.click(await screen.findByRole("menuitem", { name: /new scheduled job/i }));
    const d = await dialog(/new backup job/i);
    expect(within(d).getByText("shop_db")).toBeInTheDocument();
  });

  it("explains the all-hosts scope and inspect errors", async () => {
    localStorage.setItem("kowan_host", "all");
    renderApp("/volumes/shop_db", { api });
    expect(await screen.findByText(/select a host/i, {}, { timeout: 10_000 })).toBeInTheDocument();
  });

  it("shows an inspect error", async () => {
    renderApp(URL, { api: { ...api, "GET /hosts/:h/volumes/:name/inspect": reply(404, { error: "no such volume" }) } });
    expect(await screen.findAllByText(/no such volume/i, {}, { timeout: 10_000 })).not.toHaveLength(0);
  });
});
