import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { reply, requests } from "../../test/api";
import { currentPath, pageRoot, renderApp } from "../../test/render";
import { choose, press, toast } from "../../test/ui";

const created = (r: { body: unknown }) => ({ id: "d-new", status: "stopped", targets: ["local"], ...(r.body as object) });

async function start(api: Record<string, unknown> = {}) {
  const r = renderApp("/deployments/new", { api: { "POST /deployments": created, ...api } });
  await within(pageRoot()).findAllByText(/new deployment/i, {}, { timeout: 10_000 });
  return r;
}

async function pickHost(user: Parameters<typeof choose>[0]) {
  const host = screen.queryByRole("combobox", { name: /^host/i });
  if (host && !/local/.test(host.textContent ?? "")) await choose(user, /^host/i, /local/i);
}

describe("new deployment", () => {
  it("creates a container deployment with ports, env and after-create settings", async () => {
    const { user } = await start();
    const page = pageRoot();
    await user.type(within(page).getByLabelText(/^name/i), "api");
    await user.type(within(page).getAllByLabelText(/^image/i)[0], "ghcr.io/acme/api:2.1");
    await pickHost(user);
    await press(user, /map additional port/i, page);
    const hostPorts = within(page).getAllByPlaceholderText(/host \(0 = random\)/i);
    const ctrPorts = within(page).getAllByPlaceholderText(/^container$/i);
    await user.type(hostPorts.at(-1)!, "9000");
    await user.type(ctrPorts.at(-1)!, "9000");
    await press(user, /add variable/i, page);
    await user.type(within(page).getByRole("textbox", { name: "Name of variable 1" }), "MODE");
    await user.type(within(page).getByRole("textbox", { name: "Value of variable 1" }), "prod");
    // After creating: protection, webhook, notify on updates, a start/stop schedule.
    await user.click(within(page).getByLabelText(/protect from deletion/i));
    await user.click(within(page).getByLabelText(/create a redeploy webhook/i));
    await user.click(within(page).getByRole("slider", { name: /auto-update/i }));
    await user.click(within(page).getByRole("switch", { name: /schedule/i }));
    await user.click(within(page).getByLabelText(/deploy right after creating/i)); // off: don't deploy
    await press(user, /^create$/i, page);
    await waitFor(() => expect(currentPath()).toBe("/workloads/dep~d-new"));
    const body = requests("POST", "/deployments")[0].body as Record<string, unknown>;
    expect(body).toMatchObject({ name: "api", type: "container", targets: ["local"] });
    expect(JSON.stringify(body)).toContain("MODE");
    expect(requests("PUT", "/deployments/d-new/protection")).toHaveLength(1);
    expect(requests("POST", "/deployments/d-new/webhook")).toHaveLength(1);
    expect(requests("PUT", "/deployments/d-new/autoupdate")[0].body).toMatchObject({ mode: "notify" });
    expect(requests("POST", "/schedules")).toHaveLength(2);
    expect(requests("POST", "/deployments/d-new/deploy")).toHaveLength(0);
  });

  it("builds a compose file with the easy editor and deploys it", async () => {
    const { user } = await start();
    const page = pageRoot();
    await user.type(within(page).getByLabelText(/^name/i), "blog");
    await user.click(within(page).getByLabelText(/^compose$/i));
    await pickHost(user);
    await press(user, /add service/i, page);
    await user.type(within(page).getAllByLabelText(/service name/i).at(-1)!, "web");
    await user.type(within(page).getAllByLabelText(/^image/i).at(-1)!, "nginx:1.27");
    await waitFor(() => expect(requests("POST", "/compose/validate").length).toBeGreaterThan(0), { timeout: 5000 });
    await new Promise((r) => setTimeout(r, 600));
    await press(user, /^create$/i, page);
    await waitFor(() => expect(requests("POST", "/deployments")).toHaveLength(1));
    const body = requests("POST", "/deployments")[0].body as { compose: string; type: string };
    expect(body.type).toBe("compose");
    expect(body.compose).toContain("web:");
    expect(body.compose).toContain("nginx:1.27");
    await waitFor(() => expect(requests("POST", "/deployments/d-new/deploy")).toHaveLength(1));
  });

  it("validates before sending and reports partial failures", async () => {
    const { user } = await start({ "POST /deployments/:id/webhook": reply(500, { error: "db locked" }) });
    const page = pageRoot();
    await press(user, /^create$/i, page);
    expect(await within(page).findByText(/name is required/i)).toBeInTheDocument();
    expect(requests("POST", "/deployments")).toHaveLength(0);
    await user.type(within(page).getByLabelText(/^name/i), "api");
    await press(user, /^create$/i, page);
    expect(await within(page).findByText(/image is required/i)).toBeInTheDocument();
    await user.type(within(page).getAllByLabelText(/^image/i)[0], "nginx:1.27");
    await pickHost(user);
    await user.click(within(page).getByLabelText(/create a redeploy webhook/i));
    await press(user, /^create$/i, page);
    await toast(/not everything was set/i);
  });

  it("reports a server error and cancels back to workloads", async () => {
    const { user } = await start({ "POST /deployments": reply(409, { error: "a deployment with that name already exists" }) });
    const page = pageRoot();
    await user.type(within(page).getByLabelText(/^name/i), "shop");
    await user.type(within(page).getAllByLabelText(/^image/i)[0], "nginx:1.27");
    await pickHost(user);
    await press(user, /^create$/i, page);
    await toast(/already exists/i);
    await press(user, /^cancel$/i, page);
    await waitFor(() => expect(currentPath()).toBe("/workloads"));
  });
});

describe("new deployment host", () => {
  it("starts on the host selected in the shell", async () => {
    localStorage.setItem("kowan_host", "local");
    renderApp("/deployments/new");
    const host = await screen.findByRole("combobox", { name: /^host/i }, { timeout: 10_000 });
    expect(host.textContent).toMatch(/local/);
  });
});
