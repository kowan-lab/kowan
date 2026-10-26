import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { mockApi, reply, requests } from "../../test/api";
import { currentPath, pageRoot, renderApp } from "../../test/render";
import { dialog, press, toast, choose } from "../../test/ui";

const NOW = "2026-10-26T10:00:00Z";
const sync = (over: Record<string, unknown> = {}) => ({
  id: "gs-1", name: "infra", repoUrl: "https://git.example.com/acme/infra.git", ref: "main", path: "kowan", credentialId: "",
  intervalSec: 300, autoApply: false, prune: true, enabled: true, webhookUrl: "/hooks/gitops/abc", status: "drift",
  lastCommit: "a1b2c3d4e5f6", lastCheckedAt: NOW, lastAppliedCommit: "0f0f0f0f0f", lastAppliedAt: NOW, lastError: "",
  summary: { create: 1, update: 1, delete: 1, orphan: 0, adopt: 0, noop: 1 }, planErrors: 0, createdAt: NOW, updatedAt: NOW, ...over,
});
const plan = {
  commit: "a1b2c3d4e5f6", generatedAt: NOW, errors: [],
  summary: { create: 1, update: 1, delete: 1, orphan: 0, adopt: 0, noop: 1 },
  changes: [
    { kind: "Deployment", name: "shop", action: "update", adopt: false, resourceId: "d-shop", file: "kowan/shop.yaml",
      fields: [{ path: "compose", before: "a", after: "b" }], diff: [{ op: "-", text: "image: nginx:1.26" }, { op: "+", text: "image: nginx:1.27" }] },
    { kind: "Workflow", name: "backup", action: "create", adopt: false, resourceId: "", file: "kowan/backup.yaml", fields: [], diff: [{ op: "+", text: "name: backup" }] },
    { kind: "Channel", name: "old", action: "delete", adopt: false, resourceId: "ch-x", file: "", fields: [], diff: [] },
    { kind: "Template", name: "same", action: "noop", adopt: false, resourceId: "t", file: "kowan/t.yaml", fields: [], diff: [] },
  ],
};
const runs = [
  { id: "run-2", syncId: "gs-1", trigger: "manual", action: "apply", status: "partial", commit: "a1b2c3d4e5f6", error: "", startedAt: NOW, finishedAt: NOW,
    summary: { create: 1, update: 0, delete: 0 } },
  { id: "run-1", syncId: "gs-1", trigger: "schedule", action: "refresh", status: "error", commit: "", error: "auth failed", startedAt: NOW, finishedAt: NOW, summary: null },
];

function detailApi(over: Record<string, unknown> = {}) {
  return {
    "GET /gitops/syncs": [sync(), sync({ id: "gs-2", name: "apps", status: "in_sync", enabled: false, intervalSec: 0, lastAppliedAt: "", summary: null })],
    "GET /gitops/syncs/:id/plan": { sync: sync(), plan },
    "GET /gitops/syncs/:id/runs": runs,
    "GET /gitops/syncs/:id/managed": [{ kind: "Deployment", resourceId: "d-shop", name: "shop", commit: "0f0f0f0f0f", updatedAt: NOW }],
    "GET /gitops/syncs/:id/runs/:run": {
      ...runs[0], result: [
        { kind: "Workflow", name: "backup", action: "create", status: "ok", error: "" },
        { kind: "Deployment", name: "shop", action: "update", status: "failed", error: "compose invalid" },
      ], plan,
    },
    ...over,
  };
}

