import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { mockApi, reply, requests } from "../../test/api";
import { pageRoot, renderApp } from "../../test/render";
import { choose, dialog, press, toast } from "../../test/ui";

const NOW = "2026-10-26T10:00:00Z";
const repos = [
  { id: "r-s3", name: "offsite", type: "s3", hostId: "", config: { bucket: "kowan", prefix: "prod" }, secrets: { password: "********" }, initialized: true, createdAt: NOW, updatedAt: NOW },
  { id: "r-loc", name: "disk", type: "local", hostId: "local", config: { path: "/srv/restic" }, secrets: {}, initialized: false, createdAt: NOW, updatedAt: NOW },
  { id: "r-sftp", name: "nas", type: "sftp", hostId: "", config: { user: "bk", host: "nas", path: "/b" }, secrets: {}, initialized: true, createdAt: NOW, updatedAt: NOW },
  { id: "r-rest", name: "rest", type: "rest", hostId: "", config: { url: "https://rest.example/r" }, secrets: {}, initialized: true, createdAt: NOW, updatedAt: NOW },
];
const run = { id: "run-1", jobId: "j-db", jobName: "db", repoId: "r-s3", hostId: "local", kind: "backup", trigger: "schedule", status: "failed",
  volumes: ["shop_db"], snapshots: [], stats: {}, bytesAdded: 0, durationSec: 12, error: "repository locked\nmore", log: "restic: locked", startedAt: NOW, finishedAt: NOW };
const jobs = [
  { id: "j-db", name: "db", hostId: "local", repoId: "r-s3", selector: { mode: "deployment", deploymentId: "d-shop" }, schedule: "0 3 * * *", timezone: "Europe/Rome",
    retention: { keepDaily: 7 }, hooks: {}, tags: [], enabled: true, lastRunAt: NOW, createdAt: NOW, updatedAt: NOW, nextRun: "2026-10-27T02:00:00Z", lastRun: run, running: false },
  { id: "j-off", name: "media", hostId: "local", repoId: "r-loc", selector: { mode: "names", volumes: ["orphan-data"] }, schedule: "", timezone: "",
    retention: {}, hooks: {}, tags: [], enabled: false, lastRunAt: "", createdAt: NOW, updatedAt: NOW, nextRun: "", lastRun: null, running: false },
];
const api = {
  "GET /volbackups/repos": repos,
  "GET /volbackups/jobs": jobs,
  "GET /volbackups/runs": [run],
  "GET /volbackups/runs/:id": run,
  "POST /volbackups/jobs/preview": { volumes: ["shop_db"] },
};

async function ready() {
  return within(pageRoot()).findByText("media", {}, { timeout: 10_000 });
}

