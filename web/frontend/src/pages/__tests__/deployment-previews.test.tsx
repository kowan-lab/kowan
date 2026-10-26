import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { reply, requests } from "../../test/api";
import { currentPath, renderApp } from "../../test/render";
import { choose, dialog, press, toast } from "../../test/ui";
import { deployments, NOW } from "../../test/fixtures";
import { DEFAULT_PREVIEWS, settingsSummary, ttlLabel } from "../../components/deployment/DeploymentPreviews";
import type { Deployment } from "../../types";

const git = { repoUrl: "https://github.com/acme/shop.git", ref: "main", path: "docker-compose.yml", credentialId: "", autoDeploy: true };
const parent = (previews: Deployment["git"] extends infer G ? (G extends { previews?: infer P } ? P : never) : never, over: Partial<Deployment> = {}): Deployment => ({
  ...deployments[0], git: { ...git, previews }, redeployToken: "tok", ...over,
});
const preview = (id: string, ref: string, pr = 0, over: Partial<Deployment> = {}): Deployment => ({
  ...deployments[0], id, name: pr ? `shop-pr-${pr}` : `shop-${ref}`, status: "deployed",
  git: { ...git, ref, autoDeploy: false },
  preview: { parentId: "d-shop", ref, pullRequest: pr, title: pr ? "Faster search" : "", url: pr ? "https://github.com/acme/shop/pull/9" : "",
             createdBy: "push", lastActivityAt: NOW },
  ...over,
});
const ON = { ...DEFAULT_PREVIEWS, branches: "feature/*" };

async function overview(dep: Deployment, extra: Record<string, unknown> = {}, role?: "viewer" | "operator") {
  const r = renderApp("/workloads/dep~d-shop", {
    role,
    api: { "GET /deployments/:id": dep, "GET /deployments": [dep, deployments[1]], ...extra } as never,
  });
  const band = (await screen.findByRole("heading", { name: "Preview environments" }, { timeout: 10_000 })).closest("section")!;
  return { ...r, band };
}

describe("preview helpers", () => {
  it("words the settings", () => {
    expect(ttlLabel(0)).toBe("kept until removed");
    expect(ttlLabel(24)).toBe("removed after 1 day without pushes");
    expect(ttlLabel(72)).toBe("removed after 3 days without pushes");
    expect(ttlLabel(5)).toBe("removed after 5h without pushes");
    expect(settingsSummary({ ...ON })).toBe("branches feature/* and pull requests · removed after 3 days without pushes · up to 5");
    expect(settingsSummary({ ...ON, branches: "", pullRequests: false })).toMatch(/^nothing/);
  });
});

