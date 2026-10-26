import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { TOKEN_KEY } from "../../api/client";
import { mockApi, reply, requests } from "../../test/api";
import { users } from "../../test/fixtures";
import { pageRoot, renderApp } from "../../test/render";
import { dialog, press, toast } from "../../test/ui";

const NOW = "2026-10-26T10:00:00Z";
const upd = (over: Record<string, unknown> = {}) => ({
  id: "u1", component: "server", hostId: null, mode: "update", status: "running", fromImage: "kowan:1.0", fromDigest: "sha256:aaaaaaaaaaaa",
  fromVersion: "1.0.0", toImage: "kowan:1.1", toDigest: "sha256:bbbbbbbbbbbb", toVersion: "1.1.0",
  phases: [{ phase: "pull", ok: true, at: NOW }, { phase: "health", ok: false, at: NOW, detail: "timeout" }],
  error: "", logs: [], startedAt: NOW, finishedAt: "", startedBy: "root", ...over,
});
const status = (over: Record<string, unknown> = {}) => ({
  supported: true, image: "ghcr.io/acme/kowan:latest", container: "kowan", version: "1.0.0", running: "sha256:aaaaaaaaaaaa", latest: "sha256:bbbbbbbbbbbb",
  updateAvailable: true, canRollback: true, previous: { image: "kowan:0.9", version: "0.9.0", digest: "sha256:9" }, inProgress: null, last: null, ...over,
});

async function ready() {
  return within(pageRoot()).findByText(/signed in as/i, {}, { timeout: 10_000 });
}