describe("backups page", () => {
  it("lists jobs, runs one and opens its log", async () => {
    const { user } = renderApp("/backups", { api: { ...api, "POST /volbackups/jobs/:id/run": { ...run, id: "run-2", status: "running" } } });
    await ready();
    expect(screen.getByText(/1 job failed on the last run/i)).toBeInTheDocument();
    expect(screen.getByText("never")).toBeInTheDocument();
    await user.click(screen.getAllByRole("button", { name: "Back up now" })[0]);
    await toast(/backup started/i);
    expect(requests("POST", "/volbackups/jobs/j-db/run")).toHaveLength(1);
    expect(await screen.findByText("restic: locked")).toBeInTheDocument();
  });

  it("toggles, deletes and reports failures", async () => {
    const { user } = renderApp("/backups", { api: { ...api, "POST /volbackups/jobs/:id/run": reply(409, { error: "already running" }) } });
    await ready();
    await press(user, "Enable");
    await waitFor(() => expect(requests("PUT", "/volbackups/jobs/j-off")[0]?.body).toEqual({ enabled: true }));
    await user.click(screen.getAllByRole("button", { name: "Back up now" })[0]);
    await toast(/already running/i);
    await user.click(screen.getAllByRole("button", { name: "Delete" })[1]);
    await press(user, /^delete$/i, await dialog(/delete backup job/i));
    await waitFor(() => expect(requests("DELETE", "/volbackups/jobs/j-off")).toHaveLength(1));
    mockApi({ "DELETE /volbackups/jobs/:id": reply(500, { error: "busy" }) });
    await user.click(screen.getAllByRole("button", { name: "Delete" })[0]);
    await press(user, /^delete$/i, await dialog(/delete backup job/i));
    await toast(/busy/i);
  });

  it("creates a job by deployment with a preview and retention", async () => {
    const { user } = renderApp("/backups", { api: { ...api, "POST /volbackups/jobs": (r) => ({ ...jobs[0], ...r.body, id: "j-new" }) } });
    await ready();
    await press(user, /new job/i, pageRoot());
    const d = await dialog(/new backup job/i);
    expect(within(d).getByRole("button", { name: /create job/i })).toBeDisabled();
    await user.type(within(d).getByLabelText(/^name/i), "shop");
    await choose(user, /^repository/i, /offsite/i, d);
    await choose(user, /select by/i, /^deployment$/i, d);
    await waitFor(() => expect(requests("GET", "/deployments").length).toBeGreaterThan(0));
    await choose(user, /^deployment/i, /^shop$/i, d);
    expect(await within(d).findByText(/matches now: shop_db/i)).toBeInTheDocument();
    await user.click(within(d).getByRole("switch", { name: /stop containers/i }));
    await user.click(within(d).getAllByRole("button", { name: /add command/i })[0]);
    await user.type(within(d).getByLabelText("Command"), "pg_dump app");
    await press(user, /create job/i, d);
    await toast(/backup job saved/i);
    expect(requests("POST", "/volbackups/jobs")[0].body).toMatchObject({
      name: "shop", repoId: "r-s3", selector: { mode: "deployment", deploymentId: "d-shop" },
      hooks: { stopContainers: true, pre: [{ container: "nightly-job", command: "pg_dump app" }] },
    });
  });

  it("picks volumes by name and stack, and edits a job", async () => {
    const { user } = renderApp("/backups", { api: { ...api, "PUT /volbackups/jobs/:id": reply(400, { error: "bad cron" }) } });
    await ready();
    await user.click(screen.getAllByRole("button", { name: "Edit" })[1]);
    const d = await dialog(/edit media/i);
    expect(within(d).getByText("orphan-data")).toBeInTheDocument();
    await choose(user, /add volume/i, /shop_db/i, d);
    expect(within(d).getByText("shop_db")).toBeInTheDocument();
    await choose(user, /select by/i, /compose stack/i, d);
    await choose(user, /^stack/i, /^shop$/i, d);
    await press(user, /^save$/i, d);
    await toast(/bad cron/i);
    expect(requests("PUT", "/volbackups/jobs/j-off")[0].body).toMatchObject({ selector: { mode: "stack", stack: "shop" } });
  });

  it("manages repositories: create, test, edit and delete", async () => {
    const { user } = renderApp("/backups", {
      api: {
        ...api,
        "POST /volbackups/repos": (r) => ({ ...repos[0], ...r.body, id: "r-new" }),
        "POST /volbackups/repos/:id/test": { ok: false, initialized: false, log: "Fatal: wrong password" },
        "PUT /volbackups/repos/:id": (r) => ({ ...repos[0], ...r.body }),
      },
    });
    await ready();
    await user.click(screen.getByRole("tab", { name: /repositories/i }));
    expect(await screen.findByText("kowan/prod".replace("kowan/", "s3.amazonaws.com/kowan/"))).toBeInTheDocument();
    expect(screen.getByText("bk@nas:/b")).toBeInTheDocument();
    expect(screen.getByText("not initialised")).toBeInTheDocument();

    await user.click(within(pageRoot()).getAllByRole("button", { name: /^repository$/i })[0]);
    const d = await dialog(/new repository/i);
    await user.type(within(d).getByLabelText(/^name/i), "minio");
    await choose(user, /^type/i, /s3-compatible/i, d);
    await user.type(within(d).getByLabelText(/^bucket/i), "bk");
    await user.type(within(d).getByLabelText(/access key id/i), "AK");
    await user.type(within(d).getByLabelText(/secret access key/i), "SK");
    await user.type(within(d).getByLabelText(/repository password/i), "pw");
    await press(user, /create repository/i, d);
    await toast(/repository saved/i);
    expect(requests("POST", "/volbackups/repos")[0].body).toMatchObject({
      name: "minio", type: "s3", config: { bucket: "bk" }, secrets: { accessKeyId: "AK", secretAccessKey: "SK", password: "pw" },
    });

    await user.click(screen.getAllByRole("button", { name: /^test$/i })[0]);
    expect(await screen.findByText("Fatal: wrong password")).toBeInTheDocument();
    await user.keyboard("{Escape}");

    await user.click(screen.getAllByRole("button", { name: "Edit" })[2]);
    const e = await dialog(/edit nas/i);
    await user.type(within(e).getByLabelText(/^port/i), "2222");
    await press(user, /^save$/i, e);
    await waitFor(() => expect(requests("PUT", "/volbackups/repos/r-sftp")[0]?.body).toMatchObject({ config: { port: "2222" } }));

    mockApi({ "DELETE /volbackups/repos/:id": reply(409, { error: "used by job db" }) });
    await user.click(screen.getAllByRole("button", { name: "Delete" })[0]);
    await press(user, /^delete$/i, await dialog(/delete repository/i));
    await toast(/used by job db/i);
  });

  it("shows the run history and opens a run", async () => {
    const { user } = renderApp("/backups", { api });
    await ready();
    await user.click(screen.getByRole("tab", { name: /run history/i }));
    await user.click(await screen.findByText("repository locked"));
    expect(await screen.findByText("restic: locked")).toBeInTheDocument();
  });

  it("starts from a repository when nothing exists", async () => {
    renderApp("/backups", { api: { "GET /volbackups/repos": [], "GET /volbackups/jobs": [], "GET /volbackups/runs": [] } });
    expect(await screen.findByText(/start with a repository/i, {}, { timeout: 10_000 })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /new job/i })).toBeDisabled();
  });
});
