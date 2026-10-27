import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { mockApi, reply, requests } from "../../test/api";
import { hosts as fxHosts } from "../../test/fixtures";
import { currentPath, pageRoot, renderApp } from "../../test/render";
import { dialog, press, toast } from "../../test/ui";

const NOW = "2026-10-26T10:00:00Z";
const hosts = [
  ...fxHosts,
  { id: "h-a", name: "agent-a", type: "agent", mode: "agent", address: "10.0.0.2:9001", online: true, lastSeen: NOW, createdAt: NOW },
  { id: "h-b", name: "agent-b", type: "agent", mode: "agent", address: "10.0.0.3:9001", online: true, lastSeen: NOW, createdAt: NOW },
];
const status = (over: Record<string, unknown> = {}) => ({ supported: true, image: "kowan-agent", container: "kowan-agent", running: "sha256:a", latest: "sha256:a", updateAvailable: false, ...over });
const api = {
  "GET /hosts": hosts,
  "GET /hosts/:id/agent/update": (r: { params: Record<string, string> }) =>
    r.params.id === "h-a" ? status({ updateAvailable: true, latest: "sha256:b" })
      : r.params.id === "h-b" ? status()
        : r.params.id === "h-edge" ? status({ running: "", latest: "", error: "registry unreachable" })
          : status({ supported: false }),
};

async function ready() {
  // The header's breadcrumb names the selected host too.
  return within(pageRoot()).findAllByText("agent-b", {}, { timeout: 10_000 });
}

