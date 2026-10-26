import { act, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { reply, requests, socket } from "../../test/api";
import { containerInspect, containers, containerSpec } from "../../test/fixtures";
import { currentPath, pageRoot, renderApp } from "../../test/render";
import { dialog, press, toast } from "../../test/ui";

const ID = containers[0].id;
const URL = `/containers/${ID}?host=local`;
const withSecret = {
  ...containerInspect,
  Config: { ...containerInspect.Config, Env: ["DB_PASSWORD=hunter2", "TZ=UTC"] },
};
const stopped = {
  ...containerInspect,
  Config: { ...containerInspect.Config, Labels: {} },
  State: { Status: "exited", Running: false, Paused: false, Restarting: false, ExitCode: 137, FinishedAt: "2026-10-26T09:00:00Z", OOMKilled: true },
};

function api(inspect: object = withSecret) {
  return {
    "GET /hosts/:h/containers/:id": inspect,
    "GET /hosts/:h/containers/:id/spec": containerSpec,
    "GET /hosts/:h/containers/:id/files": { path: "/", requested: "/", running: true, entries: [
      { name: "etc", path: "/etc", kind: "dir", isDir: true, size: 0, mode: "drwxr-xr-x", modTime: 0 },
      { name: "app.conf", path: "/app.conf", kind: "file", isDir: false, size: 12, mode: "-rw-r--r--", modTime: 0 },
    ] },
    "GET /hosts/:h/containers/:id/files/content": { path: "/app.conf", size: 12, content: "listen 80\n", binary: false, tooLarge: false, limit: 1048576 },
    "GET /hosts/:h/images/:id/inspect": { inspect: { Id: "sha256:" + "a".repeat(64), RepoTags: ["nginx:1.27"], RepoDigests: ["nginx@sha256:x"], Config: { Env: [] }, Size: 100 }, history: [] },
    "GET /hosts/:h/updates": [],
  };
}

describe("container detail", () => {
  it("shows the overview and masks secrets until revealed", async () => {
    const { user } = renderApp(URL, { api: api() });
    expect(await screen.findByRole("heading", { name: "shop-web-1" }, { timeout: 10_000 })).toBeInTheDocument();
    const page = pageRoot();
    expect(within(page).getAllByText(/8080/).length).toBeGreaterThan(0);
    expect(within(page).queryByText("hunter2")).not.toBeInTheDocument();
    await user.click(within(page).getByRole("button", { name: /reveal db_password/i }));
    expect(within(page).getByText("hunter2")).toBeInTheDocument();
    expect(within(page).getAllByText(/shop_db/).length).toBeGreaterThan(0);
  });

  it("stops, restarts and pauses", async () => {
    const { user } = renderApp(URL, { api: api() });
    await screen.findByRole("heading", { name: "shop-web-1" }, { timeout: 10_000 });
    await press(user, /^stop$/i);
    await waitFor(() => expect(requests("POST", "/hosts/local/containers/:id/stop")).toHaveLength(1));
    await press(user, /^restart$/i);
    await waitFor(() => expect(requests("POST", "/hosts/local/containers/:id/restart")).toHaveLength(1));
    await user.click(screen.getByRole("button", { name: /more actions/i }));
    await user.click(await screen.findByRole("menuitem", { name: /pause/i }));
    await waitFor(() => expect(requests("POST", "/hosts/local/containers/:id/pause")).toHaveLength(1));
  });

  it("removes and goes back to workloads", async () => {
    const { user } = renderApp(URL, { api: api() });
    await screen.findByRole("heading", { name: "shop-web-1" }, { timeout: 10_000 });
    await user.click(screen.getByRole("button", { name: /more actions/i }));
    await user.click(await screen.findByRole("menuitem", { name: /remove/i }));
    const d = await dialog(/remove shop-web-1/i);
    await press(user, /remove container/i, d);
    await waitFor(() => expect(currentPath()).toBe("/workloads"));
    expect(requests("DELETE", "/hosts/local/containers/:id")[0].query.force).toBe("true"); // running: force
  });

  it("explains a crashed container and starts it again", async () => {
    const { user } = renderApp(URL, { api: { ...api(stopped), "POST /hosts/:h/containers/:id/start": reply(500, { error: "port is already allocated" }) } });
    await screen.findByRole("heading", { name: "shop-web-1" }, { timeout: 10_000 });
    expect(await screen.findAllByText(/137|out of memory|oom/i)).not.toHaveLength(0);
    // The last log lines arrive over a one-shot logs socket.
    const ws = await waitFor(() => { const s = socket("/logs"); if (!s) throw new Error("no socket"); return s; });
    await act(async () => { ws.emit("2026-10-26T09:00:00Z Killed\n"); ws.close(); });
    await press(user, /^start$/i);
    await toast(/already allocated/i);
  });

  it("streams logs and stats in their tabs", async () => {
    const { user } = renderApp(URL, { api: api() });
    await screen.findByRole("heading", { name: "shop-web-1" }, { timeout: 10_000 });
    await user.click(screen.getByRole("tab", { name: /^logs$/i }));
    const logs = await waitFor(() => { const s = socket("/logs"); if (!s) throw new Error("no socket"); return s; });
    await act(async () => {
      logs.emit("2026-10-26T10:00:00Z GET / 200\n2026-10-26T10:00:01Z ERROR upstream timed out\n");
      await new Promise((r) => setTimeout(r, 400));
    });
    expect(await screen.findByText(/upstream timed out/)).toBeInTheDocument();
    await user.click(screen.getByRole("tab", { name: /^stats$/i }));
    const stats = await waitFor(() => { const s = socket("/stats/stream"); if (!s) throw new Error("no socket"); return s; });
    await act(async () => {
      stats.emit(JSON.stringify({ cpuPercent: 12.5, memUsage: 64 << 20, memLimit: 1 << 30, memPercent: 6.25, netRx: 1000, netTx: 2000, blockRead: 0, blockWrite: 0, pids: 7 }));
    });
    expect(await screen.findAllByText(/12\.5/)).not.toHaveLength(0);
  });

  it("browses files, inspects JSON and checks the image", async () => {
    const { user } = renderApp(URL, { api: api() });
    await screen.findByRole("heading", { name: "shop-web-1" }, { timeout: 10_000 });
    await user.click(screen.getByRole("tab", { name: /^files$/i }));
    expect(await screen.findByText("app.conf")).toBeInTheDocument();
    await user.click(screen.getByText("app.conf"));
    expect(await screen.findByDisplayValue(/listen 80/).catch(() => screen.findByText(/listen 80/))).toBeTruthy();
    await user.click(screen.getByRole("tab", { name: /^inspect$/i }));
    expect(await screen.findAllByText(/"RestartCount"|RestartCount/)).not.toHaveLength(0);
    await user.click(screen.getByRole("tab", { name: /^image/i }));
    expect(await screen.findAllByText(/nginx:1\.27/)).not.toHaveLength(0);
  });

  it("is read-only for viewers", async () => {
    renderApp(URL, { role: "viewer", api: api() });
    await screen.findByRole("heading", { name: "shop-web-1" }, { timeout: 10_000 });
    expect(screen.queryByRole("button", { name: /^stop$/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /more actions/i })).not.toBeInTheDocument();
  });

  it("handles a missing container and the all-hosts scope", async () => {
    renderApp(URL, { api: { "GET /hosts/:h/containers/:id": reply(404, { error: "No such container" }) } });
    expect(await screen.findAllByText(/no container c-web/i, {}, { timeout: 10_000 })).not.toHaveLength(0);
  });

  it("asks for a host when 'All hosts' is selected", async () => {
    localStorage.setItem("kowan_host", "all");
    renderApp(`/containers/${ID}`, { api: api() });
    expect(await screen.findAllByText(/pick a host to open this container/i, {}, { timeout: 10_000 })).not.toHaveLength(0);
  });
});