describe("preview environments panel", () => {
  it("sets previews up, keeping the rest of the git source", async () => {
    const { user, band } = await overview(parent(null), { "PUT /deployments/:id": (r: { body: Deployment }) => ({ ...parent(null), git: r.body.git }) });
    expect(within(band).getByText(/run other branches and pull requests/i)).toBeInTheDocument();
    expect(within(band).getByText("off")).toBeInTheDocument();
    await user.click(within(band).getByRole("button", { name: "Set up previews" }));
    const dlg = await dialog("Preview environments");
    await user.click(within(dlg).getByRole("switch", { name: /pull and merge requests/i }));
    await press(user, "Save", dlg);
    expect(within(dlg).getByText(/pick branch patterns/i)).toBeInTheDocument();
    await user.type(within(dlg).getByLabelText("Branches"), " feature/*, fix-* ");
    await choose(user, "Remove idle previews", "After 7 days", dlg);
    await press(user, "Save", dlg);
    await toast(/previews on/i);
    const put = requests("PUT", "/deployments/:id")[0];
    expect(put.body.git).toMatchObject({ repoUrl: git.repoUrl, ref: "main", autoDeploy: true,
      previews: { enabled: true, branches: "feature/*, fix-*", pullRequests: false, ttlHours: 168, max: 5 } });
    expect(put.body.name).toBe("shop");
  });

  it("lists previews, creates and removes them", async () => {
    const list = [preview("p1", "feature/cart"), preview("p2", "topic", 9)];
    const { user, band } = await overview(parent(ON), {
      "GET /deployments/:id/previews": list,
      "POST /deployments/:id/previews": (r: { body: { branch: string } }) => preview("p3", r.body.branch),
      "DELETE /deployments/:id": {},
    });
    expect(await within(band).findByRole("link", { name: "shop-feature/cart" })).toHaveAttribute("href", "/workloads/dep~p1");
    expect(within(band).getByText("2 of 5")).toBeInTheDocument();
    expect(within(band).getByRole("link", { name: /#9 faster search/i })).toHaveAttribute("href", "https://github.com/acme/shop/pull/9");
    expect(within(band).getByText(/branches feature\/\* and pull requests/)).toBeInTheDocument();
    expect(within(band).queryByText(/no webhook yet/i)).toBeNull();

    await user.click(within(band).getByRole("button", { name: "New preview" }));
    const dlg = await dialog("New preview");
    expect(within(dlg).getByRole("button", { name: "Create preview" })).toBeDisabled();
    await user.type(within(dlg).getByLabelText("Branch"), "release/2{Enter}");
    await toast(/shop-release\/2 runs release\/2/i);
    expect(requests("POST", "/deployments/:id/previews")[0]).toMatchObject({ path: "/deployments/d-shop/previews", body: { branch: "release/2" } });

    await user.click(within(band).getByRole("button", { name: "Remove shop-pr-9" }));
    const confirm = await dialog(/remove preview/i);
    expect(within(confirm).getByText(/a new push to topic creates it again/i)).toBeInTheDocument();
    await press(user, /^remove$/i, confirm);
    await toast(/preview removed/i);
    expect(requests("DELETE", "/deployments/:id")[0].path).toBe("/deployments/p2");
  });

  it("reports failures and warns without a webhook", async () => {
    const { user, band } = await overview(parent(ON, { redeployToken: "" }), {
      "GET /deployments/:id/previews": [preview("p1", "a")],
      "POST /deployments/:id/previews": reply(409, { error: "this deployment already has 5 previews (its limit)" }),
      "DELETE /deployments/:id": reply(500, { error: "host offline" }),
      "PUT /deployments/:id": reply(400, { error: "previews.max must be between 1 and 20" }),
    });
    expect(await within(band).findByText(/no webhook yet/i)).toBeInTheDocument();
    await user.click(within(band).getByRole("button", { name: "New preview" }));
    let dlg = await dialog("New preview");
    await user.type(within(dlg).getByLabelText("Branch"), "x");
    await press(user, "Create preview", dlg);
    expect(await within(dlg).findByText(/already has 5 previews/)).toBeInTheDocument();
    await press(user, "Cancel", dlg);

    await user.click(await within(band).findByRole("button", { name: "Remove shop-a" }));
    await press(user, /^remove$/i, await dialog(/remove preview/i));
    await toast(/host offline/i);

    await user.click(within(band).getByRole("button", { name: "Settings" }));
    dlg = await dialog("Preview environments");
    expect(within(dlg).getByLabelText("Branches")).toHaveValue("feature/*");
    await press(user, "Save", dlg);
    expect(await within(dlg).findByText(/previews.max must be between/)).toBeInTheDocument();
  });

  it("turns previews off", async () => {
    const { user, band } = await overview(parent(ON), {
      "GET /deployments/:id/previews": [],
      "PUT /deployments/:id": (r: { body: Deployment }) => ({ ...parent(ON), git: r.body.git }),
    });
    expect(await within(band).findByText(/no previews running/i)).toBeInTheDocument();
    await user.click(within(band).getByRole("button", { name: "Settings" }));
    await press(user, "Turn off", await dialog("Preview environments"));
    await toast(/previews off/i);
    expect(requests("PUT", "/deployments/:id")[0].body.git.previews.enabled).toBe(false);
  });

  it("is read-only for viewers and absent on inline deployments", async () => {
    const { band } = await overview(parent(ON), { "GET /deployments/:id/previews": [preview("p1", "a")] }, "viewer");
    await within(band).findByRole("link", { name: "shop-a" });
    expect(within(band).queryByRole("button")).toBeNull();
  });

  it("keeps preview settings when the configuration is saved", async () => {
    const { user } = renderApp("/workloads/dep~d-shop?tab=config", {
      api: { "GET /deployments/:id": parent(ON), "PUT /deployments/:id": (r: { body: Deployment }) => ({ ...parent(ON), git: r.body.git }) },
    });
    const toggle = await screen.findByRole("switch", { name: /auto deploy on push/i }, { timeout: 10_000 });
    await user.click(toggle);
    await press(user, /^save$/i);
    await waitFor(() => expect(requests("PUT", "/deployments/:id")).toHaveLength(1));
    expect(requests("PUT", "/deployments/:id")[0].body.git).toMatchObject({ autoDeploy: false, previews: ON });
  });
});

describe("a preview's own page", () => {
  it("says what it previews and links back", async () => {
    const p = preview("p2", "topic", 9);
    const { user } = renderApp("/workloads/dep~p2", {
      api: { "GET /deployments/:id": p, "GET /deployments": [parent({ ...ON, ttlHours: 24 }), p] } as never,
    });
    expect(await screen.findByText(/preview of pull request #9/i, {}, { timeout: 10_000 })).toBeInTheDocument();
    expect(screen.getByText(/removed after 1 day without pushes/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Faster search" })).toHaveAttribute("href", "https://github.com/acme/shop/pull/9");
    expect(screen.queryByRole("heading", { name: "Preview environments" })).toBeNull();
    await user.click(screen.getByRole("link", { name: "shop" }));
    await waitFor(() => expect(currentPath()).toBe("/workloads/dep~d-shop"));
  });

  it("explains an orphaned preview", async () => {
    const p = preview("p1", "feature/a");
    renderApp("/workloads/dep~p1", { api: { "GET /deployments/:id": p, "GET /deployments": [p] } as never });
    expect(await screen.findByText(/preview of feature\/a/i, {}, { timeout: 10_000 })).toBeInTheDocument();
    expect(screen.getByText(/its deployment is gone/i)).toBeInTheDocument();
  });

  it("labels previews on the workloads page", async () => {
    const p = preview("p2", "topic", 9);
    renderApp("/workloads", { api: { "GET /deployments": [parent(ON), p] } as never });
    expect(await screen.findByText(/preview · pr #9/i, {}, { timeout: 10_000 })).toBeInTheDocument();
  });
});