describe("gitops list", () => {
  it("lists syncs and opens one", async () => {
    const { user } = renderApp("/gitops", { api: detailApi() });
    expect(await within(pageRoot()).findByText("apps", {}, { timeout: 10_000 })).toBeInTheDocument();
    expect(screen.getByText(/disabled · manual/)).toBeInTheDocument();
    expect(screen.getByText(/review · 5m/)).toBeInTheDocument();
    expect(screen.getByText("never")).toBeInTheDocument();
    await user.click(screen.getByText("infra"));
    await waitFor(() => expect(currentPath()).toBe("/gitops/gs-1"));
  });

  it("creates a sync and shows the webhook secret once", async () => {
    const { user } = renderApp("/gitops", { api: { "GET /gitops/syncs": [], "POST /gitops/syncs": sync({ webhookSecret: "whsec_123" }) } });
    expect(await screen.findByText(/no syncs yet/i, {}, { timeout: 10_000 })).toBeInTheDocument();
    await user.click(screen.getAllByRole("button", { name: /new sync/i })[0]);
    const d = await dialog(/new sync/i);
    expect(within(d).getByRole("button", { name: /^save$/i })).toBeDisabled();
    await user.type(within(d).getByLabelText(/^name/i), "infra");
    await user.type(within(d).getByLabelText(/repository url/i), " https://git.example.com/acme/infra.git ");
    await press(user, /^save$/i, d);
    expect(await screen.findByText("whsec_123")).toBeInTheDocument();
    expect(requests("POST", "/gitops/syncs")[0].body).toMatchObject({ name: "infra", repoUrl: "https://git.example.com/acme/infra.git", ref: "main" });
  });

  it("keeps the dialog open on a create error", async () => {
    const { user } = renderApp("/gitops", { api: { "GET /gitops/syncs": [], "POST /gitops/syncs": reply(400, { error: "repoUrl must be https" }) } });
    await screen.findByText(/no syncs yet/i, {}, { timeout: 10_000 });
    await user.click(screen.getAllByRole("button", { name: /new sync/i })[0]);
    const d = await dialog(/new sync/i);
    await user.type(within(d).getByLabelText(/^name/i), "x");
    await user.type(within(d).getByLabelText(/repository url/i), "git@x");
    await press(user, /^save$/i, d);
    expect(await within(d).findByText(/repoUrl must be https/i)).toBeInTheDocument();
  });

  it("shows a load error", async () => {
    renderApp("/gitops", { api: { "GET /gitops/syncs": reply(500, { error: "db gone" }) } });
    expect(await screen.findByText(/db gone/i, {}, { timeout: 10_000 })).toBeInTheDocument();
  });

  it("exports resources: preview, zip and push", async () => {
    const { user } = renderApp("/gitops", {
      api: {
        ...detailApi(),
        "GET /gitops/resources": [
          { kind: "Deployment", id: "d-shop", name: "shop", managedBy: "gs-1" },
          { kind: "Workflow", id: "wf-1", name: "nightly backup", managedBy: "" },
        ],
        "POST /gitops/export": (r: { body: { format?: string; push?: unknown } }) =>
          r.body.push
            ? { branch: "export", commit: "deadbeefcafe", changed: true, files: 2, warnings: ["channel ops: secret omitted"] }
            : r.body.format === "zip"
              ? reply(200, new Blob(["PK"], { type: "application/zip" }))
              : { files: [{ path: "deployments/shop.yaml", kind: "Deployment", name: "shop", content: "apiVersion: kowan/v1" }], warnings: [] },
      },
    });
    await within(pageRoot()).findByText("apps", {}, { timeout: 10_000 });
    await press(user, /^export$/i, pageRoot());
    const d = await dialog(/export resources/i);
    expect(await within(d).findByText("nightly backup")).toBeInTheDocument();
    // Everything starts selected; clearing every group disables the actions.
    await user.click(within(d).getByRole("checkbox", { name: /deployment · 1/i }));
    await user.click(within(d).getByRole("checkbox", { name: /workflow · 1/i }));
    expect(within(d).getByRole("button", { name: /download zip/i })).toBeDisabled();
    await user.click(within(d).getByRole("checkbox", { name: /shop/i }));
    await press(user, /preview files/i, d);
    expect(await within(d).findByText("deployments/shop.yaml")).toBeInTheDocument();
    await press(user, /download zip/i, d);
    await waitFor(() => expect(requests("POST", "/gitops/export").some((c) => (c.body as { format: string }).format === "zip")).toBe(true));
    await user.click(within(d).getByRole("tab", { name: /commit to a branch/i }));
    await choose(user, /repository \(sync\)/i, /^infra/i, d);
    const branch = within(d).getByLabelText(/^branch/i);
    await user.clear(branch);
    await user.type(branch, "export");
    await press(user, /commit and push/i, d);
    expect(await within(d).findByText(/pushed 2 file\(s\) to export at deadbeef/i)).toBeInTheDocument();
    expect(within(d).getByText(/secret omitted/i)).toBeInTheDocument();
    const push = requests("POST", "/gitops/export").find((c) => (c.body as { push?: unknown }).push)!.body as { resources: unknown[]; push: object };
    expect(push.push).toMatchObject({ syncId: "gs-1", branch: "export" });
    expect(push.resources).toHaveLength(1);
  });
});