describe("settings page", () => {
  it("validates and changes the own password, adopting the new session", async () => {
    const { user } = renderApp("/settings", {
      api: { "PUT /users/:id": reply(200, users.admin, { "x-session-token": "fresh-token" }), "GET /system/server/update": status() },
    });
    await ready();
    await user.type(screen.getByLabelText(/^new password/i), "short");
    await press(user, /update password/i);
    expect(await screen.findByText(/at least 8 characters/i)).toBeInTheDocument();
    expect(screen.getByText(/do not match/i)).toBeInTheDocument();
    await user.clear(screen.getByLabelText(/^new password/i));
    await user.type(screen.getByLabelText(/^new password/i), "long-enough-1");
    await user.type(screen.getByLabelText(/confirm new password/i), "long-enough-1");
    await press(user, /update password/i);
    await toast(/password updated/i);
    expect(requests("PUT", "/users/u-admin")[0].body).toEqual({ password: "long-enough-1" });
    await waitFor(() => expect(localStorage.getItem(TOKEN_KEY)).toBe("fresh-token"));
  });

  it("reports a password failure", async () => {
    const { user } = renderApp("/settings", { api: { "PUT /users/:id": reply(400, { error: "password too common" }) } });
    await ready();
    await user.type(screen.getByLabelText(/^new password/i), "password1");
    await user.type(screen.getByLabelText(/confirm new password/i), "password1");
    await press(user, /update password/i);
    await toast(/password too common/i);
  });

  it("downloads and restores a backup", async () => {
    const { user } = renderApp("/settings", {
      api: {
        "GET /backup": reply(200, new Blob(["tar"], { type: "application/gzip" }), { "content-disposition": 'attachment; filename="kowan-backup.tar.gz"' }),
        "POST /restore": { status: "staged" },
      },
    });
    await ready();
    await press(user, /download backup/i);
    await toast(/backup downloaded/i);
    expect(screen.getByRole("button", { name: /restore backup/i })).toBeDisabled();
    const input = document.querySelector('input[type="file"]') as HTMLInputElement;
    await user.upload(input, new File(["x"], "kowan.tar.gz", { type: "application/gzip" }));
    expect(screen.getByText("kowan.tar.gz")).toBeInTheDocument();
    await press(user, /restore backup/i);
    await toast(/restart the server to apply/i);
    expect(requests("POST", "/restore")[0].body).toBeInstanceOf(FormData);
  });

  it("reports backup failures", async () => {
    const { user } = renderApp("/settings", { api: { "GET /backup": reply(500, { error: "disk full" }), "POST /restore": reply(400, { error: "not a kowan backup" }) } });
    await ready();
    await press(user, /download backup/i);
    await toast(/disk full/i);
    await user.upload(document.querySelector('input[type="file"]') as HTMLInputElement, new File(["x"], "bad.tar.gz"));
    await press(user, /restore backup/i);
    await toast(/not a kowan backup/i);
  });

  it("updates the server with a pinned image and follows the phases", async () => {
    let polls = 0;
    const { user } = renderApp("/settings", {
      api: {
        "GET /system/server/update": status(),
        "GET /system/server/updates": [upd({ id: "u0", status: "updated", finishedAt: NOW })],
        "POST /system/server/update": { id: "u1" },
        "GET /system/updates/:id": () => (++polls < 2 ? reply(502, { error: "restarting" }) : upd({ status: "updated", finishedAt: NOW, phases: [{ phase: "pull", ok: true, at: NOW }] })),
      },
    });
    expect(await screen.findByText("newer image available", {}, { timeout: 10_000 })).toBeInTheDocument();
    expect(screen.getByText("kept for rollback")).toBeInTheDocument();
    await press(user, /update now/i);
    const d = await dialog(/update the server/i);
    await user.type(within(d).getByLabelText(/image \(optional\)/i), "ghcr.io/acme/kowan:1.1.0");
    await press(user, /start update/i, d);
    expect(await screen.findByText(/reconnecting/i, {}, { timeout: 5000 })).toBeInTheDocument();
    await toast(/server updated/i);
    expect(requests("POST", "/system/server/update")[0].body).toEqual({ image: "ghcr.io/acme/kowan:1.1.0" });
  });

  it("rolls back without restoring the database and opens history details", async () => {
    const { user } = renderApp("/settings", {
      api: {
        "GET /system/server/update": status({ last: upd({ id: "u9", status: "rolled_back", error: "health check timed out", finishedAt: NOW }) }),
        "GET /system/server/updates": [upd({ id: "u9", status: "rolled_back", finishedAt: NOW })],
        "POST /system/server/rollback": { id: "u2" },
        "GET /system/updates/:id": upd({ id: "u2", mode: "rollback", status: "failed", error: "previous image missing", finishedAt: NOW }),
      },
    });
    expect(await screen.findByText("health check timed out", {}, { timeout: 10_000 })).toBeInTheDocument();
    await press(user, /^details$/i);
    const det = await dialog(/^update ·/i);
    expect(within(det).getByText("timeout")).toBeInTheDocument();
    await user.keyboard("{Escape}");
    await press(user, /^roll back$/i, pageRoot());
    const d = await dialog(/roll back the server/i);
    await user.click(within(d).getByRole("checkbox", { name: /restore the database/i }));
    await press(user, /^roll back$/i, d);
    await toast(/previous image missing/i);
    expect(requests("POST", "/system/server/rollback")[0].body).toEqual({ restoreDb: false });
  });

  it("reports update start failures and unsupported setups", async () => {
    const { user } = renderApp("/settings", { api: { "GET /system/server/update": status({ canRollback: false }), "POST /system/server/update": reply(409, { error: "update in progress" }) } });
    expect(await screen.findByText("none kept yet", {}, { timeout: 10_000 })).toBeInTheDocument();
    await press(user, /update now/i);
    const d = await dialog(/update the server/i);
    await press(user, /start update/i, d);
    await toast(/update in progress/i);
    await press(user, /^cancel$/i, d);
    mockApi({ "GET /system/server/update": status({ supported: false, reason: "not running in a container" }) });
    await press(user, /^check$/i);
    expect(await screen.findByText("not running in a container")).toBeInTheDocument();
  });

  it("shows the running and latest release, and Check asks the registry again", async () => {
    let latest = "1.0.0";
    const { user } = renderApp("/settings", {
      api: {
        "GET /system/server/update": (r: { query: Record<string, string> }) => {
          if (r.query.refresh) latest = "1.1.0";
          return status({ updateAvailable: latest !== "1.0.0", currentVersion: "1.0.0", latestVersion: latest, newVersion: latest !== "1.0.0" });
        },
      },
    });
    expect(await screen.findByText("up to date", {}, { timeout: 10_000 })).toBeInTheDocument();
    expect(screen.getByText("Latest release")).toBeInTheDocument();
    expect(screen.queryByText("bbbbbbbbbbbb")).toBeNull(); // a version, not a digest
    await press(user, /^check$/i);
    expect(await screen.findByText("newer version available")).toBeInTheDocument();
    expect(screen.getByText("1.1.0")).toBeInTheDocument();
    expect(requests("GET", "/system/server/update").some((r) => r.query.refresh === "1")).toBe(true);
    expect(screen.getByRole("button", { name: /update now/i })).toBeInTheDocument();
  });

  it("shows versions and Check for a server run from source", async () => {
    const { user } = renderApp("/settings", {
      api: {
        "GET /system/server/update": status({
          supported: false, reason: "the server is not running in a container", currentVersion: "0.1.0", latestVersion: "0.1.1", newVersion: true,
        }),
      },
    });
    expect(await screen.findByText(/self-update is not available/i, {}, { timeout: 10_000 })).toBeInTheDocument();
    expect(screen.getByText("0.1.0")).toBeInTheDocument();
    expect(screen.getByText("0.1.1")).toBeInTheDocument();
    expect(screen.getByText(/to move to 0\.1\.1, pull the new image or the new code/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /update now/i })).toBeNull();
    await press(user, /^check$/i);
    await waitFor(() => expect(requests("GET", "/system/server/update").some((r) => r.query.refresh === "1")).toBe(true));
  });

  it("reports a failed check", async () => {
    const { user } = renderApp("/settings", { api: { "GET /system/server/update": (r: { query: Record<string, string> }) =>
      r.query.refresh ? reply(502, { error: "registry unreachable" }) : status({ updateAvailable: false, currentVersion: "1.0.0", latestVersion: "1.0.0" }) } });
    await screen.findByText("up to date", {}, { timeout: 10_000 });
    await press(user, /^check$/i);
    await toast(/could not check for updates/i);
  });

  it("shows a status error and hides admin cards for operators", async () => {
    renderApp("/settings", { api: { "GET /system/server/update": reply(500, { error: "docker socket missing" }) } });
    expect(await screen.findByText(/docker socket missing/i, {}, { timeout: 10_000 })).toBeInTheDocument();
  });

  it("operators see only profile and appearance", async () => {
    renderApp("/settings", { role: "operator" });
    await ready();
    expect(screen.queryByText(/backup & restore/i)).toBeNull();
    expect(screen.queryByText(/server updates/i)).toBeNull();
  });
});
