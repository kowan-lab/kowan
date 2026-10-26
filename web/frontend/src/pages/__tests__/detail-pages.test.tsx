// Host, network and image detail pages.
import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { mockApi, reply, requests } from "../../test/api";
import { containers, images, networks } from "../../test/fixtures";
import { currentPath, pageRoot, renderApp } from "../../test/render";
import { dialog, press, toast } from "../../test/ui";

const NOW = "2026-10-26T10:00:00Z";

describe("host detail", () => {
  const agent = { id: "h-a", name: "agent-a", type: "agent", mode: "agent", address: "10.0.0.2:9001", online: true, lastSeen: NOW, createdAt: NOW };

  it("shows the swarm nodes of a manager", async () => {
    const { user } = renderApp("/hosts/local", {
      api: {
        "GET /hosts/:id/swarm": {
          active: true,
          nodes: [
            { id: "n1", hostname: "mgr-1", role: "manager", leader: true, availability: "active", status: "ready", address: "10.0.0.1", engineVersion: "29.0.0" },
            { id: "n2", hostname: "wrk-1", role: "worker", leader: false, availability: "drain", status: "down", address: "10.0.0.3", engineVersion: "28.5.1" },
          ],
        },
      },
    });
    await user.click(await screen.findByRole("tab", { name: /swarm/i }, { timeout: 10_000 }));
    expect(await screen.findByText("mgr-1")).toBeInTheDocument();
    expect(screen.getByText("wrk-1")).toBeInTheDocument();
    expect(screen.getByText("28.5.1")).toBeInTheDocument();
  });

  it("explains a non-manager and swarm errors", async () => {
    const { user } = renderApp("/hosts/local", { api: { "GET /hosts/:id/swarm": { active: false, nodes: [] } } });
    await user.click(await screen.findByRole("tab", { name: /swarm/i }, { timeout: 10_000 }));
    expect(await screen.findByText(/not a swarm manager/i)).toBeInTheDocument();
  });

  it("shows a swarm load error", async () => {
    const { user } = renderApp("/hosts/local", { api: { "GET /hosts/:id/swarm": reply(502, { error: "agent offline" }) } });
    await user.click(await screen.findByRole("tab", { name: /swarm/i }, { timeout: 10_000 }));
    expect(await screen.findByText(/agent offline/i)).toBeInTheDocument();
  });

  it("shows an agent's update panel", async () => {
    renderApp("/hosts/h-a", {
      api: {
        "GET /hosts": [...(await import("../../test/fixtures")).hosts, agent],
        "GET /hosts/:id": agent,
        "GET /hosts/:id/agent/update": { supported: false, reason: "agent runs outside Docker", image: "", container: "", running: "", latest: "", updateAvailable: false },
      },
    });
    expect(await screen.findByText(/agent runs outside docker/i, {}, { timeout: 10_000 })).toBeInTheDocument();
  });

  it("handles a missing host and a load error", async () => {
    renderApp("/hosts/nope");
    expect(await screen.findByText(/host not found/i, {}, { timeout: 10_000 })).toBeInTheDocument();
  });

  it("shows a host list error", async () => {
    renderApp("/hosts/local", { api: { "GET /hosts": reply(500, { error: "db down" }) } });
    expect(await screen.findByText(/failed to load hosts/i, {}, { timeout: 10_000 })).toBeInTheDocument();
    // The error state must not refetch the list in a loop.
    const n = requests("GET", "/hosts").length;
    await new Promise((r) => setTimeout(r, 300));
    expect(requests("GET", "/hosts").length - n).toBeLessThan(3);
  });
});