describe("gitops sync detail", () => {
  it("shows the plan, runs and managed resources", async () => {
    const { user } = renderApp("/gitops/gs-1", { api: detailApi() });
    expect(await screen.findByText(/kowan\/shop.yaml/, {}, { timeout: 10_000 })).toBeInTheDocument();
    expect(screen.getByText("auth failed")).toBeInTheDocument();
    expect(screen.getByText("+1 ~0 −0")).toBeInTheDocument();
    // Changes start expanded with their field table and diff; a click folds one.
    expect(await screen.findByText(/image: nginx:1.27/)).toBeInTheDocument();
    expect(screen.getByText("From git")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /1 field/ }));
    await waitFor(() => expect(screen.queryByText("From git")).toBeNull());
  });

  it("applies the plan after confirmation", async () => {
    const { user } = renderApp("/gitops/gs-1", {
      api: detailApi({ "POST /gitops/syncs/:id/apply": { run: { ...runs[0], status: "applied", result: [{ status: "ok" }] }, sync: sync({ status: "in_sync" }) } }),
    });
    await screen.findByText(/kowan\/shop.yaml/, {}, { timeout: 10_000 });
    await press(user, /^apply$/i, pageRoot());
    const d = await dialog(/apply commit a1b2c3d/i);
    expect(within(d).getByText(/1 to create, 1 to update, 1 to delete/)).toBeInTheDocument();
    expect(within(d).getByText(/tears down its containers/i)).toBeInTheDocument();
    await press(user, /^apply$/i, d);
    await toast(/is live/i);
    expect(requests("POST", "/gitops/syncs/gs-1/apply")[0].body).toEqual({ commit: "a1b2c3d4e5f6" });
  });

  it("reports partial and failed applies", async () => {
    const { user } = renderApp("/gitops/gs-1", {
      api: detailApi({ "POST /gitops/syncs/:id/apply": { run: { ...runs[0], result: [{ status: "failed" }, { status: "ok" }] }, sync: sync() } }),
    });
    await screen.findByText(/kowan\/shop.yaml/, {}, { timeout: 10_000 });
    await press(user, /^apply$/i, pageRoot());
    await press(user, /^apply$/i, await dialog(/apply commit/i));
    await toast(/applied with failures/i);
    mockApi({ "POST /gitops/syncs/:id/apply": reply(409, { error: "plan is stale" }) });
    await press(user, /^apply$/i, pageRoot());
    await press(user, /^apply$/i, await dialog(/apply commit/i));
    await toast(/plan is stale/i);
  });

  it("refreshes, rotates the webhook and edits", async () => {
    const { user } = renderApp("/gitops/gs-1", {
      api: detailApi({
        "POST /gitops/syncs/:id/refresh": sync({ status: "error", lastError: "bad yaml in kowan/x.yaml", plan: null }),
        "POST /gitops/syncs/:id/webhook": sync({ webhookSecret: "whsec_new" }),
        "PUT /gitops/syncs/:id": sync({ autoApply: true }),
      }),
    });
    await screen.findByText(/kowan\/shop.yaml/, {}, { timeout: 10_000 });
    await press(user, /^refresh$/i, pageRoot());
    await toast(/bad yaml in kowan\/x.yaml/i);
    mockApi({ "POST /gitops/syncs/:id/refresh": reply(502, { error: "could not clone" }) });
    await press(user, /^refresh$/i, pageRoot());
    await toast(/could not clone/i);
    await press(user, /rotate secret/i, pageRoot());
    expect(await screen.findByText("whsec_new")).toBeInTheDocument();
    await press(user, /^edit$/i, pageRoot());
    const d = await dialog(/edit sync/i);
    expect(within(d).getByLabelText(/repository url/i)).toHaveValue("https://git.example.com/acme/infra.git");
    await user.click(within(d).getByRole("switch", { name: /apply automatically/i }));
    await press(user, /^save$/i, d);
    await waitFor(() => expect(requests("PUT", "/gitops/syncs/gs-1")[0]?.body).toMatchObject({ autoApply: true }));
  });

  it("opens a run with its per-resource result", async () => {
    const { user } = renderApp("/gitops/gs-1", { api: detailApi() });
    await screen.findByText("auth failed", {}, { timeout: 10_000 });
    await user.click(screen.getByText("Partial"));
    const d = await dialog(/apply · a1b2c3d/i);
    expect(await within(d).findByText("compose invalid")).toBeInTheDocument();
    expect(within(d).getByText("trigger: manual")).toBeInTheDocument();
  });

  it("deletes the sync", async () => {
    const { user } = renderApp("/gitops/gs-1", { api: detailApi() });
    await screen.findByText(/kowan\/shop.yaml/, {}, { timeout: 10_000 });
    await press(user, /delete sync/i, pageRoot());
    await press(user, /^delete$/i, await dialog(/delete sync/i));
    await waitFor(() => expect(currentPath()).toBe("/gitops"));
    expect(requests("DELETE", "/gitops/syncs/gs-1")).toHaveLength(1);
  });

  it("reports a delete failure", async () => {
    const { user } = renderApp("/gitops/gs-1", { api: detailApi({ "DELETE /gitops/syncs/:id": reply(500, { error: "locked" }) }) });
    await screen.findByText(/kowan\/shop.yaml/, {}, { timeout: 10_000 });
    await press(user, /delete sync/i, pageRoot());
    await press(user, /^delete$/i, await dialog(/delete sync/i));
    await toast(/locked/i);
  });

  it("blocks apply on repository problems and shows the last error", async () => {
    renderApp("/gitops/gs-1", {
      api: detailApi({
        "GET /gitops/syncs/:id/plan": {
          sync: sync({ status: "error", lastError: "2 files failed to parse" }),
          plan: { ...plan, errors: [{ file: "kowan/x.yaml", doc: 0, message: "unknown kind Foo" }] },
        },
      }),
    });
    expect(await screen.findByText(/2 files failed to parse/, {}, { timeout: 10_000 })).toBeInTheDocument();
    expect(screen.getByText(/unknown kind Foo/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^apply$/i })).toBeDisabled();
  });

  it("shows not found", async () => {
    renderApp("/gitops/nope", { api: detailApi({ "GET /gitops/syncs/:id/plan": reply(404, { error: "sync not found" }) }) });
    expect(await screen.findByText(/sync not found/i, {}, { timeout: 10_000 })).toBeInTheDocument();
  });
});