describe("hosts page", () => {
  it("adds an agent host and shows its docker command", async () => {
    const { user } = renderApp("/hosts", {
      api: { ...api, "POST /hosts": (r) => ({ host: { ...hosts[2], id: "h-new", name: r.body.name }, dockerRun: "docker run -d kowan-agent --token abc" }) },
    });
    await ready();
    await press(user, /^add host$/i, pageRoot());
    const d = await dialog(/add host/i);
    await press(user, /^add|^create/i, within(d).getByRole("button", { name: /^add host$|^add$|^create$/i }).parentElement!);
    expect(await within(d).findByText(/name is required/i)).toBeInTheDocument();
    expect(within(d).getByText(/address is required/i)).toBeInTheDocument();
    await user.type(within(d).getByLabelText(/^name/i), " node-3 ");
    await user.type(within(d).getByLabelText(/^address/i), "10.0.0.4:9001");
    await user.click(within(d).getByRole("button", { name: /^add host$|^add$|^create$/i }));
    await toast(/host added/i);
    expect(requests("POST", "/hosts")[0].body).toEqual({ name: "node-3", mode: "agent", address: "10.0.0.4:9001" });
    const e = await dialog();
    expect(within(e).getByText("docker run -d kowan-agent --token abc")).toBeInTheDocument();
    await press(user, /copy docker command/i, e);
    expect(await navigator.clipboard.readText()).toBe("docker run -d kowan-agent --token abc");
  });

  it("adds an edge host without an address and reports errors", async () => {
    const { user } = renderApp("/hosts?add=1", { api: { ...api, "POST /hosts": reply(409, { error: "name already used" }) } });
    const d = await dialog(/add host/i);
    await user.click(within(d).getByRole("tab", { name: /^edge$/i }));
    expect(within(d).queryByLabelText(/^address/i)).toBeNull();
    await user.type(within(d).getByLabelText(/^name/i), "edge-2");
    await user.click(within(d).getByRole("button", { name: /^add host$|^add$|^create$/i }));
    await toast(/name already used/i);
    expect(requests("POST", "/hosts")[0].body).toEqual({ name: "edge-2", mode: "edge" });
    expect(currentPath()).toBe("/hosts");
  });

  it("shows join commands and removes hosts", async () => {
    localStorage.setItem("kowan_host", "h-b");
    const { user } = renderApp("/hosts", { api: { ...api, "GET /hosts/:id/edge-command": { host: hosts[1], dockerRun: "docker run edge --token x" } } });
    await ready();
    await press(user, "Join command for edge-1");
    expect(await screen.findByText("docker run edge --token x")).toBeInTheDocument();
    await press(user, /^done$|^close$/i, await dialog());
    await press(user, "Remove agent-b");
    await press(user, /^remove$/i, await dialog(/remove host/i));
    await toast(/host removed/i);
    expect(requests("DELETE", "/hosts/h-b")).toHaveLength(1);
    expect(localStorage.getItem("kowan_host")).toBe("local");
    mockApi({ "DELETE /hosts/:id": reply(409, { error: "host has deployments" }), "GET /hosts/:id/edge-command": reply(500, { error: "no token" }) });
    await press(user, "Remove agent-a");
    await press(user, /^remove$/i, await dialog(/remove host/i));
    await toast(/host has deployments/i);
    await press(user, /^cancel$/i, await dialog(/remove host/i));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    await press(user, "Join command for edge-1");
    await toast(/no token/i);
  });

  it("shows agent versions and updates one agent", async () => {
    const { user } = renderApp("/hosts", { api: { ...api, "POST /hosts/:id/agent/update": { id: "u-1" } } });
    await ready();
    expect(await screen.findByText(/update available/i)).toBeInTheDocument();
    expect(screen.getByText(/up to date/i)).toBeInTheDocument();
    expect(screen.getByText(/version unknown/i)).toBeInTheDocument();
    await press(user, /^update$/i, pageRoot());
    expect(currentPath()).toBe("/hosts");
    await press(user, /^update agent$/i, await dialog(/update agent/i));
    await waitFor(() => expect(currentPath()).toBe("/hosts/h-a"));
  });

  it("reports a failed agent update", async () => {
    const { user } = renderApp("/hosts", { api: { ...api, "POST /hosts/:id/agent/update": reply(502, { error: "pull failed" }) } });
    await ready();
    await user.click(await within(pageRoot()).findByRole("button", { name: /^update$/i }));
    await press(user, /^update agent$/i, await dialog(/update agent/i));
    await toast(/pull failed/i);
  });

  it("rolls an update across agents", async () => {
    const { user } = renderApp("/hosts", {
      api: { ...api, "POST /agents/update": { batchId: "b1", ids: ["u-a"] }, "GET /system/updates/:id": { id: "u-a", status: "succeeded", phases: [] } },
    });
    await ready();
    await press(user, /update agents/i, pageRoot());
    const d = await dialog(/update agents/i);
    await waitFor(() => expect(within(d).getByRole("checkbox", { name: "Update agent-a" })).toBeChecked());
    expect(within(d).getByRole("checkbox", { name: "Update agent-b" })).not.toBeChecked();
    await press(user, /update 1 agent$/i, d);
    await waitFor(() => expect(requests("POST", "/agents/update")[0]?.body).toEqual({ hostIds: ["h-a"] }));
    expect(await within(d).findAllByRole("button", { name: /^close$/i })).toHaveLength(2);
  });

  it("reports a rolling update that cannot start", async () => {
    const { user } = renderApp("/hosts", { api: { ...api, "POST /agents/update": reply(409, { error: "another update runs" }) } });
    await ready();
    await press(user, /update agents/i, pageRoot());
    const d = await dialog(/update agents/i);
    await user.click(within(d).getByRole("checkbox", { name: "Update agent-b" }));
    await waitFor(() => expect(within(d).getByRole("button", { name: /update \d agents?/i })).toBeEnabled());
    await press(user, /update \d agents?/i, d);
    await toast(/another update runs/i);
  });

  it("opens a host and shows load errors", async () => {
    const { user } = renderApp("/hosts", { api });
    await ready();
    await user.click(screen.getByText("agent-b"));
    await waitFor(() => expect(currentPath()).toBe("/hosts/h-b"));
  });

  it("renames a host from its row", async () => {
    const { user } = renderApp("/hosts", { api: { ...api, "PATCH /hosts/:id": (r) => ({ ...hosts[2], name: r.body.name }) } });
    await ready();
    await user.click(within(pageRoot()).getByRole("button", { name: "Rename agent-a" }));
    const d = await dialog(/rename host/i);
    const input = within(d).getByLabelText(/^name/i);
    expect(input).toHaveValue("agent-a");
    await user.clear(input);
    await user.type(input, "rack-1");
    await user.click(within(d).getByRole("button", { name: /^rename$/i }));
    await toast(/host renamed/i);
    expect(requests("PATCH", "/hosts/h-a")[0].body).toEqual({ name: "rack-1" });
  });

  it("offers rename only to admins", async () => {
    renderApp("/hosts", { role: "viewer", api });
    await ready();
    expect(within(pageRoot()).queryByRole("button", { name: /^rename /i })).toBeNull();
  });

  it("shows a load error", async () => {
    renderApp("/hosts", { api: { "GET /hosts": reply(500, { error: "db down" }) } });
    expect(await screen.findAllByText(/db down/i, {}, { timeout: 10_000 })).not.toHaveLength(0);
  });
});