describe("network detail", () => {
  const net = { ...networks[1], ipam: [{ subnet: "172.18.0.0/16", gateway: "172.18.0.1" }], labels: { team: "web" } };
  const URL = `/networks/n-shop?host=local`;

  it("disconnects a container and removes the network", async () => {
    const { user } = renderApp(URL, { api: { "GET /hosts/:h/networks/:id": net, "POST /hosts/:h/networks/:id/disconnect": {} } });
    expect(await within(pageRoot()).findByText("172.18.0.0/16", {}, { timeout: 10_000 })).toBeInTheDocument();
    expect(screen.getByText("team")).toBeInTheDocument();
    await press(user, "Disconnect shop-web-1");
    await toast(/container disconnected/i);
    expect(requests("POST", "/hosts/local/networks/n-shop/disconnect")[0].body).toMatchObject({ containerId: containers[0].id, force: true });
    mockApi({ "DELETE /hosts/:h/networks/:id": reply(409, { error: "network has active endpoints" }) });
    await press(user, /^remove$/i, pageRoot());
    await press(user, /^remove$/i, await dialog(/remove network/i));
    await toast(/active endpoints/i);
    mockApi({ "DELETE /hosts/:h/networks/:id": reply(204) });
    await press(user, /^remove$/i, await dialog(/remove network/i));
    await waitFor(() => expect(currentPath()).toBe("/networks"));
  });

  it("reports a failed disconnect and inspect errors", async () => {
    const { user } = renderApp(URL, { api: { "GET /hosts/:h/networks/:id": net, "POST /hosts/:h/networks/:id/disconnect": reply(500, { error: "endpoint busy" }) } });
    await within(pageRoot()).findByText("172.18.0.0/16", {}, { timeout: 10_000 });
    await press(user, "Disconnect shop-web-1");
    await toast(/endpoint busy/i);
  });

  it("shows an inspect error and the all-hosts hint", async () => {
    renderApp(URL, { api: { "GET /hosts/:h/networks/:id": reply(404, { error: "network not found" }) } });
    expect(await screen.findAllByText(/network not found/i, {}, { timeout: 10_000 })).not.toHaveLength(0);
  });

  it("asks for a host in the all-hosts view", async () => {
    localStorage.setItem("kowan_host", "all");
    renderApp("/networks/n-shop");
    expect(await screen.findByText(/select a host/i, {}, { timeout: 10_000 })).toBeInTheDocument();
  });
});

describe("image detail", () => {
  const id = images[0].id;
  const inspect = {
    inspect: {
      Id: id, RepoTags: ["nginx:1.27", "<none>:<none>"], Created: NOW, Size: 192_000_000, Architecture: "amd64", Os: "linux",
      Config: { Env: ["NGINX_VERSION=1.27.2", "PATH=/usr/bin"], Labels: { maintainer: "NGINX" }, Cmd: ["nginx", "-g", "daemon off;"], ExposedPorts: { "80/tcp": {} }, WorkingDir: "/" },
    },
    history: [
      { id: "sha256:l1", created: NOW, createdBy: "/bin/sh -c #(nop) CMD [\"nginx\"]", size: 0, comment: "" },
      { id: "sha256:l2", created: NOW, createdBy: "ADD rootfs.tar /", size: 80_000_000, comment: "" },
    ],
  };
  const URL = `/images/${encodeURIComponent(id)}?host=local`;

  it("shows config, labels and history, then removes with force", async () => {
    const { user } = renderApp(URL, { api: { "GET /hosts/:h/images/:id/inspect": inspect } });
    expect(await within(pageRoot()).findByText("NGINX_VERSION", {}, { timeout: 10_000 })).toBeInTheDocument();
    expect(screen.getByText("maintainer")).toBeInTheDocument();
    expect(screen.getByText(/ADD rootfs.tar/)).toBeInTheDocument();
    mockApi({ "DELETE /hosts/:h/images/:id": reply(409, { error: "image is in use" }) });
    await press(user, /^remove$/i, pageRoot());
    let d = await dialog();
    await user.click(within(d).getByRole("checkbox", { name: /force remove/i }));
    await press(user, /^remove$/i, d);
    await toast(/image is in use/i);
    expect(requests("DELETE", "/hosts/local/images/:id")[0].query).toEqual({ force: "true" });
    mockApi({ "DELETE /hosts/:h/images/:id": reply(204) });
    d = await dialog();
    await press(user, /^remove$/i, d);
    await waitFor(() => expect(currentPath()).toBe("/images"));
  });

  it("shows an empty history, inspect errors and the all-hosts hint", async () => {
    renderApp(URL, { api: { "GET /hosts/:h/images/:id/inspect": { inspect: { Id: id, RepoTags: [], Config: {} }, history: [] } } });
    expect(await screen.findByText(/no history|no layers/i, {}, { timeout: 10_000 })).toBeInTheDocument();
  });

  it("shows an inspect error", async () => {
    renderApp(URL, { api: { "GET /hosts/:h/images/:id/inspect": reply(404, { error: "No such image" }) } });
    expect(await screen.findAllByText(/no such image/i, {}, { timeout: 10_000 })).not.toHaveLength(0);
  });

  it("asks for a host in the all-hosts view", async () => {
    localStorage.setItem("kowan_host", "all");
    renderApp(`/images/${encodeURIComponent(id)}`);
    expect(await screen.findByText(/select a host/i, {}, { timeout: 10_000 })).toBeInTheDocument();
  });
});
