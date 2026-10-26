import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { reply, requests } from "../../test/api";
import { renderApp } from "../../test/render";
import { choose, dialog, press, toast } from "../../test/ui";
import { deployments } from "../../test/fixtures";
import { promotionCandidates } from "../../components/deployment/DeploymentPromotion";
import type { Deployment } from "../../types";

const staging: Deployment = { ...deployments[0], id: "d-shop", name: "shop" };
const prod: Deployment = { ...deployments[0], id: "d-prod", name: "shop-prod" };
const api: Deployment = deployments[1];
const plan = {
  target: { id: "d-prod", name: "shop-prod" },
  changes: [{ what: "web image", from: "nginx:1.27", to: "nginx:1.28" }],
  notes: ["not in shop-prod, left out: extra"],
  pinned: false,
  diff: [{ op: " ", text: "services:" }, { op: "-", text: "    image: nginx:1.27" }, { op: "+", text: "    image: nginx:1.28" }],
};

async function band(dep: Deployment, all: Deployment[], extra: Record<string, unknown> = {}, role?: "viewer" | "operator") {
  const r = renderApp(`/workloads/dep~${dep.id}`, {
    role,
    api: { "GET /deployments/:id": dep, "GET /deployments": all, ...extra } as never,
  });
  const el = (await screen.findByRole("heading", { name: "Promotion" }, { timeout: 10_000 })).closest("section")!;
  return { ...r, el };
}

describe("promotion candidates", () => {
  it("keeps deployments of the same kind", () => {
    const git = { ...prod, id: "g", git: { repoUrl: "https://x/r.git", ref: "main", path: "c.yml", credentialId: "", autoDeploy: true } };
    const preview = { ...prod, id: "p", preview: { parentId: "d-shop", ref: "x", pullRequest: 0, title: "", url: "", createdBy: "", lastActivityAt: "" } };
    expect(promotionCandidates(staging, [staging, prod, api, git, preview]).map((d) => d.id)).toEqual(["d-prod"]);
  });
});

describe("promotion panel", () => {
  it("pairs a deployment with its production", async () => {
    const { user, el } = await band(staging, [staging, prod, api], {
      "PUT /deployments/:id/promotion": (r: { body: { to: string } }) => ({ ...staging, promoteTo: r.body.to }),
    });
    expect(within(el).getByText("not set")).toBeInTheDocument();
    await user.click(within(el).getByRole("button", { name: "Promote to…" }));
    await choose(user, "Promotes to", "shop-prod", el);
    await toast(/promotes to shop-prod/i);
    expect(requests("PUT", "/deployments/:id/promotion")[0]).toMatchObject({ path: "/deployments/d-shop/promotion", body: { to: "d-prod" } });
  });

  it("shows the plan and promotes", async () => {
    const paired = { ...staging, promoteTo: "d-prod" };
    const { user, el } = await band(paired, [paired, prod], {
      "GET /deployments/:id/promote": plan,
      "POST /deployments/:id/promote": prod,
    });
    expect(within(el).getByText("to shop-prod")).toBeInTheDocument();
    expect(within(el).getByText(/its own environment, ports and volumes/)).toBeInTheDocument();
    await user.click(within(el).getByRole("button", { name: "Promote to shop-prod" }));
    const dlg = await dialog(/promote shop to shop-prod/i);
    expect(await within(dlg).findByText("web image")).toBeInTheDocument();
    expect(within(dlg).getByText(/left out: extra/)).toBeInTheDocument();
    expect(within(dlg).getByRole("table", { name: "Diff" })).toBeInTheDocument();
    expect(requests("GET", "/deployments/:id/promote")[0].query).toEqual({ to: "d-prod" });
    await user.click(within(dlg).getByRole("checkbox", { name: /deploy shop-prod now/i }));
    await press(user, "Promote to shop-prod", dlg);
    await toast(/saved; deploy it to apply/i);
    expect(requests("POST", "/deployments/:id/promote")[0].body).toEqual({ to: "d-prod", deploy: false });
  });

  it("explains a git pin, nothing to promote and refusals", async () => {
    const paired = { ...staging, promoteTo: "d-prod" };
    const { user, el } = await band(paired, [paired, prod], {
      "GET /deployments/:id/promote": { ...plan, pinned: true, notes: [] },
      "POST /deployments/:id/promote": reply(409, { error: "shop-prod already runs what shop runs" }),
    });
    await user.click(within(el).getByRole("button", { name: "Promote to shop-prod" }));
    let dlg = await dialog(/promote shop/i);
    expect(await within(dlg).findByText(/pinned to this commit/)).toBeInTheDocument();
    await press(user, "Promote to shop-prod", dlg);
    await toast(/already runs/i);
    await press(user, "Cancel", dlg);
  });

  it("disables promoting when the target already matches or cannot be planned", async () => {
    const paired = { ...staging, promoteTo: "d-prod" };
    let current: unknown = { ...plan, changes: [] };
    const { user, el } = await band(paired, [paired, prod], { "GET /deployments/:id/promote": () => current });
    await user.click(within(el).getByRole("button", { name: "Promote to shop-prod" }));
    let dlg = await dialog(/promote shop/i);
    expect(await within(dlg).findByText(/already runs what shop runs/)).toBeInTheDocument();
    expect(within(dlg).getAllByRole("button", { name: "Promote to shop-prod" }).at(-1)).toBeDisabled();
    await press(user, "Cancel", dlg);
    current = reply(400, { error: "the deployments use different repositories" });
    await user.click(within(el).getByRole("button", { name: "Promote to shop-prod" }));
    dlg = await dialog(/promote shop/i);
    expect(await within(dlg).findByText(/different repositories/)).toBeInTheDocument();
  });

  it("clears the target and reports pairing errors", async () => {
    const paired = { ...staging, promoteTo: "d-prod" };
    const { user, el } = await band(paired, [paired, prod], {
      "PUT /deployments/:id/promotion": reply(400, { error: "nope" }),
    });
    await user.click(within(el).getByRole("button", { name: "Change" }));
    await choose(user, "Promotes to", "Nothing", el);
    await toast(/nope/i);
    expect(requests("PUT", "/deployments/:id/promotion")[0].body).toEqual({ to: "" });
  });

  it("tells production where promotions come from, read-only for viewers", async () => {
    const src = { ...staging, promoteTo: "d-prod" };
    const { el } = await band(prod, [src, prod], {}, "viewer");
    expect(within(el).getByText("from shop")).toBeInTheDocument();
    expect(within(el).getByRole("link", { name: "shop" })).toHaveAttribute("href", "/workloads/dep~d-shop");
    expect(within(el).queryByRole("button")).toBeNull();
  });

  it("is hidden when there is nothing to show", async () => {
    renderApp("/workloads/dep~d-api", { role: "viewer", api: { "GET /deployments/:id": api, "GET /deployments": [api, staging] } as never });
    await screen.findByRole("heading", { name: /policies and wiring/i }, { timeout: 10_000 });
    await waitFor(() => expect(requests("GET", "/deployments").length).toBeGreaterThan(0));
    expect(screen.queryByRole("heading", { name: "Promotion" })).toBeNull();
  });
});
