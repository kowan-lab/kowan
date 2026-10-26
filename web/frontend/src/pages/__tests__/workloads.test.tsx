import { act, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { reply, requests, socket } from "../../test/api";
import type { Handler } from "../../test/api";
import { containers, containerSpec, deployments } from "../../test/fixtures";
import { currentPath, pageRoot, renderApp } from "../../test/render";
import { dialog, press, toast } from "../../test/ui";

const SHOP = "/workloads/dep~d-shop";

async function open(path: string, name: string, api: Record<string, Handler> = {}, role?: "viewer") {
  const r = renderApp(path, { api, role });
  await screen.findByRole("heading", { name }, { timeout: 10_000 });
  return r;
}

describe("workload: deployment", () => {
  it("redeploys, stops and toggles auto-heal and protection", async () => {
    const { user } = await open(SHOP, "shop");
    await press(user, /^redeploy$/i);
    await waitFor(() => expect(requests("POST", "/deployments/d-shop/deploy")).toHaveLength(1));
    await press(user, /^stop$/i);
    await waitFor(() => expect(requests("POST", "/deployments/d-shop/stop")).toHaveLength(1));
    await press(user, /^more$/i);
    await user.click(await screen.findByRole("menuitem", { name: /auto-heal/i }));
    await waitFor(() => expect(requests("PUT", "/deployments/d-shop/enabled")[0].body).toEqual({ enabled: false }));
    await press(user, /^more$/i);
    await user.click(await screen.findByRole("menuitem", { name: /deletion protection/i }));
    await waitFor(() => expect(requests("PUT", "/deployments/d-shop/protection")[0].body).toEqual({ protected: true }));
  });

  it("removes the containers, then deletes the deployment", async () => {
    const { user } = await open(SHOP, "shop");
    await press(user, /^more$/i);
    await user.click(await screen.findByRole("menuitem", { name: /remove containers/i }));
    let d = await dialog();
    await user.click(within(d).getAllByRole("button").at(-1)!);
    await waitFor(() => expect(requests("POST", "/deployments/d-shop/destroy")).toHaveLength(1));
    await press(user, /^more$/i);
    await user.click(await screen.findByRole("menuitem", { name: /delete deployment/i }));
    d = await dialog();
    const confirmInput = within(d).queryByRole("textbox");
    if (confirmInput) await user.type(confirmInput, "shop");
    await user.click(within(d).getAllByRole("button").at(-1)!);
    await waitFor(() => expect(requests("DELETE", "/deployments/d-shop")).toHaveLength(1));
    await waitFor(() => expect(currentPath()).toBe("/workloads"));
  });

  it("edits the configuration and saves", async () => {
    const { user } = await open(SHOP + "?tab=config", "shop", { "PUT /deployments/:id": (r: { body: unknown }) => ({ ...deployments[0], ...(r.body as object) }) });
    const save = await screen.findByRole("button", { name: /^save$/i }, { timeout: 10_000 });
    expect(save).toBeDisabled();
    await user.click(screen.getByLabelText(/swarm cluster/i));
    expect(screen.getAllByText(/unsaved changes/i).length).toBeGreaterThan(0);
    // The compose file is validated (debounced) before it can be saved.
    await waitFor(() => expect(requests("POST", "/compose/validate").length).toBeGreaterThan(0), { timeout: 5000 });
    await waitFor(() => expect(screen.queryByText(/validating/i)).not.toBeInTheDocument());
    await user.click(screen.getByRole("button", { name: /^save$/i }));
    await waitFor(() => expect(requests("PUT", "/deployments/d-shop")[0].body).toMatchObject({ strategy: "swarm" }));
  });

  it("writes chosen secret values into the compose", async () => {
    const compose = "services:\n  web:\n    image: nginx:[[shop-env.TAG]]\n    environment: [PASSWORD=[[shop-env.DB_PASSWORD]]]\n";
    let dep = { ...deployments[0], compose };
    const { user } = await open(SHOP + "?tab=config", "shop", {
      "GET /deployments": () => [dep, ...deployments.slice(1)],
      "GET /deployments/:id": () => dep,
      "POST /deployments/:id/inline-secrets": (r: { body: { refs: string[] } }) => {
        dep = { ...dep, compose: dep.compose.replace("[[shop-env.TAG]]", "1.27") };
        return dep;
      },
    });
    const panel = await screen.findByRole("region", { name: /secret values in the compose/i }, { timeout: 10_000 });
    const write = within(panel).getByRole("button", { name: /write into the compose/i });
    expect(write).toBeDisabled();
    expect(within(panel).getAllByRole("checkbox")).toHaveLength(2);
    await user.click(within(panel).getByRole("checkbox", { name: "Write shop-env.TAG into the compose" }));
    await user.click(write);
    await toast(/written into the compose/i);
    expect(requests("POST", "/deployments/d-shop/inline-secrets")[0].body).toEqual({ refs: ["shop-env.TAG"] });
    await waitFor(() => expect(within(panel).getAllByRole("checkbox")).toHaveLength(1));
    expect(screen.queryAllByText(/unsaved changes/i)).toHaveLength(0);
  });

  it("hides the secret values panel from operators", async () => {
    const dep = { ...deployments[0], compose: "services:\n  web:\n    image: nginx:[[shop-env.TAG]]\n" };
    renderApp(SHOP + "?tab=config", { role: "operator", api: { "GET /deployments": [dep, ...deployments.slice(1)], "GET /deployments/:id": dep } });
    await screen.findByRole("button", { name: /^save$/i }, { timeout: 10_000 });
    expect(screen.queryByRole("region", { name: /secret values in the compose/i })).toBeNull();
  });

  it("shows deployment logs, policies and activity", async () => {
    const { user } = await open(SHOP, "shop");
    await user.click(screen.getByRole("tab", { name: /^logs$/i }));
    const ws = await waitFor(() => { const s = socket("/deployments/d-shop/logs"); if (!s) throw new Error("no socket"); return s; });
    await act(async () => {
      ws.emit("2026-10-26T10:00:00Z ready to accept connections\n");
      await new Promise((r) => setTimeout(r, 400));
    });
    expect(await screen.findByText(/ready to accept connections/)).toBeInTheDocument();
    await user.click(screen.getByRole("tab", { name: /policies/i }));
    expect(await within(pageRoot()).findAllByText(/auto-update|update/i)).not.toHaveLength(0);
    await user.click(screen.getByRole("tab", { name: /activity/i }));
    await waitFor(() => expect(requests("GET", "/audit").length).toBeGreaterThan(0));
  });

  it("manages the redeploy webhook", async () => {
    const { user } = await open(SHOP, "shop", { "POST /deployments/:id/webhook": { token: "tok-123", url: "/hooks/deploy/tok-123" } });
    await user.click(await screen.findByRole("button", { name: /create webhook/i }, { timeout: 10_000 }));
    await waitFor(() => expect(requests("POST", "/deployments/d-shop/webhook")).toHaveLength(1));
  });

  it("explains a failed deploy with the server's error", async () => {
    const failed = { ...deployments[1], status: "error" };
    const { user } = await open("/workloads/dep~d-api", "api", {
      "GET /deployments/:id": failed,
      "GET /deployments": [deployments[0], failed],
      "GET /deployments/:id/status": [{ hostId: "local", state: "error", message: "pull access denied for ghcr.io/acme/api" }],
    });
    expect(await screen.findByText(/pull access denied/i, {}, { timeout: 10_000 })).toBeInTheDocument();
    await user.click(screen.getAllByRole("button", { name: /^redeploy$/i }).at(-1)!);
    await waitFor(() => expect(requests("POST", "/deployments/d-api/deploy")).toHaveLength(1));
  });

  it("shows the git source of a git deployment", async () => {
    const gitDep = { ...deployments[0], git: { repoUrl: "https://github.com/acme/shop.git", ref: "main", path: "compose.yml", autoDeploy: true } };
    await open(SHOP, "shop", { "GET /deployments/:id": gitDep, "GET /deployments": [gitDep, deployments[1]] });
    expect(await screen.findAllByText(/github\.com\/acme\/shop/i, {}, { timeout: 10_000 })).not.toHaveLength(0);
  });

  it("is read-only for viewers", async () => {
    await open(SHOP, "shop", {}, "viewer");
    expect(screen.queryByRole("button", { name: /^redeploy$/i })).not.toBeInTheDocument();
  });

  it("reports a missing deployment", async () => {
    renderApp("/workloads/dep~gone", { api: { "GET /deployments/:id": reply(404, { error: "deployment not found" }) } });
    expect(await within(pageRoot()).findAllByText(/not found|no workload|doesn.t exist/i, {}, { timeout: 10_000 })).not.toHaveLength(0);
  });
});

describe("workload: stack and container", () => {
  const blog = [
    { ...containers[1], id: "b1".padEnd(64, "0"), names: ["/blog-app-1"], labels: { "com.docker.compose.project": "blog", "com.docker.compose.service": "app" } },
  ];

  it("a discovered compose stack restarts its containers", async () => {
    const { user } = await open("/workloads/stack~local~blog", "blog", { "GET /hosts/:id/containers": [...containers, ...blog] });
    await press(user, /^restart$/i);
    await waitFor(() => expect(requests("POST", "/hosts/local/containers/:id/restart")).toHaveLength(1));
    await user.click(screen.getByRole("tab", { name: /compose/i }));
    expect(await within(pageRoot()).findAllByText(/compose/i)).not.toHaveLength(0);
  });

  it("a lone container starts and shows its spec", async () => {
    const { user } = await open("/workloads/ctr~local~nightly-job", "nightly-job", { "GET /hosts/:h/containers/:id/spec": containerSpec });
    await press(user, /^start$/i);
    await waitFor(() => expect(requests("POST", "/hosts/local/containers/:id/start")).toHaveLength(1));
    await user.click(screen.getByRole("tab", { name: /config/i }));
    expect(await within(pageRoot()).findAllByText(/nginx:1\.27|container spec/i)).not.toHaveLength(0);
    await user.click(screen.getByRole("tab", { name: /activity/i }));
    await waitFor(() => expect(requests("GET", "/hosts/local/events").length).toBeGreaterThan(0));
  });
});

describe("workloads list", () => {
  it("filters and opens a workload", async () => {
    const { user } = renderApp("/workloads");
    await within(pageRoot()).findAllByText("shop", {}, { timeout: 10_000 });
    const search = within(pageRoot()).queryByRole("searchbox") ?? within(pageRoot()).queryByPlaceholderText(/search|filter/i);
    if (search) await user.type(search, "nightly");
    await user.click((await within(pageRoot()).findAllByText("nightly-job"))[0]);
    await waitFor(() => expect(currentPath()).toMatch(/^\/workloads\/ctr~local~nightly-job/));
    toast;
  });
});
